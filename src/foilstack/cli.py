"""Command line: ingest a catalogue, then encode it.

Two commands and not one, because they fail for different reasons and take
wildly different amounts of time. Ingest is a few minutes of HTTP against the
upstream source. Encoding downloads every reference image and pushes it through
the model, which is the long pole. Separating them means a network blip in hour
two does not cost you hour one.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import os
import sys
from pathlib import Path

import httpx
from sqlalchemy import func, select, text

from foilstack import __version__, db
from foilstack.config import get_settings
from foilstack.embedding import embed_image
from foilstack.plugins import export_plugins, source_plugins

logging.basicConfig(level=logging.INFO, format="%(message)s")
# Distinguishes "upstream has no image for this card", which is ordinary and
# permanent, from "we could not get it", which is worth another go later.
MISSING = object()


def image_is_permanently_missing(status_code: int, body: bytes) -> bool:
    """Whether upstream has told us, for good, that there is no image here.

    Two ways it says so. A 4xx is the obvious one. The other is a 200 carrying
    an empty body — `image/jpeg`, zero bytes — which a handful of products
    answer with and which is just as permanent, but arrives looking like
    success. Untreated it reached the encoder, failed to decode, and was
    retried four times with backoff on every run, forever.

    429 is excluded even though it is a 4xx. The caller handles it before ever
    reaching here, so in practice it never arrives — but "too many requests" is
    the opposite of a permanent answer, and a policy function that is only
    correct because of what its one caller happens to do first is a trap for
    the second caller.

    Deliberately not a method on anything: it is the whole retry policy for
    catalogue images in two lines, and it is worth being able to read and test
    it without a network.
    """
    if status_code == 429:
        return False
    return (400 <= status_code < 500) or (status_code == 200 and not body)


IMAGE_HEADERS = {
    "User-Agent": f"foilstack/{__version__} (+https://github.com/foilstack/foilstack)",
    "Accept": "image/*,*/*",
}

log = logging.getLogger("foilstack")


async def cmd_ingest(args) -> int:
    settings = get_settings()
    db.init(settings.database_url)
    plugins = source_plugins()
    plugin = plugins.get(args.source)
    if plugin is None:
        log.error("unknown source %r; available: %s", args.source, sorted(plugins))
        return 2

    if args.game or getattr(args, "set", None):
        plugin = type(plugin)(game=args.game or plugin.game, set_code=getattr(args, "set", None))

    session = db.session()
    seen = 0
    async for record in plugin.fetch(limit=args.limit):
        namespaced = f"{plugin.name}:{record.source_id}"
        existing = session.scalar(select(db.Card).where(db.Card.source_id == namespaced))
        if existing is None:
            session.add(
                db.Card(
                    source=plugin.name,
                    source_id=namespaced,
                    name=record.name,
                    game=record.game,
                    set_name=record.set_name,
                    number=record.number,
                    variant=record.variant,
                    image_url=record.image_url,
                    market=record.market,
                    currency=record.currency,
                )
            )
        else:
            existing.market = record.market
            existing.image_url = record.image_url
        seen += 1
        if seen % 250 == 0:
            session.commit()
            log.info("  %s cards", seen)
    session.commit()
    log.info("ingested %s cards from %s", seen, plugin.name)
    return 0


async def cmd_embed(args) -> int:
    """Encode every catalogue image that this encoder has not already done.

    Resumable by default, which matters because this is the long pole: on a
    real catalogue it is hours, and a network blip an hour in should cost the
    blip, not the hour. `--all` forces a re-encode, which is what a model
    change requires — vectors from two encoders are not comparable.
    """
    settings = get_settings()
    db.init(settings.database_url)
    session = db.session()

    query = select(db.Card).where(db.Card.image_url.is_not(None))
    if not args.retry_missing:
        # Cards upstream has already said it has no image for. Skipped by
        # default because that answer does not change: re-asking costs a
        # request per card per run and returns the same 404.
        query = query.where(db.Card.image_missing_at.is_(None))
    if not args.all:
        done = select(db.CardEmbedding.card_id).where(
            db.CardEmbedding.model == settings.embed_model
        )
        query = query.where(db.Card.id.not_in(done))
    cards = session.scalars(query.order_by(db.Card.id)).all()
    if args.limit:
        cards = cards[: args.limit]
    if not cards:
        total = session.scalar(select(func.count(db.Card.id))) or 0
        if total == 0:
            log.error("no cards with images. run `ingest` first")
            return 2
        skipped = (
            session.scalar(
                select(func.count(db.Card.id)).where(db.Card.image_missing_at.is_not(None))
            )
            or 0
        )
        log.info("nothing to do: all %s cards are encoded with %s", total, settings.embed_model)
        if skipped:
            # Named rather than left as a silent gap between the catalogue
            # count and the vector count, which otherwise looks like a
            # half-finished encode that never finishes.
            log.info("  (%s have no image upstream and are skipped)", skipped)
        return 0

    log.info(
        "encoding %s cards with %s (%s at a time)",
        len(cards),
        settings.embed_model,
        args.concurrency,
    )
    written = failed = missing = processed = 0
    # Bounded, not unbounded. The reference images come from a CDN rather than
    # from the catalogue API, so the pacing that applies to `sync-prices` is not
    # the constraint here — but "not the constraint" is not a licence to open a
    # thousand sockets at somebody else's expense. A small pool overlaps the
    # download of the next card with the encoding of the current one, which is
    # where nearly all of the wall clock goes.
    limiter = asyncio.Semaphore(max(1, args.concurrency))

    async def fetch_and_encode(client, card):
        """One card, with the manners a shared CDN deserves.

        Retries what might work and gives up immediately on what will not. A
        catalogue this size is full of promo and staff entries that upstream
        has no image for at all — those answer 403 or 404, forever, and
        retrying one four times with backoff costs seven seconds to learn
        nothing. Thousands of them turned a run that should take an hour into
        one that had not finished a tenth of it.

        "Will not work" includes a 200 carrying an empty body, which is the
        same permanent answer dressed as success.
        """
        async with limiter:
            for attempt in range(4):
                try:
                    image = await client.get(card.image_url, headers=IMAGE_HEADERS)
                    if image.status_code in (429, 502, 503, 504):
                        # Honour Retry-After when they send one; they know
                        # better than a guess does.
                        wait = float(image.headers.get("Retry-After") or 2**attempt)
                        log.warning("  %s on %s, waiting %.0fs", image.status_code, card.name, wait)
                        await asyncio.sleep(min(wait, 60))
                        continue
                    if image_is_permanently_missing(image.status_code, image.content):
                        # Not a warning: on this catalogue it is thousands of
                        # lines saying the same ordinary thing, and the count
                        # at the end says it once.
                        log.debug("  no image upstream for %s (%s)", card.name, image.status_code)
                        return card.id, MISSING
                    image.raise_for_status()
                    vector = await embed_image(settings.embedder_url, image.content)
                except Exception as exc:  # noqa: BLE001 - one bad image must not end the run
                    if attempt == 3:
                        log.warning("  skip %s (%s)", card.name, type(exc).__name__)
                        return card.id, None
                    await asyncio.sleep(2**attempt)
                    continue
                return card.id, vector
            return card.id, None

    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        pending = [asyncio.create_task(fetch_and_encode(client, c)) for c in cards]
        for coro in asyncio.as_completed(pending):
            card_id, vector = await coro
            processed += 1
            if vector is MISSING:
                missing += 1
                # Recorded, not just counted. This is the whole point of the
                # sentinel: a permanent answer is worth keeping.
                card = session.get(db.Card, card_id)
                if card is not None:
                    card.image_missing_at = dt.datetime.now(dt.UTC)
            elif vector is None:
                failed += 1
            else:
                session.merge(
                    db.CardEmbedding(
                        card_id=card_id,
                        embedding=[float(x) for x in vector],
                        model=settings.embed_model,
                    )
                )
                written += 1
            # Committed in batches so an interrupted run keeps its work: the
            # point of resumability is lost if everything lives in one
            # transaction that a Ctrl-C rolls back.
            if processed % 100 == 0:
                session.commit()
                log.info(
                    "  encoded %s/%s (%s no image, %s failed)",
                    processed,
                    len(cards),
                    missing,
                    failed,
                )

    session.commit()
    log.info("wrote %s vectors (%s have no image upstream, %s failed)", written, missing, failed)
    if missing:
        log.info("  those %s will be skipped from now on. `--retry-missing` to ask again", missing)
    # A batch where every remaining card simply has no image upstream is a
    # finished job, not a failed one — and on a resumed run over a large
    # catalogue that is exactly what the last batch looks like.
    return 0 if (written or missing) else 1


async def cmd_sets(args) -> int:
    """List the sets a source can fetch, so `--set` is a choice not a guess."""
    plugins = source_plugins()
    plugin = plugins.get(args.source)
    if plugin is None:
        log.error("unknown source %r; available: %s", args.source, sorted(plugins))
        return 2
    plugin = type(plugin)(game=args.game)
    if not hasattr(plugin, "sets"):
        log.error("%s does not publish a set list", plugin.name)
        return 2

    rows = await plugin.sets()
    log.info("%-10s %-46s %s", "CODE", "SET", "RELEASED")
    for row in rows:
        if args.contains and args.contains.lower() not in (row["name"] or "").lower():
            continue
        log.info(
            "%-10s %-46s %s",
            row["abbreviation"] or row["group_id"],
            (row["name"] or "")[:46],
            row["published_on"],
        )
    log.info("%s sets in %s", len(rows), args.game)
    return 0


async def cmd_rematch(args) -> int:
    """Re-run matching over scans already imported.

    The reason this exists: ingesting the set a seller actually collects is the
    fix for "everything matched the wrong game", and without this the only way
    to benefit from a newly ingested set is to upload the same archive again.
    The images are already on disk and already paid for.
    """
    settings = get_settings()
    db.init(settings.database_url)
    session = db.session()

    from foilstack.importing import rematch_scan

    query = select(db.Scan)
    if args.status != "all":
        query = query.where(db.Scan.status == args.status)
    if args.user:
        user = session.scalar(select(db.User).where(db.User.email == args.user.lower()))
        if user is None:
            log.error("no account with email %s", args.user)
            return 2
        query = query.where(db.Scan.user_id == user.id)
    scans = session.scalars(query.order_by(db.Scan.id)).all()
    if not scans:
        log.info("no scans with status %r", args.status)
        return 0

    log.info("re-matching %s scans", len(scans))
    changed = failed = 0
    for i, scan in enumerate(scans, 1):
        try:
            if await rematch_scan(session, scan, settings):
                changed += 1
        except Exception as exc:  # noqa: BLE001 - one bad scan must not end the run
            log.warning("  skip %s (%s)", scan.filename, type(exc).__name__)
            failed += 1
        if i % 25 == 0:
            session.commit()
            log.info("  %s/%s", i, len(scans))
    session.commit()
    log.info("re-matched %s scans (%s now have a match, %s failed)", len(scans), changed, failed)
    return 0


async def cmd_sync_prices(args) -> int:
    """Refresh prices for one game, or for every game in the catalogue.

    `--game all` exists because the alternative was a list someone has to
    remember to update. Prices were synced for whatever games were named in an
    environment variable, so ingesting a game and forgetting to add it there
    left it priced at whatever `ingest` first saw — for good, with nothing
    anywhere saying so. What should be synced is not a setting; it is whatever
    has been ingested, which the database already knows.
    """
    settings = get_settings()
    db.init(settings.database_url)
    session = db.session()

    plugins = source_plugins()
    plugin = plugins.get(args.source)
    if plugin is None:
        log.error("unknown source %r; available: %s", args.source, sorted(plugins))
        return 2
    if not hasattr(plugin, "fetch_prices"):
        log.error("%s does not publish prices", plugin.name)
        return 2

    if args.game == "all":
        games = list(
            session.scalars(
                select(db.Card.game)
                .where(db.Card.source == plugin.name)
                .distinct()
                .order_by(db.Card.game)
            ).all()
        )
        if not games:
            log.error("no %s cards ingested yet. run `foilstack ingest` first", plugin.name)
            return 2
        log.info("syncing every ingested game: %s", " ".join(games))
    else:
        games = [args.game]

    # Read once and shared across games. The build timestamp is one file
    # covering the whole source, so asking for it per game would multiply the
    # cheapest part of this by the number of catalogues for no new information.
    stamp = None
    probe = type(plugin)(game=games[0], set_code=args.set)
    if hasattr(probe, "last_updated"):
        try:
            stamp = await probe.last_updated()
        except Exception as exc:  # noqa: BLE001 - a missing stamp is not fatal
            log.warning("could not read upstream timestamp (%s)", type(exc).__name__)

    worst = 0
    for game in games:
        code = await _sync_one_game(session, plugin, game, stamp, args)
        worst = max(worst, code)
    return worst


async def _sync_one_game(session, plugin, game: str, stamp: str | None, args) -> int:
    """Refresh prices for one game.

    Follows TCGCSV's stated usage guidelines rather than polling blindly: their
    files rebuild exactly once a day, they publish the build timestamp, and
    they ask that a full sync run only when it is newer than your last pull.
    So on an unchanged stamp this returns having made no requests at all.

    History is appended only where a number actually changed. A catalogue that
    has not moved writes nothing.
    """
    plugin = type(plugin)(game=game, set_code=args.set)

    # Keyed per game, not per source. One row for the whole source meant a
    # successful Magic sync recorded the upstream stamp globally, and every
    # other catalogue then saw its own first run as "already up to date" and
    # never pulled a price.
    kind = f"prices:{game}"
    state = session.get(db.SyncState, (plugin.name, kind))
    if stamp and state is not None and state.upstream_stamp == stamp and not args.force:
        log.info("%s: upstream unchanged since %s, nothing to do", game, stamp)
        state.last_run_at = _now()
        session.commit()
        return 0
    log.info("syncing %s prices (upstream build %s)", game, stamp or "unknown")

    # Map upstream ids to our card rows once, rather than querying per price.
    cards = {
        source_id.split(":", 1)[-1]: card_id
        for card_id, source_id in session.execute(
            select(db.Card.id, db.Card.source_id).where(
                db.Card.source == plugin.name, db.Card.game == game
            )
        ).all()
    }
    if not cards:
        log.error("no %s %s cards ingested yet. run `foilstack ingest` first", plugin.name, game)
        return 2

    today = dt.date.today()
    seen = changed = 0
    async for record in plugin.fetch_prices():
        card_id = cards.get(record.source_id)
        if card_id is None:
            continue  # a printing we have not ingested
        seen += 1
        current = session.get(db.CardPrice, (card_id, record.sub_type))
        fields = ("market", "low", "mid", "high")
        incoming = {f: getattr(record, f) for f in fields}

        if current is None:
            session.add(db.CardPrice(card_id=card_id, sub_type=record.sub_type, **incoming))
            moved = True
        else:
            moved = any(getattr(current, f) != incoming[f] for f in fields)
            for f in fields:
                setattr(current, f, incoming[f])

        if moved:
            # `merge` rather than `add`: a second run on the same day should
            # correct that day's reading, not collide with it.
            session.merge(
                db.CardPriceHistory(
                    card_id=card_id,
                    sub_type=record.sub_type,
                    recorded_on=today,
                    **incoming,
                )
            )
            changed += 1
        if seen % 500 == 0:
            session.commit()
            log.info("  %s printings (%s changed)", seen, changed)

    # Keep the card's headline price current too. It is the fallback used when
    # a printing has no row of its own, and left alone it would still hold
    # whatever `ingest` saw months ago — a stale number quietly standing in for
    # a fresh one is worse than no number at all.
    session.execute(
        text("""
        UPDATE cards c SET market = p.market, updated_at = now()
          FROM (
            SELECT DISTINCT ON (card_id) card_id, market
              FROM card_prices
             WHERE market IS NOT NULL
             ORDER BY card_id, (sub_type ILIKE '%foil%'), market
          ) p
         WHERE p.card_id = c.id AND c.source = :source AND c.game = :game
           AND (c.market IS DISTINCT FROM p.market)
    """),
        {"source": plugin.name, "game": game},
    )

    session.merge(
        db.SyncState(
            source=plugin.name,
            kind=kind,
            upstream_stamp=stamp,
            last_run_at=_now(),
            rows_changed=changed,
            message=f"{seen} printings, {changed} changed",
        )
    )
    session.commit()
    log.info("%s: synced %s printings, %s price changes recorded", game, seen, changed)
    return 0


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def cmd_migrate(args) -> int:
    """Bring the database up to the current schema.

    Exists so a pip-installed foilstack can create its own tables. The compose
    deployment runs `alembic upgrade head` from the repository, which needs
    `alembic.ini` and a migrations directory beside it — neither of which a
    wheel installed into site-packages has any reason to have.

    The revisions therefore live inside the package, at
    `foilstack/migrations`, and this points alembic at wherever that turned out
    to be. `env.py` already reads the URL from `foilstack.config`, so there is
    nothing for a config file to carry.
    """
    from alembic import command
    from alembic.config import Config

    settings = get_settings()
    here = Path(__file__).resolve().parent / "migrations"
    if not here.is_dir():
        log.error("migrations are missing from the installed package (%s)", here)
        return 2

    cfg = Config()
    cfg.set_main_option("script_location", str(here))
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    log.info("migrating %s", settings.database_url.rsplit("@", 1)[-1])
    command.upgrade(cfg, args.revision)
    log.info("schema is up to date")
    return 0


def cmd_plugins(_args) -> int:
    sources = source_plugins()
    exports = export_plugins()
    print("sources:")
    for name, plugin in sources.items():
        print(f"  {name:12s} games: {', '.join(plugin.games)}")
    print("exports:")
    for name, spec in exports.items():
        print(f"  {name:12s} {len(spec.columns)} columns -> {spec.filename}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="foilstack")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="pull a catalogue from a source plugin")
    p_ingest.add_argument("--source", default="tcgcsv")
    p_ingest.add_argument("--game", default=None)
    p_ingest.add_argument(
        "--set",
        default=None,
        help="one set only, by code or name (see `foilstack sets`)",
    )
    p_ingest.add_argument("--limit", type=int, default=None)
    p_ingest.set_defaults(fn=cmd_ingest, is_async=True)

    p_embed = sub.add_parser("embed", help="encode catalogue images into vectors")
    p_embed.add_argument("--limit", type=int, default=None)
    p_embed.add_argument(
        "--concurrency",
        type=int,
        default=int(os.getenv("FOILSTACK_EMBED_CONCURRENCY", "8")),
        help="cards in flight at once (default 8)",
    )
    p_embed.add_argument(
        "--all",
        action="store_true",
        help="re-encode cards that already have a vector for this model",
    )
    p_embed.add_argument(
        "--retry-missing",
        action="store_true",
        help="also try cards upstream previously had no image for",
    )
    p_embed.set_defaults(fn=cmd_embed, is_async=True)

    p_sets = sub.add_parser("sets", help="list the sets a source can fetch")
    p_sets.add_argument("--source", default="tcgcsv")
    p_sets.add_argument("--game", default="pokemon")
    p_sets.add_argument("--contains", default=None, help="filter by name substring")
    p_sets.set_defaults(fn=cmd_sets, is_async=True)

    p_rematch = sub.add_parser(
        "rematch",
        help="re-run matching over scans already imported",
    )
    p_rematch.add_argument(
        "--status",
        default="unmatched",
        choices=["unmatched", "pending", "error", "all"],
        help="which scans to redo (default: unmatched)",
    )
    p_rematch.add_argument("--user", default=None, help="limit to one account's scans")
    p_rematch.set_defaults(fn=cmd_rematch, is_async=True)

    p_sync = sub.add_parser(
        "sync-prices",
        help="refresh prices and record the ones that changed",
    )
    p_sync.add_argument("--source", default="tcgcsv")
    p_sync.add_argument(
        "--game",
        default="all",
        help="one game, or 'all' for every game in the catalogue (default)",
    )
    p_sync.add_argument("--set", default=None, help="one set only")
    p_sync.add_argument(
        "--force",
        action="store_true",
        help="sync even if upstream reports no new build",
    )
    p_sync.set_defaults(fn=cmd_sync_prices, is_async=True)

    p_migrate = sub.add_parser("migrate", help="create or update the database schema")
    p_migrate.add_argument(
        "revision",
        nargs="?",
        default="head",
        help="target revision (default: head)",
    )
    p_migrate.set_defaults(fn=cmd_migrate, is_async=False)

    p_plugins = sub.add_parser("plugins", help="list installed plugins")
    p_plugins.set_defaults(fn=cmd_plugins, is_async=False)

    args = parser.parse_args(argv)
    if getattr(args, "is_async", False):
        return asyncio.run(args.fn(args))
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())

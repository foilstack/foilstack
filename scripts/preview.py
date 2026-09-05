"""Spin up a throwaway instance with sample data, for looking at the UI.

A disposable database and a signed-in account, so screenshots never require
touching a real deployment or anybody's password. Catalogue rows are copied
from whatever database DATABASE_URL points at, because the reference images are
what make the screens look like themselves — no inventory, scans or accounts
come across.

    uv run python scripts/preview.py --port 8099        # serve until Ctrl-C
    uv run python scripts/preview.py --shots ./shots    # screenshot and exit
    uv run python scripts/preview.py --demo src/foilstack/web/static/demo   # record the walkthrough
    uv run python scripts/preview.py --landing src/foilstack/web/static/shots  # the landing stills
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import create_engine, func, select, text

EMAIL = "preview@foilstack.invalid"
PASSWORD = "preview-only-password"

# How many scans wait in the review queue. Enough to fill the pane, so the
# screen shows what reviewing a batch is actually like.
PENDING = 7

# How many of those waiting came in on the second, newer upload. Split so the
# queue shows more than one section — with the rest in the older one, because a
# batch part-way through being reviewed is the ordinary state.
#
# The dearest cards are deliberately in the *older* batch. The queue puts the
# earliest upload first, and the landing hero and the README animation both
# shoot the top of that list: leave the good cards in the recent batch and both
# open on three twenty-cent commons, which is a screenshot that argues nothing.
# Same reasoning as seeding runners-up — the fixture has to show what the
# product does.
RECENT = 3


def _admin_url() -> str:
    from foilstack.config import get_settings

    return get_settings().database_url


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--shots", type=Path, default=None)
    ap.add_argument("--demo", type=Path, default=None, help="record the walkthrough and exit")
    ap.add_argument(
        "--landing", type=Path, default=None, help="shoot the landing-page stills and exit"
    )
    ap.add_argument("--keep", action="store_true", help="leave the database behind")
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--height", type=int, default=900)
    ap.add_argument("--only", default=None, help="shoot just this screen by name")
    ap.add_argument("--scale", type=int, default=None, help="device pixel ratio for --demo")
    ap.add_argument("--keep-frames", action="store_true", help="leave --demo frames on disk")
    ap.add_argument(
        "--bulk",
        type=int,
        default=0,
        metavar="N",
        help="pad inventory to N rows, to see the screens a large seller sees",
    )
    args = ap.parse_args(argv)

    source_url = _admin_url()
    name = f"foilstack_preview_{uuid.uuid4().hex[:8]}"
    base = source_url.rsplit("/", 1)[0]
    preview_url = f"{base}/{name}"

    admin = create_engine(source_url, isolation_level="AUTOCOMMIT", future=True)
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    print(f"created {name}")

    try:
        env = dict(os.environ, DATABASE_URL=preview_url)
        subprocess.run(
            ["alembic", "upgrade", "head"], check=True, env=env, stdout=subprocess.DEVNULL
        )
        _seed(source_url, preview_url)
        if args.bulk:
            _bulk(source_url, preview_url, args.bulk)

        proc = subprocess.Popen(
            [
                "uvicorn",
                "foilstack.web.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(args.port),
                "--log-level",
                "warning",
            ],
            env=dict(
                env,
                FOILSTACK_MULTI_USER="true",
                FOILSTACK_SECRET_KEY="preview-only-secret",
                PYTHONPATH="src",
            ),
        )
        url = f"http://127.0.0.1:{args.port}"
        _wait(url)
        print(f"serving {url}  ({EMAIL} / {PASSWORD})")

        if args.demo:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from demo import main as record

            card_id = _first_card(preview_url)
            record(
                [
                    "--url",
                    url,
                    "--out",
                    str(args.demo),
                    "--email",
                    EMAIL,
                    "--password",
                    PASSWORD,
                    *(["--card", str(card_id)] if card_id else []),
                    *(["--scale", str(args.scale)] if args.scale else []),
                    *(["--keep-frames"] if args.keep_frames else []),
                ]
            )
            proc.terminate()
        elif args.landing:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from landing import main as shoot_landing

            card_id = _first_card(preview_url)
            shoot_landing(
                [
                    "--url",
                    url,
                    "--out",
                    str(args.landing),
                    "--email",
                    EMAIL,
                    "--password",
                    PASSWORD,
                    *(["--card", str(card_id)] if card_id else []),
                    *(["--scale", str(args.scale)] if args.scale else []),
                ]
            )
            proc.terminate()
        elif args.shots:
            from shots import main as shoot

            sys.path.insert(0, str(Path(__file__).resolve().parent))
            card_id = _first_card(preview_url)
            shoot(
                [
                    "--url",
                    url,
                    "--out",
                    str(args.shots),
                    "--email",
                    EMAIL,
                    "--password",
                    PASSWORD,
                    "--width",
                    str(args.width),
                    "--height",
                    str(args.height),
                    *(["--only", args.only] if args.only else []),
                    *(["--card", str(card_id)] if card_id else []),
                ]
            )
            proc.terminate()
        else:
            proc.wait()
    finally:
        if not args.keep:
            with admin.connect() as conn:
                conn.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :n"
                    ),
                    {"n": name},
                )
                conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
            print(f"dropped {name}")
    return 0


def _seed(source_url: str, preview_url: str) -> None:
    """A user, a slice of the catalogue, some scans and some inventory."""
    import datetime as dt

    from foilstack import db
    from foilstack.config import get_settings
    from foilstack.importing import scan_path
    from foilstack.web import auth

    scans_dir = get_settings().scans_dir

    src = create_engine(source_url, future=True)
    with src.connect() as conn:
        # Real (scan, card) pairs from real matching, best score first — not a
        # scan and a card zipped together by position. A screenshot of this
        # screen argues that the top match is evidence; pairing an arbitrary
        # card with an arbitrary photograph and captioning it "99% match" makes
        # that argument dishonest, and it is obvious to anyone who plays.
        pairs = (
            conn.execute(
                text(
                    "SELECT s.id AS scan_id, s.stored_path, s.filename, s.auto_accepted,"
                    "       c.score, cd.id, cd.source, cd.source_id, cd.name, cd.source_name,"
                    "       cd.game,"
                    "       cd.set_name, cd.number, cd.variant, cd.image_url, cd.market,"
                    "       cd.currency"
                    "  FROM scans s"
                    "  JOIN candidates c ON c.scan_id = s.id AND c.rank = 0"
                    "  JOIN cards cd ON cd.id = c.card_id"
                    " WHERE cd.image_url IS NOT NULL AND cd.market IS NOT NULL"
                    "   AND s.size_bytes > 0"
                    " ORDER BY c.score DESC LIMIT 400"
                )
            )
            .mappings()
            .all()
        )

        # The photograph has to still be there. Discarding a scan gives the
        # bytes back — the image is deleted and `size_bytes` zeroed — while the
        # row and its candidates stay, because they are the record of why it
        # was thrown away. So the best-scoring pairs in a database that has
        # been used are exactly the ones most likely to have no picture behind
        # them, and the screen the hero shoots renders four broken thumbnails
        # under captions claiming a 94% match. The zero is the cheap half of
        # the check and the stat is the honest one: a data directory can be
        # shared, moved or restored without the column knowing.
        pairs = [r for r in pairs if scan_path(r["stored_path"], scans_dir)][:60]

        # The runners-up, from the same matching run.
        #
        # The preview used to seed one candidate per scan, which quietly made
        # the queue argue less than the product does: `also matched: ...` never
        # rendered, and the "wrong card?" panel opened on a heading that
        # promised a choice above a single option. The screen's whole claim is
        # that the top match is evidence rather than an assertion, and evidence
        # means the things it beat are visible.
        #
        # Real rows again, not invented ones. A fabricated runner-up captioned
        # with a percentage is the same dishonesty as a fabricated top match.
        alternates = (
            conn.execute(
                text(
                    "SELECT c.scan_id, c.score, c.rank,"
                    "       cd.id, cd.source, cd.source_id, cd.name, cd.source_name, cd.game,"
                    "       cd.set_name, cd.number, cd.variant, cd.image_url, cd.market,"
                    "       cd.currency"
                    "  FROM candidates c"
                    "  JOIN cards cd ON cd.id = c.card_id"
                    " WHERE c.scan_id = ANY(:ids) AND c.rank BETWEEN 1 AND 3"
                    "   AND cd.image_url IS NOT NULL"
                    " ORDER BY c.scan_id, c.rank"
                ),
                {"ids": [r["scan_id"] for r in pairs]},
            )
            .mappings()
            .all()
        )

    if not pairs:
        raise SystemExit(
            "the source database has no matched scans to build a preview from.\n"
            "Import an archive there first — a preview seeded with invented "
            "pairs would show matches that are not matches."
        )
    # Distinct cards first, then the duplicates.
    #
    # Ordering purely by score put the same card in the queue twice, because
    # two scans of one card both match it and both score highly. On screen that
    # reads as a bug rather than as a duplicate, and the queue is the first
    # thing anyone sees. So: a run of distinct cards to review, and the repeats
    # held back for the confirmed rows, where a duplicate is the point — it is
    # what the inventory screen consolidates.
    distinct: list = []
    repeats: list = []
    seen_card: set[int] = set()
    for row in pairs:
        if row["id"] in seen_card:
            repeats.append(row)
        else:
            seen_card.add(row["id"])
            distinct.append(row)

    # Enough waiting in the queue to fill the pane and to be worth scrolling.
    # Two was enough to prove the screen renders and far too few to show what
    # reviewing actually feels like.
    #
    # Seeded dearest first within the batch. The queue renders scans in the
    # order they were imported, which for a fixture is simply the order they
    # are created here — and the landing hero and the README animation are both
    # stills of the top of that list. Left in catalogue order they open on a
    # twenty-cent common. A seller photographing the good card first is an
    # ordinary way for a real batch to arrive, so this is a plausible pile
    # rather than a flattering one.
    pending = sorted(distinct[:PENDING], key=lambda r: float(r["market"] or 0), reverse=True)
    cards = pending + distinct[PENDING:] + repeats[:2]

    db.init(preview_url)
    session = db.session()
    user = db.User(email=EMAIL, password_hash=auth.hash_password(PASSWORD))
    session.add(user)
    session.commit()

    # Two scans can match the same printing — that is the duplicate case the
    # inventory screen exists to consolidate — so the catalogue rows behind
    # them have to be deduplicated before insert.
    kept_scans = {row["scan_id"] for row in cards}
    alts_by_scan: dict[int, list] = {}
    for alt in alternates:
        if alt["scan_id"] in kept_scans:
            alts_by_scan.setdefault(alt["scan_id"], []).append(alt)

    seen: set[int] = set()
    # The runners-up are catalogue rows like any other and have to exist before
    # a candidate can point at one.
    for row in list(cards) + [a for alts in alts_by_scan.values() for a in alts]:
        if row["id"] in seen:
            continue
        seen.add(row["id"])
        session.add(
            db.Card(
                **{
                    k: row[k]
                    for k in (
                        "id",
                        "source",
                        "source_id",
                        "name",
                        "source_name",
                        "game",
                        "set_name",
                        "number",
                        "variant",
                        "image_url",
                        "market",
                        "currency",
                    )
                }
            )
        )
    session.commit()

    # Two uploads, not one. The queue groups by the archive a scan arrived in,
    # and a preview seeded from a single job renders one section heading and
    # proves nothing about the screen — the same way seeding one candidate per
    # scan used to hide the runner-up row.
    older = db.ImportJob(
        user_id=user.id,
        filename="binder-a.zip",
        status="done",
        total=len(cards) - RECENT,
        processed=len(cards) - RECENT,
        created_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=2),
    )
    newer = db.ImportJob(
        user_id=user.id,
        filename="singles-box.zip",
        status="done",
        total=RECENT,
        processed=RECENT,
        created_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=20),
    )
    session.add_all([older, newer])
    session.commit()

    # Two scans left in the queue so the import screen has something to show,
    # the rest committed so inventory does — including a duplicate and a sale.
    for i, card in enumerate(cards):
        scan = db.Scan(
            # The tail of the waiting scans is the recent drop; everything
            # else — the rest of the queue and all the committed cards —
            # belongs to the older archive that is still being worked through.
            job_id=newer.id if PENDING - RECENT <= i < PENDING else older.id,
            user_id=user.id,
            filename=card["filename"],
            stored_path=card["stored_path"],
            status="pending" if i < PENDING else "confirmed",
            auto_accepted=card["auto_accepted"],
            best_score=float(card["score"]),
        )
        session.add(scan)
        session.commit()
        # The real score, so the number under the bar is the number the matcher
        # actually produced for these two images.
        session.add(
            db.Candidate(scan_id=scan.id, card_id=card["id"], score=float(card["score"]), rank=0)
        )
        for alt in alts_by_scan.get(card["scan_id"], []):
            # Skip a runner-up that is the top match again: two scans of one
            # printing is ordinary, and (scan, card) has to stay unique.
            if alt["id"] == card["id"]:
                continue
            session.add(
                db.Candidate(
                    scan_id=scan.id,
                    card_id=alt["id"],
                    score=float(alt["score"]),
                    rank=int(alt["rank"]),
                )
            )
        if i >= PENDING:
            session.add(
                db.InventoryItem(
                    user_id=user.id,
                    card_id=card["id"],
                    scan_id=scan.id,
                    condition="NM" if i % 3 else "LP",
                    finish="foil" if i % 4 == 0 else "nonfoil",
                    cost=round(float(card["market"]) * 0.4, 2),
                    status="sold" if i == 5 else "stock",
                    sold_price=round(float(card["market"]) * 0.9, 2) if i == 5 else None,
                    sold_at=(dt.datetime.now(dt.UTC) - dt.timedelta(days=9) if i == 5 else None),
                )
            )
    # A short run of price history on the first few cards, so the trend has a
    # shape to draw. The last card is left with a single reading on purpose —
    # that state has its own rendering and is worth being able to see.
    import random

    today = dt.date.today()

    # Price history goes on cards that are *in the inventory*, which is the only
    # place a chart can be reached from — `/inventory/<card>` is reached by
    # clicking a stock line. Attaching it to the first few cards overall put it
    # on the ones still sitting in the review queue, so every card page a
    # visitor could actually open drew an empty panel.
    #
    # Deduplicated too. Two scans matching one printing is the ordinary case —
    # it is the duplicate the inventory screen consolidates — and card_prices is
    # keyed on (card_id, sub_type), so the repeat raised a unique violation and
    # took the whole preview down with it.
    unique: list = []
    seen_price: set[int] = set()
    for card in cards[PENDING:]:
        if card["id"] in seen_price:
            continue
        seen_price.add(card["id"])
        unique.append(card)

    for n, card in enumerate(unique[:7]):
        base = float(card["market"] or 1.0)
        # The seventh gets exactly one reading — the state every card is in on
        # the day price recording starts, and the one that must not render as a
        # line through no information.
        backs = [0] if n == 6 else list(range(21, -1, -3))
        for back in backs:
            drift = 1 + (random.random() - 0.45) * 0.08 * (21 - back) / 21
            session.add(
                db.CardPriceHistory(
                    card_id=card["id"],
                    sub_type=card.get("variant") or "Normal",
                    recorded_on=today - dt.timedelta(days=back),
                    market=round(base * drift, 2),
                    low=round(base * drift * 0.88, 2),
                    mid=round(base * drift * 1.05, 2),
                    high=round(base * drift * 1.4, 2),
                )
            )
        session.add(
            db.CardPrice(
                card_id=card["id"],
                sub_type=card.get("variant") or "Normal",
                market=base,
                low=round(base * 0.88, 2),
                mid=round(base * 1.05, 2),
                high=round(base * 1.4, 2),
            )
        )
    session.commit()

    # The sync log those prices imply, which the plugins screen reads to say
    # whether each game is current and whether its history has been recovered.
    #
    # Seeded for the same reason the queue seeds runners-up: without these rows
    # every game reads "never · not run", so the two columns that exist to show
    # a healthy catalogue can only ever be looked at in their failure state.
    # Magic gets a backfill and Pokemon does not, because that is the real
    # shape of it — MTGJSON is the only enricher and it is a Magic project.
    #
    # Every game that got seeded, not a hardcoded pair. The footer names any
    # ingested game with no sync run behind it, so a fixture that seeds cards
    # from a third game and a sync log from two puts a red "no prices" badge
    # across the bottom of the landing hero — a broken install advertised on
    # the front page, and the install is not broken.
    now = dt.datetime.now(dt.UTC)
    ages = {"magic": (2, 486, 31), "pokemon": (5, 118, 9)}
    seeded_games = sorted(g for g in session.scalars(select(db.Card.game).distinct()) if g)
    for game in seeded_games:
        ago, printings, changed = ages.get(game, (3, 204, 17))
        session.add(
            db.SyncState(
                source="tcgcsv",
                kind=f"prices:{game}",
                upstream_stamp=(now - dt.timedelta(hours=ago)).isoformat(timespec="seconds"),
                last_run_at=now - dt.timedelta(hours=ago),
                rows_changed=changed,
                message=f"{printings} printings, {changed} changed",
            )
        )
    session.add(
        db.SyncState(
            source="mtgjson",
            kind="backfill:magic",
            upstream_stamp="5.2.2+20260826",
            last_run_at=now - dt.timedelta(days=6),
            rows_changed=1842,
            message="2,904 daily prices read, 1,842 recorded",
        )
    )
    session.commit()

    # One card with the ambiguity the picker exists for: three printings that
    # all answer to "foil", an order of magnitude apart.
    amb = unique[0]
    ambiguous = (
        ("1st Edition Holofoil", 30.0),
        ("Unlimited Holofoil", 6.4),
        ("Holofoil", 1.0),
    )
    for sub, mult in ambiguous:
        base = float(amb["market"] or 1.0) * mult
        session.merge(
            db.CardPrice(
                card_id=amb["id"],
                sub_type=sub,
                market=round(base, 2),
                low=round(base * 0.9, 2),
                mid=round(base * 1.05, 2),
                high=round(base * 1.3, 2),
            )
        )
        # History per printing, not just per card. The trend panel is keyed on
        # the printing a copy is declared as, so a card whose history was
        # written under "Normal" while its copies say "1st Edition Holofoil"
        # renders "no price history" — on the very card chosen to show the
        # feature off, because the ambiguous one is the interesting one.
        for back in range(21, -1, -3):
            drift = 1 + (random.random() - 0.45) * 0.09 * (21 - back) / 21
            session.merge(
                db.CardPriceHistory(
                    card_id=amb["id"],
                    sub_type=sub,
                    recorded_on=today - dt.timedelta(days=back),
                    market=round(base * drift, 2),
                    low=round(base * drift * 0.9, 2),
                    mid=round(base * drift * 1.05, 2),
                    high=round(base * drift * 1.3, 2),
                )
            )
    session.add(
        db.InventoryItem(
            user_id=user.id,
            card_id=amb["id"],
            condition="NM",
            finish="foil",
            cost=round(float(amb["market"] or 1.0) * 0.4, 2),
        )
    )
    session.commit()

    # A second copy of one card, so a quantity above 1 is visible.
    session.add(
        db.InventoryItem(
            user_id=user.id,
            card_id=cards[3]["id"],
            condition="NM",
            finish="nonfoil",
            cost=round(float(cards[3]["market"]) * 0.35, 2),
        )
    )
    session.commit()

    # A copy declared foil on a card the catalogue only prices as Normal.
    # Better than a third of the cards upstream have no foil printing at all,
    # so this is the ordinary case and not an edge — and it is invisible
    # without a seed, because it renders as a perfectly ordinary price with a
    # warning beside it rather than as a missing one.
    plain = next(
        c
        for c in unique[:7]
        if c["id"] != amb["id"] and "foil" not in (c.get("variant") or "Normal").lower()
    )
    session.add(
        db.InventoryItem(
            user_id=user.id,
            card_id=plain["id"],
            condition="NM",
            finish="foil",
            cost=round(float(plain["market"] or 1.0) * 0.4, 2),
        )
    )
    session.commit()

    # Some of it already on a marketplace, including exactly one copy of the
    # card that has two. Without this the listing screen photographs in one
    # state only — everything "ready", the button offering the whole run — and
    # the three states that screen actually has are the reason its counts were
    # wrong: listed, half listed, and nothing left to mark.
    stock = session.scalars(
        select(db.InventoryItem)
        .where(db.InventoryItem.user_id == user.id, db.InventoryItem.status == "stock")
        .order_by(db.InventoryItem.id)
    ).all()
    listed_at = dt.datetime.now(dt.UTC) - dt.timedelta(days=3)
    seen: set[tuple] = set()
    for item in stock[: len(stock) // 2]:
        # One copy of the duplicated line and not the other, so the "1 of 2
        # listed" pill has something to render.
        key = (item.card_id, item.condition, item.finish)
        if key in seen:
            continue
        seen.add(key)
        item.listed = 1
        item.listed_channels = "tcgplayer"
        item.listed_at = listed_at
    session.commit()

    # Prices on the cards still in the queue too. Every review row in the
    # preview rendered without one, because prices were only ever seeded on
    # cards that had already been confirmed — and the price beside a match is
    # the number a seller uses to decide whether a row is worth a second look.
    #
    # The dearest of them is left with a Normal printing only, so toggling
    # that row to Foil shows what the fallback does — which is now only
    # reachable by hand, and that is the point of keeping it here.
    #
    # The second is the mirror: Foil only, under an import that asked for
    # non-foil. That row has to render *as foil*, priced and unmarked, because
    # a default cannot answer for a card the catalogue prices on one side of
    # the line only. Better than a fifth of the cards upstream are like this,
    # so a preview without one hides the ordinary case.
    #
    # The rest get both, which is what a foil toggle looks like when the
    # catalogue can actually answer the question.
    for n, card in enumerate(cards[:PENDING]):
        base = float(card["market"] or 1.0)
        if n != 1:
            session.merge(
                db.CardPrice(
                    card_id=card["id"],
                    sub_type="Normal",
                    market=round(base, 2),
                    low=round(base * 0.88, 2),
                    mid=round(base * 1.05, 2),
                    high=round(base * 1.4, 2),
                )
            )
        if n:
            session.merge(
                db.CardPrice(
                    card_id=card["id"],
                    sub_type="Foil",
                    market=round(base * 3.2, 2),
                    low=round(base * 3.2 * 0.88, 2),
                    mid=round(base * 3.2 * 1.05, 2),
                    high=round(base * 3.2 * 1.4, 2),
                )
            )
    session.commit()
    session.close()


def _bulk(source_url: str, preview_url: str, target: int) -> None:
    """Pad inventory out to `target` rows, over a catalogue widened to match.

    The seeded preview holds a couple of dozen cards, which is the right size
    for a screenshot of the review queue and useless for looking at anything
    that only appears at scale. The inventory screen is paged now, and a pager
    cannot be looked at on one page of results — the whole class of bug it
    introduces (a filter that keeps the page number, a select-all that quietly
    means "this page", a row count that reports the window as the answer) is
    invisible until there is a second page.

    It widens the catalogue *first*, and that is the part worth keeping. The
    screen groups by card, so padding inventory alone against the seeded slice
    of 152 cards buys 152 lines however many rows are inserted — a hundred
    thousand cards would render as two pages of enormous quantities and prove
    nothing about paging. Roughly eight copies per card is what a shop's shelf
    looks like and what makes the line count move.

    Deliberately crude beyond that. The cards are real, so names, sets and
    prices are real and the facet chips have something true to count, but this
    is not trying to be a plausible inventory — it exists so a person can see
    the screen a shop sees. `--bulk 100000` takes a while and is the point.
    """
    import random

    from sqlalchemy import func, select

    from foilstack import db

    db.init(preview_url)
    session = db.session()
    try:
        user_id = session.scalar(select(db.User.id).order_by(db.User.id))
        held = session.scalar(
            select(func.count(db.InventoryItem.id)).where(db.InventoryItem.user_id == user_id)
        )
        wanted = target - (held or 0)
        if wanted <= 0:
            return

        have = set(session.scalars(select(db.Card.source_id)).all())
        _widen_catalogue(source_url, session, have, target // 8)

        # Sampled with replacement, so lines carry quantities — a screen where
        # every row reads "1" hides the consolidation this table exists to do.
        pool = session.scalars(select(db.Card.id)).all()
        if not pool:
            print("no catalogue to draw from; skipping --bulk")
            return

        rng = random.Random(20260904)
        conditions = ["NM", "NM", "NM", "LP", "MP", "HP"]
        rows = [
            {
                "user_id": user_id,
                "card_id": rng.choice(pool),
                "condition": rng.choice(conditions),
                "finish": "foil" if rng.random() < 0.22 else "nonfoil",
                "status": "sold" if rng.random() < 0.06 else "stock",
                "cost": round(rng.uniform(0.1, 40.0), 2),
                "listed": 1 if rng.random() < 0.3 else 0,
            }
            for _ in range(wanted)
        ]
        for start in range(0, len(rows), 5000):
            session.execute(db.InventoryItem.__table__.insert(), rows[start : start + 5000])
            session.commit()
        print(f"padded inventory to {target} rows over {len(pool)} cards")
    finally:
        session.close()


def _widen_catalogue(source_url: str, session, have: set[str], wanted: int) -> None:
    """Copy more real cards, and their prices, out of the source catalogue.

    Prices come across too rather than being invented. `cards.market` alone
    would render a market column and nothing else true: the printing label, the
    `?` on a guessed printing and the warning on a finish the catalogue does not
    price are all read off `card_prices`, so a preview without it shows a
    version of the screen that cannot go wrong in any of the ways it actually
    goes wrong.
    """
    if wanted <= len(have):
        return
    engine = create_engine(source_url, future=True)
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT id, source, source_id, name, source_name, game, set_name,"
                    "       number, variant, image_url, market, currency"
                    "  FROM cards"
                    " WHERE image_url IS NOT NULL AND market IS NOT NULL"
                    " ORDER BY id"
                    " LIMIT :n"
                ),
                {"n": wanted * 2},
            )
            .mappings()
            .all()
        )
        fresh = [r for r in rows if r["source_id"] not in have][:wanted]
        if not fresh:
            return
        prices = (
            conn.execute(
                text(
                    "SELECT card_id, sub_type, market, low, mid, high"
                    "  FROM card_prices WHERE card_id = ANY(:ids)"
                ),
                {"ids": [r["id"] for r in fresh]},
            )
            .mappings()
            .all()
        )

    from foilstack import db

    # Keyed by the source id, because the row ids in the two databases are
    # unrelated — the rule the whole codebase keeps about never carrying one
    # across an install applies just as much to a throwaway one.
    for start in range(0, len(fresh), 2000):
        session.execute(
            db.Card.__table__.insert(),
            [{k: v for k, v in r.items() if k != "id"} for r in fresh[start : start + 2000]],
        )
        session.commit()
    here = dict(
        session.execute(
            select(db.Card.source_id, db.Card.id).where(
                db.Card.source_id.in_([r["source_id"] for r in fresh])
            )
        ).all()
    )
    by_old = {r["id"]: here[r["source_id"]] for r in fresh}
    remapped = [{**p, "card_id": by_old[p["card_id"]]} for p in prices if p["card_id"] in by_old]
    for start in range(0, len(remapped), 2000):
        session.execute(db.CardPrice.__table__.insert(), remapped[start : start + 2000])
        session.commit()


def _first_card(preview_url: str) -> int | None:
    """A stock line worth opening: one with a price history behind it.

    The plain "first inventory row" this used to be was a coin toss, and both
    the demo and the landing stills exist to show a price trend — a card page
    reading "No price history for this card. Run foilstack sync-prices" is an
    illustration of the feature being absent. It got through once, into a
    screenshot placed directly under a paragraph about price history.

    Preferring the card with the most recorded price points also picks the most
    legible chart, since two points is a line segment rather than a trend.
    Falls back to the old behaviour when nothing has been synced at all.
    """
    from foilstack import db

    db.init(preview_url)
    session = db.session()
    try:
        with_history = session.scalar(
            select(db.InventoryItem.card_id)
            .join(db.CardPriceHistory, db.CardPriceHistory.card_id == db.InventoryItem.card_id)
            .group_by(db.InventoryItem.card_id)
            .order_by(func.count().desc())
            .limit(1)
        )
        return with_history or session.scalar(
            select(db.InventoryItem.card_id).order_by(db.InventoryItem.id)
        )
    finally:
        session.close()


def _wait(url: str, tries: int = 60) -> None:
    import httpx

    for _ in range(tries):
        try:
            if httpx.get(f"{url}/healthz", timeout=1.0).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"{url} never became ready")


if __name__ == "__main__":
    sys.exit(main())

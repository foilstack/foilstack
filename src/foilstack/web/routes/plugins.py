"""The plugins screen: what is installed, and what it has actually done.

Split out of app.py, which had it as one of its last screen routes. It stands
alone — no other route shares a path shape with `/plugins` — and it needs a
handful of catalogue queries that have no business in the application module.

The page used to be a manifest: three tables naming the source plugins, the
export mappings and the encoder URL. Everything on it was equally true of an
install that had never ingested a card, which made it a slower way of reading
`foilstack plugins`. The questions an operator actually arrives with are about
state — *is my catalogue current, can it be matched against, has the backfill
run* — and every one of those is answerable from `cards`, `card_embeddings`
and `sync_state`. So the state comes first and the manifest follows it.

`sync_state` is doing most of the work here, and it is the right table for it:
it is tiny, it records every run including the ones that correctly changed
nothing, and it carries upstream's own build stamp. Counting days in
`card_price_history` would answer the backfill question more directly and is
not affordable — that table is millions of rows on a real Magic catalogue, and
a page that takes two seconds to say "yes, it ran" is one nobody opens.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select

from foilstack import db
from foilstack.config import Settings
from foilstack.embedding import encoder_health
from foilstack.plugins import (
    EnrichmentPlugin,
    SourcePlugin,
    enrichment_plugins,
    export_plugins,
    source_plugins,
)
from foilstack.web.chrome import _ago, _chrome, templates
from foilstack.web.deps import db_session, owner, settings_dep

router = APIRouter()


def _game_labels() -> dict[str, str]:
    """Every game slug any installed plugin knows, in human words.

    `dragonballfusion` is a cache key and a CLI argument; "Dragon Ball Fusion
    World" is the name of a game. The plugin contract carries the translation
    precisely so a screen does not have to hardcode one, and the landing page
    has always used it — this page printed the raw slugs for thirteen games in
    a single table cell, which is both wrong to read and what made the table
    wide enough to push its own last column off the side of the viewport.

    Sources and enrichers both contribute, because a game may be enriched by a
    plugin whose source is not installed, and a slug with no label at all still
    has to render as something.
    """
    installed: list[SourcePlugin | EnrichmentPlugin] = [
        *source_plugins().values(),
        *enrichment_plugins().values(),
    ]
    out: dict[str, str] = {}
    for plugin in installed:
        labels = getattr(plugin, "labels", {}) or {}
        for game in plugin.games:
            out.setdefault(game, labels.get(game) or game.title())
    return out


def _sync_runs(session) -> dict[str, dict]:
    """Every recorded sync run, keyed by `kind` — `prices:magic`, `backfill:magic`.

    One query for a table with a handful of rows, rather than a lookup per
    game. `kind` is unique per source here only because one source syncs a
    given game; if two ever did, the later row would win and the column would
    quietly name the wrong plugin, so the source is carried through and shown.
    """
    return {
        kind: {
            "source": source,
            "at": at,
            "ago": _ago(at),
            "rows": rows or 0,
            "message": message or "",
            "stamp": stamp or "",
        }
        for source, kind, at, rows, message, stamp in session.execute(
            select(
                db.SyncState.source,
                db.SyncState.kind,
                db.SyncState.last_run_at,
                db.SyncState.rows_changed,
                db.SyncState.message,
                db.SyncState.upstream_stamp,
            )
        ).all()
    }


def _catalogue(session, model: str, labels: dict[str, str], runs: dict[str, dict]) -> list[dict]:
    """One row per game actually ingested, with everything that game's state is.

    Grouped by source as well as game. Two sources covering one game is
    allowed by the schema — `source_id` is namespaced exactly so they cannot
    collide — and collapsing them here would report one plugin's card count
    against the other's sync.

    `encoded` is the number that decides whether this game can be matched at
    all, and it is counted against the *configured* model rather than against
    every vector present. A model swap leaves the old vectors in place, and a
    count that included them would report a fully encoded catalogue that
    search, which filters by model, cannot see.
    """
    encoded = dict(
        session.execute(
            select(db.Card.game, func.count(db.CardEmbedding.card_id))
            .join(db.CardEmbedding, db.CardEmbedding.card_id == db.Card.id)
            .where(db.CardEmbedding.model == model)
            .group_by(db.Card.game)
        ).all()
    )
    enrichers = {
        game: plugin.name for plugin in enrichment_plugins().values() for game in plugin.games
    }

    rows = []
    for game, source, cards in session.execute(
        select(db.Card.game, db.Card.source, func.count(db.Card.id)).group_by(
            db.Card.game, db.Card.source
        )
    ).all():
        prices = runs.get(f"prices:{game}")
        backfill = runs.get(f"backfill:{game}")
        rows.append(
            {
                "game": game,
                "label": labels.get(game) or game.title(),
                "source": source,
                "cards": cards,
                "encoded": encoded.get(game, 0),
                "prices": prices,
                "backfill": backfill,
                # Distinguishes "no backfill has run" from "nothing could ever
                # back this game up". Only Magic has a source that publishes
                # its own past; printing "never" against Pokémon would read as
                # a job somebody forgot to run rather than as a fact about
                # what upstream holds.
                "enricher": enrichers.get(game),
            }
        )
    rows.sort(key=lambda r: (-r["cards"], r["label"]))
    return rows


@router.get("/plugins", response_class=HTMLResponse)
async def page_plugins(
    request: Request,
    session=Depends(db_session),
    user: db.User = Depends(owner),
    settings: Settings = Depends(settings_dep),
):
    health = await encoder_health(settings.embedder_url)
    labels = _game_labels()
    runs = _sync_runs(session)

    sources = [
        {
            "name": p.name,
            "games": [labels.get(g) or g.title() for g in p.games],
            # The command has to name a game this plugin can actually fetch.
            # It named `pokemon` for every source regardless, which is right
            # for the one source installed today and a copy-pasteable error
            # for the first one that does not do Pokémon.
            "command": f"foilstack ingest --source {p.name} --game {p.games[0]}",
        }
        for p in source_plugins().values()
    ]
    enrichers = [
        {
            "name": p.name,
            "games": [labels.get(g) or g.title() for g in p.games],
            "matches_source": p.matches_source,
            "command": f"foilstack enrich --source {p.name} --game {p.games[0]}",
        }
        for p in enrichment_plugins().values()
    ]

    chrome = _chrome(session, request, user, settings)
    return templates.TemplateResponse(
        request,
        "plugins.html",
        {
            "nav": "plugins",
            "sources": sources,
            "enrichers": enrichers,
            "exporters": export_plugins().values(),
            "catalogue": _catalogue(session, settings.embed_model, labels, runs),
            "encoder": health,
            "encoder_url": settings.embedder_url,
            "embed_model": settings.embed_model,
            # Everything the catalogue table cannot show per game: cards with
            # no vector cannot be found by any search, and the total is the
            # only place that shows up before a game has been ingested at all.
            "unencoded": max(chrome["catalog_cards"] - chrome["vector_count"], 0),
            **chrome,
        },
    )

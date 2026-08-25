"""The page shell: the template environment and the values every screen needs.

Split from app.py so a route module can render a page without importing the
application object, which would be a cycle. What lives here is the furniture
shared by every screen — the Jinja environment and its filters, the topbar
figures, and the small formatters those depend on — and nothing that answers a
request.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from foilstack import __version__, db, inventory, search
from foilstack.config import Settings

# `foilstack.web` has no __init__.py, so this cannot be shared through the
# package. It is defined here rather than in app.py because chrome is what
# needs it — the template directory and the static files it hashes — and app.py
# imports it back from here.
BASE_DIR = Path(__file__).parent

templates = Jinja2Templates(directory=BASE_DIR / "templates")

# The channels offered on the listing screen. Every one of them is a CSV you
# upload yourself: there is no API client behind any of these names, and the
# screen says so rather than implying a connection that does not exist.
CHANNELS = [
    {"key": "tcgplayer", "name": "TCGplayer", "note": "csv upload · no api key held"},
    {"key": "ebay", "name": "eBay", "note": "csv upload · no oauth held"},
]


def _money(n: float | None) -> str:
    if n is None:
        return "—"
    return ("-$" if n < 0 else "$") + f"{abs(n):,.2f}"


templates.env.filters["money"] = _money


def _asset_version() -> str:
    """A short hash of the static assets, appended to their URLs.

    Without it a CDN happily serves last week's CSS against today's HTML, and
    the page arrives with every new class name unstyled — which looks like
    broken CSS rather than a cache, and cost an hour to spot the first time.

    Every file that carries this query string has to be hashed into it. The
    stylesheet alone was enough until there was a script too: a JS change with
    no CSS change would have shipped a stale script behind an unchanged
    version, which is the same bug wearing a different hat. `app.js` joined
    the list the moment it existed, for the same reason — it holds the code
    the screens share, so it is the one script whose staleness breaks more
    than one page.
    """
    digest = hashlib.sha256()
    for name in (
        "app.css",
        "app.js",
        "zoom.js",
        "brand/mark.svg",
        "brand/favicon.svg",
        # The two raster icons were served with `?v=` from the beginning and
        # hashed into it by nobody, so a new favicon would have sat behind a
        # version that never moved. Found by the test, not by a person.
        "brand/favicon-32.png",
        "brand/apple-touch-icon.png",
    ):
        try:
            digest.update((BASE_DIR / "static" / name).read_bytes())
        except OSError:
            return "0"

    # The demo, by size and mtime rather than by content. It carries the same
    # query string, so it has to be in the hash — a CDN held a stale
    # `application/octet-stream` copy of it through a deploy that fixed exactly
    # that, because the URL never changed. Reading three megabytes on every
    # request to learn something `stat` already knows would be the wrong way to
    # fix it.
    for name in ("demo/foilstack.webp", "demo/foilstack-mobile.webp", "demo/foilstack.gif"):
        try:
            info = (BASE_DIR / "static" / name).stat()
        except OSError:
            continue
        digest.update(f"{name}:{info.st_size}:{info.st_mtime_ns}".encode())

    return digest.hexdigest()[:10]


def _aware(when: dt.datetime) -> dt.datetime:
    """Postgres hands these back tz-aware; a stray naive one must not crash a page."""
    return when if when.tzinfo else when.replace(tzinfo=dt.UTC)


def _ago(then: dt.datetime | None) -> str:
    if then is None:
        return "never"
    if then.tzinfo is None:
        then = then.replace(tzinfo=dt.UTC)
    seconds = (dt.datetime.now(dt.UTC) - then).total_seconds()
    if seconds < 90:
        return "just now"
    for unit, size in (("min", 60), ("hr", 3600), ("d", 86400)):
        if seconds < size * 60 or unit == "d":
            return f"{int(seconds // size)} {unit} ago"
    return "a while ago"


def _chrome(session, request: Request, user: db.User, settings: Settings) -> dict:
    """The values every page's shell needs, for this account only."""
    # One row is one card, and only cards still in stock count as "held".
    mine = (db.InventoryItem.user_id == user.id, db.InventoryItem.status == "stock")
    held = session.scalar(select(func.count(db.InventoryItem.id)).where(*mine)) or 0
    # Priced the same way the inventory screen prices it — per printing, with
    # any printing the seller named taking precedence. Summing `cards.market`
    # here was cheaper and wrong: that column is the plain printing's price, so
    # the topbar quoted one total while the table below it showed another.
    rows = inventory.items(session, user.id, status="stock")
    value = sum(r["market"] or 0 for r in rows)
    needs_printing = sum(1 for r in rows if r["printing_guessed"])
    # When the sync last *ran*, not when a price last moved.
    #
    # This read `max(cards.updated_at)`, which only advances when a number
    # actually changes — so a sync that ran ten minutes ago against an unchanged
    # upstream still reported "synced 10 hr ago", and the footer looked like a
    # stalled job. It sent someone to check whether the service was broken,
    # which is the opposite of what a status line is for. `sync_state` records
    # every run, including the ones that correctly did nothing.
    # The *oldest* run among games that have been ingested, not the newest.
    #
    # `max` was flattering to the point of being wrong: a deployment syncing
    # Magic every six hours and never syncing Dragon Ball at all reported
    # "synced 4 hr ago", because the Magic row was the newest one and nothing
    # asked whether every game had a row. The footer read as healthy while a
    # whole catalogue sat frozen at its ingest-day prices.
    #
    # `min` answers the question the line is actually making a claim about:
    # everything you can see a price for is at least this fresh.
    ingested = set(session.scalars(select(db.Card.game).distinct()).all())
    runs = {
        kind.split(":", 1)[-1]: at
        for kind, at in session.execute(
            select(db.SyncState.kind, db.SyncState.last_run_at).where(
                db.SyncState.kind.like("prices:%")
            )
        ).all()
    }
    unsynced = sorted(g for g in ingested if g not in runs)
    covered = [at for g, at in runs.items() if g in ingested and at is not None]
    synced = min(covered) if covered else session.scalar(select(func.max(db.Card.updated_at)))
    sources = session.scalars(select(db.Card.source).distinct()).all()

    return {
        "version": __version__,
        "git_sha": settings.git_sha,
        # Recomputed per request: the stylesheet is bind-mounted during
        # development, so caching this would defeat the point of having it.
        "asset_v": _asset_version(),
        "catalog_cards": session.scalar(select(func.count(db.Card.id))) or 0,
        "vector_count": search.count(session, settings.embed_model),
        "pending": session.scalar(
            select(func.count(db.Scan.id)).where(
                db.Scan.user_id == user.id, db.Scan.status == "pending"
            )
        )
        or 0,
        "inventory_rows": held,
        "inventory_count": held,
        "inventory_value": _money(value),
        "needs_printing": needs_printing,
        "listed_count": session.scalar(
            select(func.count(db.InventoryItem.id)).where(*mine, db.InventoryItem.listed == 1)
        )
        or 0,
        "host_label": request.url.netloc or "localhost",
        "user": user,
        "multi_user": settings.multi_user,
        "support_url": settings.support_url,
        "price_source": ", ".join(sources) if sources else "no catalogue",
        "synced_ago": _ago(synced),
        # Named in the footer rather than left to be discovered by noticing a
        # card's price never moves.
        "unsynced_games": unsynced,
    }

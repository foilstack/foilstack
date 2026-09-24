"""Price history, and the geometry to draw it.

The chart is an inline SVG built here rather than a charting library loaded in
the browser. The whole application is server-rendered with no build step, and a
sparkline is a polyline — importing 90KB of JavaScript to draw one would be the
largest dependency in the project by an order of magnitude.

`card_price_history` is a change log, so the points are irregular by design: a
card that has not moved in a fortnight has no rows in that fortnight. They are
connected with straight lines anyway, because the alternative — a step chart —
implies the price sat still and then jumped, and what actually happened is that
we did not look. A line between two known points is the honest shape for
"somewhere between these".
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from foilstack import db

# How far back the card page looks. Long enough to show a season, short enough
# that a card with two years of history does not render a hairline.
WINDOW_DAYS = 180


@dataclass(frozen=True)
class Point:
    on: dt.date
    value: float


def history(
    session, card_id: int, sub_types: list[str], days: int = WINDOW_DAYS
) -> dict[str, list[Point]]:
    """Recorded market prices per printing, oldest first."""
    if not sub_types:
        return {}
    cutoff = dt.date.today() - dt.timedelta(days=days)
    rows = session.scalars(
        select(db.CardPriceHistory)
        .where(
            db.CardPriceHistory.card_id == card_id,
            db.CardPriceHistory.sub_type.in_(sub_types),
            db.CardPriceHistory.recorded_on >= cutoff,
            db.CardPriceHistory.market.is_not(None),
        )
        .order_by(db.CardPriceHistory.recorded_on)
    ).all()

    out: dict[str, list[Point]] = {}
    for row in rows:
        out.setdefault(row.sub_type, []).append(Point(row.recorded_on, float(row.market)))
    return out


def _value_at_or_before(points: list[Point], when: dt.date) -> float | None:
    """The price in effect on a date.

    Not `== when`: this is a change log, so most dates have no row. The price
    that day is whatever was last recorded on or before it — reading the log as
    a daily snapshot is the mistake that makes it answer confidently and wrong.
    """
    seen = None
    for p in points:
        if p.on <= when:
            seen = p.value
        else:
            break
    return seen


def summarise(points: list[Point]) -> dict[str, Any]:
    """Latest value plus the moves worth quoting."""
    if not points:
        return {"latest": None, "points": 0}
    latest = points[-1].value
    today = dt.date.today()
    out: dict[str, Any] = {
        "latest": latest,
        "points": len(points),
        "first_on": points[0].on,
        "last_on": points[-1].on,
        "low": min(p.value for p in points),
        "high": max(p.value for p in points),
    }
    for label, days in (("d7", 7), ("d30", 30)):
        # Only quote a move we can actually see. With a week of history a
        # "30-day change" would be the change since the first reading wearing a
        # label that says otherwise.
        earliest = points[0].on
        then = _value_at_or_before(points, today - dt.timedelta(days=days))
        if then is None or earliest > today - dt.timedelta(days=days):
            out[label] = None
            continue
        out[label] = {
            "abs": round(latest - then, 2),
            "pct": round(100 * (latest - then) / then, 1) if then else None,
        }
    return out


def spark(points: list[Point], width: int = 320, height: int = 56, pad: int = 6) -> dict:
    """Coordinates for a sparkline. One point renders as one dot.

    Returns geometry only — no markup — so the template decides how it looks
    and this stays testable without parsing SVG.
    """
    if not points:
        return {"points": 0}

    inner_w = width - pad * 2
    inner_h = height - pad * 2
    values = [p.value for p in points]
    lo, hi = min(values), max(values)
    span = hi - lo

    def y_for(v: float) -> float:
        # A flat series has no span to scale against; centring it says "this
        # did not move", where scaling it would draw noise as a mountain range.
        if span == 0:
            return pad + inner_h / 2
        return pad + inner_h - ((v - lo) / span) * inner_h

    if len(points) == 1:
        cx, cy = pad + inner_w / 2, pad + inner_h / 2
        return {
            "points": 1,
            "width": width,
            "height": height,
            "dot": {"x": round(cx, 1), "y": round(cy, 1)},
            "path": "",
            "area": "",
            "lo": lo,
            "hi": hi,
        }

    first_day = points[0].on.toordinal()
    days = max(1, points[-1].on.toordinal() - first_day)
    coords = [
        (
            round(pad + inner_w * (p.on.toordinal() - first_day) / days, 1),
            round(y_for(p.value), 1),
        )
        for p in points
    ]
    path = "M " + " L ".join(f"{x} {y}" for x, y in coords)
    area = f"{path} L {coords[-1][0]} {height - pad} L {coords[0][0]} {height - pad} Z"
    return {
        "points": len(points),
        "width": width,
        "height": height,
        "path": path,
        "area": area,
        "dot": {"x": coords[-1][0], "y": coords[-1][1]},
        "lo": lo,
        "hi": hi,
    }


def last_synced(session) -> dict[str, dt.datetime | None]:
    """When each ingested game's prices last synced, `None` for never.

    Keyed by every game in the catalogue rather than every row in `sync_state`,
    because a game nobody ever synced has no row, and a question asked only of
    the rows present cannot notice one missing. That is how a footer reading
    the newest run once reported "synced 4 hr ago" over a whole catalogue
    frozen at its ingest-day prices.

    `last_run_at` moves only when a run *finishes*, including a run that
    correctly found upstream unchanged and did nothing — so it measures "is the
    sync alive", not "did a price move".
    """
    games = session.scalars(select(db.Card.game).distinct()).all()
    runs = {
        kind.split(":", 1)[-1]: at
        for kind, at in session.execute(
            select(db.SyncState.kind, db.SyncState.last_run_at).where(
                db.SyncState.kind.like("prices:%")
            )
        ).all()
    }
    return {game: runs.get(game) for game in sorted(games)}


def stale_after(check_every_s: int) -> dt.timedelta:
    """How long a game may go unsynced before that counts as broken.

    One missed check is forgiven — a blip upstream, a restart — and the hour is
    room for a full pull across every game to finish once a new build lands.
    Beyond that the loop is not running, and it matters before the day is out:
    upstream mirrors one day at a time, so a sync dead for a day has already
    cost a day of history that nothing can buy back.
    """
    return dt.timedelta(seconds=2 * check_every_s + 3600)


def stale(
    synced: dict[str, dt.datetime | None], now: dt.datetime, after: dt.timedelta
) -> list[str]:
    """The games whose prices have gone too long without a sync, or never had one."""
    late = []
    for game, at in synced.items():
        if at is not None and at.tzinfo is None:
            at = at.replace(tzinfo=dt.UTC)
        if at is None or now - at > after:
            late.append(game)
    return late

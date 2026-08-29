"""Writing a backfilled price series into history without damaging it.

`card_price_history` is the one table in this database that cannot be
reconstructed, so everything here is arranged around a single rule: **a
backfill may add days, and may never change one**. A day already recorded by
`sync-prices` came from the source we treat as authoritative and carries the
full low/mid/high spread; a backfilled day carries a market price alone. Losing
the first to insert the second would be a downgrade dressed as an improvement.

That rule is enforced twice, deliberately. Rows already present are excluded
before the insert is composed, and the insert itself ends `ON CONFLICT DO
NOTHING`. The first is what makes the run correct; the second is what makes it
*safe to run twice*, which matters far more than it looks — an operator who is
not sure whether the last attempt finished has to be able to just run it again.

The other rule is the table's own convention: a row exists only where the
number moved. Upstream publishes a daily snapshot, so ninety days of an
unchanged bulk common is one row here, not ninety. Applying that has to happen
across the seam between backfilled days and recorded ones, which means it
happens in SQL — Python does not know what is already there.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator
from typing import Any

from sqlalchemy import text

from foilstack.plugins.base import PriceHistoryRecord

log = logging.getLogger("foilstack")

# Landing tables for one run. Unlogged because their entire contents are
# derived from a file on disk: replaying them after a crash costs a re-parse,
# and paying WAL for several million throwaway rows on a self-hoster's disk to
# avoid that is the wrong trade.
STAGING = "_enrich_staging"
RESOLVED = "_enrich_resolved"

# One statement per entry, because psycopg refuses to prepare a script: a
# multi-statement string with bound parameters in it is a syntax error, and
# these carry the source and game.
_CREATE = (
    f"DROP TABLE IF EXISTS {STAGING}",
    f"""
    CREATE UNLOGGED TABLE {STAGING} (
        product_id  text NOT NULL,
        sub_type    text NOT NULL,
        recorded_on date NOT NULL,
        market      double precision
    )
    """,
)

# Which staged rows are actually ours to write.
#
# Three filters, and each drops something for its own reason. The join to
# `cards` drops printings we never ingested. The join to `card_prices` is the
# one that keeps this honest: it means a backfill can only name a printing the
# catalogue has already named for itself, so no sub-type is ever invented by a
# mapping table in a plugin — if upstream's idea of a printing does not match a
# row `sync-prices` wrote, we decline rather than guess. And the `NOT EXISTS`
# is the day-already-recorded rule, applied before the insert is even built.
_RESOLVE = (
    f"DROP TABLE IF EXISTS {RESOLVED}",
    f"""
CREATE UNLOGGED TABLE {RESOLVED} AS
SELECT c.id AS card_id, s.sub_type, s.recorded_on, s.market
  FROM {STAGING} s
  JOIN cards c
    ON c.source = :source
   AND c.game = :game
   AND c.source_id = :prefix || s.product_id
 WHERE EXISTS (
         SELECT 1 FROM card_prices p
          WHERE p.card_id = c.id AND p.sub_type = s.sub_type
       )
   AND NOT EXISTS (
         SELECT 1 FROM card_price_history h
          WHERE h.card_id = c.id
            AND h.sub_type = s.sub_type
            AND h.recorded_on = s.recorded_on
       )
    """,
    f"CREATE INDEX ON {RESOLVED} (card_id, sub_type, recorded_on)",
)

# The change-log rule, applied across both sets at once.
#
# `combined` is every day we are about to add plus every day already recorded
# for the same printings, and `lag` then asks each candidate whether it says
# anything the previous day did not. Reading the recorded days in is what makes
# the seam correct: the last backfilled day before your first real one is only
# worth keeping if it differs from what came before it, and the first real day
# is left alone either way because only `incoming` rows are selected.
_SELECT = f"""
WITH series AS (
    SELECT DISTINCT card_id, sub_type FROM {RESOLVED}
), combined AS (
    SELECT h.card_id, h.sub_type, h.recorded_on, h.market, false AS incoming
      FROM card_price_history h
      JOIN series s ON s.card_id = h.card_id AND s.sub_type = h.sub_type
    UNION ALL
    SELECT card_id, sub_type, recorded_on, market, true FROM {RESOLVED}
), marked AS (
    SELECT *, lag(market) OVER (
                 PARTITION BY card_id, sub_type ORDER BY recorded_on
             ) AS previous
      FROM combined
)
SELECT card_id, sub_type, recorded_on, market
  FROM marked
 WHERE incoming AND (previous IS NULL OR market IS DISTINCT FROM previous)
"""

_INSERT = f"""
INSERT INTO card_price_history (card_id, sub_type, recorded_on, market)
{_SELECT}
ON CONFLICT (card_id, sub_type, recorded_on) DO NOTHING
"""


async def stage(session: Any, records: AsyncIterator[PriceHistoryRecord]) -> int:
    """Land every record upstream offered, unfiltered, in one COPY.

    Unfiltered on purpose. Deciding what to keep needs the catalogue and the
    history beside it, and doing that per record in Python is several million
    round trips to answer a question one query answers once.
    """
    for statement in _CREATE:
        session.execute(text(statement))
    session.commit()

    connection = session.connection().connection
    raw = getattr(connection, "driver_connection", connection)
    staged = 0
    with (
        raw.cursor() as cursor,
        cursor.copy(
            f"COPY {STAGING} (product_id, sub_type, recorded_on, market) FROM STDIN"
        ) as copy,
    ):
        async for record in records:
            copy.write_row((record.source_id, record.sub_type, record.recorded_on, record.market))
            staged += 1
            if staged % 1_000_000 == 0:
                log.info("  staged %s daily prices", f"{staged:,}")
    session.commit()
    return staged


def apply(session: Any, source: str, game: str, dry_run: bool = False) -> dict[str, int]:
    """Resolve the staged rows against the catalogue and append what is new."""
    for statement in _RESOLVE:
        session.execute(
            text(statement),
            {"source": source, "game": game, "prefix": f"{source}:"},
        )
    session.commit()
    resolved = session.execute(text(f"SELECT count(*) FROM {RESOLVED}")).scalar_one()

    if dry_run:
        would = session.execute(text(f"SELECT count(*) FROM ({_SELECT}) AS q")).scalar_one()
        return {"resolved": resolved, "inserted": 0, "would_insert": would}

    inserted = session.execute(text(_INSERT)).rowcount
    session.commit()
    return {"resolved": resolved, "inserted": inserted, "would_insert": inserted}


def cleanup(session: Any) -> None:
    """Drop the landing tables.

    In a `finally`, because leaving several hundred megabytes of scratch in
    somebody's database after a failed run is its own small betrayal — and
    because the next run recreates them anyway, so keeping them buys nothing.
    """
    # Rolled back first, because the run this most needs to tidy up after is the
    # one that failed — and a failed statement leaves the transaction aborted,
    # so a bare DROP would be refused and the scratch would survive anyway.
    session.rollback()
    for table in (STAGING, RESOLVED):
        session.execute(text(f"DROP TABLE IF EXISTS {table}"))
    session.commit()


def summarise(stats: dict[str, int], staged: int, applied: dict[str, int]) -> Iterator[str]:
    """The run, in the order the numbers stop being obvious.

    Every line is a count of something dropped, because that is the half worth
    reading. A backfill that inserts fewer rows than expected looks exactly like
    one that worked, and this table is the one nobody can rebuild.
    """
    yield f"{stats.get('series', 0):,} printings, {staged:,} daily prices read"
    if stats.get("unmapped"):
        yield f"{stats['unmapped']:,} printings upstream could not match to a product id"
    if stats.get("ambiguous"):
        yield f"{stats['ambiguous']:,} skipped: one printing, several products, no safe choice"
    dropped = staged - applied["resolved"]
    yield f"{dropped:,} not ours to write: not ingested, unknown printing, or already recorded"
    yield f"{applied['would_insert']:,} days where the price actually moved"

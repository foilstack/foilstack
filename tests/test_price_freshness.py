"""When a quiet price sync counts as a dead one.

The rule behind `/healthz/prices`, without a database. Upstream mirrors one day
at a time, so the threshold has to forgive a blip and still fire with time left
before a day of history is lost.
"""

from __future__ import annotations

import datetime as dt

from foilstack import prices

NOW = dt.datetime(2026, 9, 23, 12, tzinfo=dt.UTC)
AFTER = prices.stale_after(21600)


def test_the_default_interval_alarms_well_inside_a_day():
    """One missed six-hour check is forgiven; a day never is."""
    assert dt.timedelta(hours=12) < AFTER < dt.timedelta(hours=24)


def test_a_game_never_synced_is_stale():
    """No `sync_state` row is the worst case, not the harmless one: those cards
    still hold their ingest-day prices and every day is history gone."""
    assert prices.stale({"pokemon": None}, NOW, AFTER) == ["pokemon"]


def test_only_the_games_past_the_threshold_are_named():
    synced = {
        "lorcana": NOW - AFTER + dt.timedelta(minutes=1),
        "magic": NOW - AFTER - dt.timedelta(minutes=1),
        "pokemon": NOW - dt.timedelta(hours=1),
    }
    assert prices.stale(synced, NOW, AFTER) == ["magic"]


def test_a_naive_timestamp_is_read_as_utc_rather_than_crashing():
    """Postgres hands these back aware; a stray naive one must not turn the
    health check itself into a 500."""
    naive = (NOW - dt.timedelta(hours=1)).replace(tzinfo=None)
    assert prices.stale({"magic": naive}, NOW, AFTER) == []

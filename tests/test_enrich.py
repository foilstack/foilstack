"""The price backfill, and the one property that matters: it cannot lose a day.

`card_price_history` is the only table here whose contents cannot be rebuilt,
so most of what follows is a test that something did *not* happen — a recorded
day was not overwritten, a printing the catalogue never named was not invented,
a second run did not double anything.
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from foilstack import enrich
from foilstack.plugins.base import PriceHistoryRecord
from foilstack.plugins.enrichers.mtgjson import _AMBIGUOUS, MTGJSONEnricher, _sku_finish

ADMIN_URL = os.getenv(
    "FOILSTACK_TEST_DATABASE_URL",
    "postgresql+psycopg://foilstack:foilstack@localhost:5434/foilstack",
)
DAY = dt.date(2026, 6, 1)


def day(offset: int) -> dt.date:
    return DAY + dt.timedelta(days=offset)


# --------------------------------------------------------------------------
# The upstream shapes, without a network or a 1.1 GB file.
# --------------------------------------------------------------------------


def test_etched_is_read_from_finish_not_printing():
    """An etched SKU says `printing: FOIL` and `finish: ETCHED`.

    Reading `printing` alone both loses every etched printing and makes plain
    foil look ambiguous, because two different products land in one bucket.
    """
    assert _sku_finish({"printing": "FOIL", "finish": "ETCHED"}) == "etched"
    assert _sku_finish({"printing": "FOIL"}) == "foil"
    assert _sku_finish({"printing": "NON FOIL"}) == "normal"
    assert _sku_finish({"printing": "SOMETHING NEW"}) is None


def write_gz(path, payload):
    with gzip.open(path, "wt", encoding="utf8") as fh:
        json.dump(payload, fh)
    return path


def test_one_printing_sold_as_two_products_is_kept_apart(tmp_path):
    """Foil and non-foil are usually one product; for some sets they are two.

    Keying the map per uuid instead of per finish files foil prices against the
    non-foil product, which is a real price on the wrong card.
    """
    skus = write_gz(
        tmp_path / "s.json.gz",
        {
            "data": {
                "u1": [
                    {"printing": "NON FOIL", "productId": 100},
                    {"printing": "FOIL", "productId": 200},
                    {"printing": "FOIL", "finish": "ETCHED", "productId": 300},
                ]
            }
        },
    )
    found = MTGJSONEnricher(cache_dir=tmp_path)._sku_map(skus)
    assert found["u1"] == {"normal": 100, "foil": 200, "etched": 300}


def test_a_printing_listed_twice_upstream_is_refused_rather_than_guessed(tmp_path):
    skus = write_gz(
        tmp_path / "s.json.gz",
        {
            "data": {
                "u1": [
                    {"printing": "NON FOIL", "productId": 100},
                    {"printing": "NON FOIL", "productId": 999},
                ]
            }
        },
    )
    found = MTGJSONEnricher(cache_dir=tmp_path)._sku_map(skus)
    assert found["u1"]["normal"] == _AMBIGUOUS


def test_only_tcgplayer_is_read_and_days_come_out_in_order(tmp_path):
    """Cardmarket prices are euros. Averaging them into a dollar column would
    be a fabricated number, and the provider list in the docs is already behind
    the data — so the provider is read by name, never enumerated."""
    prices = write_gz(
        tmp_path / "p.json.gz",
        {
            "data": {
                "u1": {
                    "paper": {
                        "tcgplayer": {
                            "retail": {"normal": {"2026-06-03": 3.0, "2026-06-01": 1.0}},
                            "currency": "USD",
                        },
                        "cardmarket": {
                            "retail": {"normal": {"2026-06-01": 99.0}},
                            "currency": "EUR",
                        },
                    }
                }
            }
        },
    )
    plugin = MTGJSONEnricher(cache_dir=tmp_path)
    got = list(plugin._read_prices(prices, {"u1": {"normal": 100}}))
    assert [(r.recorded_on, r.market) for r in got] == [(day(0), 1.0), (day(2), 3.0)]
    assert all(r.sub_type == "Normal" for r in got)
    assert 99.0 not in [r.market for r in got]


def test_unmappable_printings_are_counted_not_dropped_silently(tmp_path):
    prices = write_gz(
        tmp_path / "p.json.gz",
        {
            "data": {
                "u1": {"paper": {"tcgplayer": {"retail": {"foil": {"2026-06-01": 1.0}}}}},
                "u2": {"paper": {"tcgplayer": {"retail": {"normal": {"2026-06-01": 2.0}}}}},
            }
        },
    )
    plugin = MTGJSONEnricher(cache_dir=tmp_path)
    list(plugin._read_prices(prices, {"u2": {"normal": _AMBIGUOUS}}))
    assert plugin.stats["unmapped"] == 1
    assert plugin.stats["ambiguous"] == 1
    assert plugin.stats["series"] == 0


def test_it_refuses_a_game_it_has_no_data_for():
    """MTGJSON is a Magic project. A Pokémon backfill from it would be empty,
    and an empty run looks exactly like a working one."""
    with pytest.raises(ValueError, match="Magic project"):
        MTGJSONEnricher(game="pokemon")


def test_a_record_must_name_a_printing():
    with pytest.raises(ValueError):
        PriceHistoryRecord(source_id="1", sub_type="", recorded_on=DAY, market=1.0)


# --------------------------------------------------------------------------
# The writer, against a real database.
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def session():
    """A fresh database, migrated, dropped afterwards."""
    name = f"foilstack_enrich_{uuid.uuid4().hex[:8]}"
    try:
        engine = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT", future=True)
        with engine.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception as exc:  # noqa: BLE001 - no server, wrong password, anything
        pytest.skip(f"no Postgres for enrich tests: {type(exc).__name__}")

    url = ADMIN_URL.rsplit("/", 1)[0] + "/" + name
    os.environ["DATABASE_URL"] = url

    from foilstack.config import get_settings

    get_settings.cache_clear()

    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")

    from foilstack import db

    db.init(url)
    made = db.session()
    yield made
    made.close()

    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT", future=True)
    with admin.connect() as conn:
        conn.execute(
            text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :n"),
            {"n": name},
        )
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))


@pytest.fixture
def catalogue(session):
    """One card with two printings, and nothing else.

    Built per test rather than shared. Three tests in this suite's history once
    leaned on a fixture earlier tests edited, and passed alone while failing in
    sequence.
    """
    session.execute(text("TRUNCATE cards RESTART IDENTITY CASCADE"))
    session.commit()
    card_id = session.execute(
        text("""
        INSERT INTO cards (source, source_id, name, game, image_url, currency)
        VALUES ('tcgcsv', 'tcgcsv:100', 'Black Lotus', 'magic', 'http://x/y.jpg', 'USD')
        RETURNING id
    """)
    ).scalar_one()
    for sub in ("Normal", "Foil"):
        session.execute(
            text("INSERT INTO card_prices (card_id, sub_type, market) VALUES (:c, :s, 1.0)"),
            {"c": card_id, "s": sub},
        )
    session.commit()
    return card_id


async def run(session, records, dry_run=False):
    async def stream():
        for record in records:
            yield record

    try:
        staged = await enrich.stage(session, stream())
        return staged, enrich.apply(session, "tcgcsv", "magic", dry_run=dry_run)
    finally:
        enrich.cleanup(session)


def history(session, card_id, sub_type="Normal"):
    return session.execute(
        text("""
        SELECT recorded_on, market, low FROM card_price_history
         WHERE card_id = :c AND sub_type = :s ORDER BY recorded_on
    """),
        {"c": card_id, "s": sub_type},
    ).all()


def record(offset, market, sub_type="Normal", product="100"):
    return PriceHistoryRecord(
        source_id=product, sub_type=sub_type, recorded_on=day(offset), market=market
    )


async def test_a_flat_series_becomes_one_row_not_ninety(session, catalogue):
    """Upstream publishes a snapshot every day; this table is a change log."""
    await run(session, [record(i, 5.0) for i in range(10)])
    assert [(r.recorded_on, r.market) for r in history(session, catalogue)] == [(day(0), 5.0)]


async def test_it_records_the_day_a_price_moved(session, catalogue):
    await run(session, [record(0, 5.0), record(1, 5.0), record(2, 7.0), record(3, 7.0)])
    assert [(r.recorded_on, r.market) for r in history(session, catalogue)] == [
        (day(0), 5.0),
        (day(2), 7.0),
    ]


async def test_a_day_already_recorded_is_never_overwritten(session, catalogue):
    """The one rule everything else is arranged around.

    A day `sync-prices` wrote carries the full low/mid/high spread from the
    source we treat as authoritative. A backfill carries a market price alone,
    and its snapshot is taken hours from ours, so its number for that day is
    both different and worse. Losing the first to insert the second would be a
    downgrade dressed as an improvement.
    """
    session.execute(
        text("""
        INSERT INTO card_price_history (card_id, sub_type, recorded_on, market, low, mid, high)
        VALUES (:c, 'Normal', :d, 9.0, 8.0, 9.5, 12.0)
    """),
        {"c": catalogue, "d": day(5)},
    )
    session.commit()

    await run(session, [record(5, 999.0)])

    rows = {r.recorded_on: r for r in history(session, catalogue)}
    assert rows[day(5)].market == 9.0, "the recorded day was overwritten"
    assert rows[day(5)].low == 8.0, "the recorded day lost its spread"


async def test_the_seam_does_not_repeat_a_price_already_standing(session, catalogue):
    """A backfilled day worth keeping is one that says something new.

    The last backfilled day before the first recorded one is the case that only
    reads correctly if the writer can see both sets at once.
    """
    session.execute(
        text("""
        INSERT INTO card_price_history (card_id, sub_type, recorded_on, market)
        VALUES (:c, 'Normal', :d, 5.0)
    """),
        {"c": catalogue, "d": day(4)},
    )
    session.commit()

    await run(session, [record(0, 3.0), record(1, 5.0), record(2, 5.0), record(3, 5.0)])

    assert [(r.recorded_on, r.market) for r in history(session, catalogue)] == [
        (day(0), 3.0),
        (day(1), 5.0),
        (day(4), 5.0),
    ]


async def test_running_it_twice_changes_nothing(session, catalogue):
    """An operator unsure whether the last attempt finished has to be able to
    just run it again."""
    series = [record(0, 5.0), record(1, 7.0), record(2, 7.0), record(3, 9.0)]
    await run(session, series)
    first = history(session, catalogue)

    _, applied = await run(session, series)

    assert applied["inserted"] == 0
    assert history(session, catalogue) == first


async def test_it_will_not_invent_a_printing_the_catalogue_never_named(session, catalogue):
    """The sub-type has to already exist in `card_prices`.

    That filter is what keeps a mapping table inside a plugin from deciding
    what a printing is called. If upstream's idea of one does not match a row
    `sync-prices` wrote, we decline rather than guess.
    """
    await run(session, [record(0, 5.0, sub_type="Reverse Holofoil")])
    assert history(session, catalogue, "Reverse Holofoil") == []


async def test_a_card_that_was_never_ingested_is_skipped(session, catalogue):
    _, applied = await run(session, [record(0, 5.0, product="999999")])
    assert applied["inserted"] == 0


async def test_a_dry_run_writes_nothing_but_says_what_it_would(session, catalogue):
    staged, applied = await run(session, [record(0, 5.0), record(1, 9.0)], dry_run=True)
    assert staged == 2
    assert applied["would_insert"] == 2
    assert applied["inserted"] == 0
    assert history(session, catalogue) == []


async def test_the_scratch_tables_do_not_outlive_the_run(session, catalogue):
    """Several hundred megabytes of landing table left in somebody's database
    after a run is its own small betrayal."""
    await run(session, [record(0, 5.0)])
    for table in (enrich.STAGING, enrich.RESOLVED):
        present = session.execute(
            text("SELECT to_regclass(:t) IS NOT NULL"), {"t": table}
        ).scalar_one()
        assert not present, f"{table} was left behind"

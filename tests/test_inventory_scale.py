"""The aggregate and paginated paths must answer what `items()` answers.

`/inventory` no longer builds every row a seller owns to draw one screen, and
the topbar never did need to. Both now ask Postgres instead, through
`inventory.priced_printing` — which is a second expression of the rule
`resolve_printing` and `pick_printing` state in Python.

Two expressions of one rule is the whole risk of this change, and it is the
kind that fails quietly: a topbar disagreeing with the table under it by a few
dollars looks like a rounding choice, not a bug. So these drive both against
the same rows and demand the same numbers, over a catalogue built to contain
every shape the picker distinguishes — a card priced on both sides of the foil
line, one priced on only one side, one with several foil printings at
different money, and one the catalogue has no price for at all.

Needs Postgres: the picker is a lateral with window functions, so there is
nothing here that SQLite could answer.
"""

from __future__ import annotations

import math
import os
import uuid

import pytest
from sqlalchemy import create_engine, text

ADMIN_URL = os.getenv(
    "FOILSTACK_TEST_DATABASE_URL",
    "postgresql+psycopg://foilstack:foilstack@localhost:5434/foilstack",
)


def _admin_engine():
    return create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT", future=True)


@pytest.fixture(scope="module")
def priced_inventory():
    """A seller holding one copy of every awkward pricing shape there is."""
    name = f"foilstack_scale_{uuid.uuid4().hex[:8]}"
    try:
        engine = _admin_engine()
        with engine.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception as exc:  # noqa: BLE001 - no server, wrong password, anything
        pytest.skip(f"no Postgres for scale tests: {type(exc).__name__}")

    url = ADMIN_URL.rsplit("/", 1)[0] + "/" + name
    os.environ["DATABASE_URL"] = url

    # Before alembic, for the reason `test_isolation` says: `migrations/env.py`
    # reads `get_settings()`, and a cached settings object sends the migration
    # at the developer's own database.
    from foilstack.config import get_settings

    get_settings.cache_clear()

    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")

    from foilstack import db

    db.init(url)
    session = db.session()

    user = db.User(email="scale@example.com", password_hash="x")
    session.add(user)
    session.commit()

    # Each tuple is a card and the printings the catalogue prices it in. The
    # last has none at all, which is the case that must fall back to
    # `cards.market` rather than to nothing.
    catalogue = [
        ("Both Sides", 4.00, [("Normal", 4.00, 3.50), ("Foil", 19.00, 17.00)]),
        ("Foil Only", 8.00, [("Holofoil", 30.00, 28.00)]),
        ("Plain Only", 2.00, [("Normal", 2.00, 1.75)]),
        (
            "Three Foils",
            5.00,
            [
                ("Normal", 5.00, 4.50),
                ("Holofoil", 855.00, 800.00),
                ("1st Edition Holofoil", 10000.00, 9000.00),
                ("Unlimited Holofoil", 2146.00, 2000.00),
            ],
        ),
        ("Unpriced", 1.25, []),
        # A printing with no market price at all: `pick_printing` sorts it as
        # zero rather than dropping it, and `items()` then falls back to
        # `cards.market`. The two have to agree about which of those happens.
        ("Null Market", 6.00, [("Normal", None, None)]),
    ]

    cards = {}
    for i, (card_name, market, printings) in enumerate(catalogue, start=1):
        card = db.Card(
            source="t",
            source_id=f"t:{i}",
            name=card_name,
            game="mtg",
            set_name=f"Set {i % 2}",
            number=str(i),
            market=market,
        )
        session.add(card)
        session.flush()
        cards[card_name] = card.id
        for sub, sub_market, low in printings:
            session.add(db.CardPrice(card_id=card.id, sub_type=sub, market=sub_market, low=low))
    session.commit()

    # One inventory row per (card, finish, declared printing, condition, status)
    # combination worth distinguishing. `sub_type` is set on a few, including
    # one naming a printing the catalogue does not carry — which must fall back
    # to the guess rather than price at nothing.
    rows = [
        ("Both Sides", "nonfoil", None, "NM", "stock"),
        ("Both Sides", "foil", None, "LP", "stock"),
        ("Both Sides", "foil", "Normal", "NM", "stock"),
        ("Foil Only", "nonfoil", None, "NM", "stock"),
        ("Foil Only", "foil", None, "MP", "stock"),
        ("Plain Only", "foil", None, "NM", "stock"),
        ("Three Foils", "foil", None, "NM", "stock"),
        ("Three Foils", "foil", "Holofoil", "NM", "stock"),
        ("Three Foils", "foil", "Gone From Upstream", "NM", "stock"),
        ("Three Foils", "nonfoil", None, "HP", "stock"),
        ("Unpriced", "nonfoil", None, "NM", "stock"),
        ("Null Market", "nonfoil", None, "NM", "stock"),
        # Sold rows must not reach the topbar's figures at all.
        ("Three Foils", "foil", None, "NM", "sold"),
        ("Both Sides", "nonfoil", None, "NM", "sold"),
    ]
    for card_name, finish, sub_type, condition, status in rows:
        session.add(
            db.InventoryItem(
                user_id=user.id,
                card_id=cards[card_name],
                finish=finish,
                sub_type=sub_type,
                condition=condition,
                status=status,
                cost=1.0,
            )
        )
    session.commit()

    user_id = user.id
    session.close()
    yield url, user_id

    if db._engine is not None:
        db._engine.dispose()
    with _admin_engine().connect() as conn:
        conn.execute(
            text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :n"),
            {"n": name},
        )
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
    get_settings.cache_clear()


def test_sql_picks_the_printing_python_picks(priced_inventory):
    """The lateral and `resolve_printing` must name the same printing, per row.

    Compared row by row rather than in total, because two errors that cancel
    would pass a comparison of the sums — and the printing is what the price,
    the `?` flag and the TCGplayer condition column are all read off.
    """
    from sqlalchemy import select, true

    from foilstack import db, inventory

    _, user_id = priced_inventory
    with db.session() as session:
        priced = inventory.priced_printing()
        from_sql = {
            row[0]: (row[1], row[2])
            for row in session.execute(
                select(
                    db.InventoryItem.id,
                    priced.c.sub_type,
                    inventory.func.coalesce(priced.c.market, db.Card.market),
                )
                .select_from(db.InventoryItem)
                .join(db.Card, db.Card.id == db.InventoryItem.card_id)
                .outerjoin(priced, true())
                .where(db.InventoryItem.user_id == user_id)
            ).all()
        }
        from_python = {
            r["id"]: (r["sub_type"], r["market"]) for r in inventory.items(session, user_id)
        }

    assert from_sql == from_python


def test_index_agrees_with_items_on_every_shared_key(priced_inventory):
    """The thin read and the wide one must not disagree about anything.

    `index` exists to be cheaper, not to be different: the inventory screen
    reads it and the card page reads `items()`, and a seller moving between the
    two must not see a card change its price, its printing or its warning
    triangle on the way. So every key they share is compared, rather than the
    handful this screen happens to paint — the next key added to `items()`
    should be caught here if `index` was not given it too.
    """
    from foilstack import db, inventory

    _, user_id = priced_inventory
    with db.session() as session:
        wide = {r["id"]: r for r in inventory.items(session, user_id)}
        thin = {r["id"]: r for r in inventory.index(session, user_id)}

    assert set(thin) == set(wide)
    for item_id, row in thin.items():
        shared = set(row) & set(wide[item_id])
        assert {k: row[k] for k in shared} == {k: wide[item_id][k] for k in shared}, item_id

    # And `printings` is absent rather than empty — the two mean different
    # things, and a screen reading `[]` as "the catalogue prices nothing" off a
    # row that simply never fetched them would be wrong in silence.
    assert "printings" not in next(iter(thin.values()))


def test_position_matches_summing_items(priced_inventory):
    """The topbar's three figures, against the rows it replaced building."""
    from foilstack import db, inventory

    _, user_id = priced_inventory
    with db.session() as session:
        stock = inventory.items(session, user_id, status="stock")
        expected = {
            "count": len(stock),
            "market": sum(r["market"] or 0 for r in stock),
            "needs_printing": sum(1 for r in stock if r["printing_guessed"]),
        }
        got = inventory.position(session, user_id)

    assert got["count"] == expected["count"]
    assert got["needs_printing"] == expected["needs_printing"]
    assert round(got["market"], 2) == round(expected["market"], 2)
    # And the fixture has to be worth comparing: a `position` that agreed with
    # `items()` because both answered zero would pass everything above.
    assert expected["count"] > 0
    assert expected["market"] > 0
    assert expected["needs_printing"] > 0


def test_prices_for_survives_more_cards_than_postgres_can_bind(priced_inventory):
    """More distinct cards than one statement may name.

    Not a performance test. `IN (...)` renders one bind parameter per element
    and Postgres carries at most 65535 in a message, so this was a hard 500 —
    on every screen in the application, because the topbar went through the
    same fetch. The ids need not exist; what is being proved is that the
    statement is issued at all.
    """
    from foilstack import db, inventory

    _, _ = priced_inventory
    with db.session() as session:
        assert inventory._prices_for(session, set(range(1, 70_001))) is not None


def test_a_filter_selection_resolves_to_what_the_screen_showed(priced_inventory, monkeypatch):
    """Selecting by filter must pick the very rows the seller was looking at.

    This is the whole risk of the change. A selection is no longer a list of
    ids — one page of a hundred lines is around 740 copies and a 6.4 KB
    querystring, and "everything matching" would be a quarter of a megabyte —
    so the filter travels and `/listings` resolves it. If the two ever narrow
    differently, the seller lists cards they never saw and nothing on either
    screen says so.

    Driven over a page size of two, so the fixture's handful of cards produces
    real pages rather than one that happens to hold everything.
    """
    from foilstack import db, inventory
    from foilstack.web.deps import build_selection
    from foilstack.web.routes import inventory as inv_routes
    from foilstack.web.routes.listings import _resolve

    monkeypatch.setattr(inv_routes, "PAGE_SIZE", 2)
    _, user_id = priced_inventory

    with db.session() as session:
        copies = inventory.index(session, user_id, "market")
        for show in ("stock", "all"):
            for sort, direction in (("name", "asc"), ("market", "desc"), ("quantity", "desc")):
                found = inventory.narrow(copies, show=show, sort=sort, dir=direction)
                pages = max(1, math.ceil(len(found.rows) / 2))
                assert pages > 1, "the fixture must actually page for this to prove anything"

                for page in range(1, pages + 1):
                    window = found.rows[(page - 1) * 2 : page * 2]
                    expected = {i for line in window for i in line["ids"]}
                    got, described = _resolve(
                        session,
                        user_id,
                        "market",
                        build_selection(sel="page", show=show, sort=sort, dir=direction, page=page),
                    )
                    assert got == expected, (show, sort, direction, page)
                    assert f"{len(window):,} line" in described

                everything = {i for line in found.rows for i in line["ids"]}
                got, described = _resolve(
                    session,
                    user_id,
                    "market",
                    build_selection(sel="all", show=show, sort=sort, dir=direction),
                )
                assert got == everything, (show, sort, direction)
                assert described.startswith("all ")


def test_a_page_run_past_the_end_lists_the_last_page(priced_inventory, monkeypatch):
    """Clamped where the screen clamps, so a stale bookmark lists something.

    404ing instead would be the one case where a seller who paged too far can
    neither see nor list their own cards.
    """
    from foilstack import db, inventory
    from foilstack.web.deps import build_selection
    from foilstack.web.routes import inventory as inv_routes
    from foilstack.web.routes.listings import _resolve

    monkeypatch.setattr(inv_routes, "PAGE_SIZE", 2)
    _, user_id = priced_inventory

    with db.session() as session:
        copies = inventory.index(session, user_id, "market")
        rows = inventory.narrow(copies, show="stock").rows
        last = rows[(math.ceil(len(rows) / 2) - 1) * 2 :]
        got, _ = _resolve(session, user_id, "market", build_selection(sel="page", page=9999))

    assert got == {i for line in last for i in line["ids"]}


def test_hand_picked_ids_are_not_widened_by_a_filter_riding_along(priced_inventory):
    """The mode decides, not the presence of parameters.

    The inventory form submits the filter on every run, including a run of
    three ticked rows, because one form serves both. So a selection with `sel`
    empty has to mean the ids even with a full filter beside it — otherwise
    ticking three rows under a facet would list everything under that facet.
    """
    from foilstack import db
    from foilstack.web.deps import build_selection
    from foilstack.web.routes.listings import _resolve

    _, user_id = priced_inventory
    with db.session() as session:
        got, described = _resolve(
            session,
            user_id,
            "market",
            build_selection(ids=[3, 4], sel="", show="all", wire={"game": ["mtg"]}),
        )

    assert got == {3, 4}
    assert described == ""

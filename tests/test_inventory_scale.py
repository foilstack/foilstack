"""The aggregate and paginated paths must answer what `items()` answers.

`/inventory` no longer builds every row a seller owns to draw one screen, and
the topbar never did need to. Both ask Postgres instead, through
`inventory.priced_printing` — which is now the only expression of the rule.

It was one of two. `resolve_printing` and `pick_printing` said the same thing
in Python, and most of this module existed to drive the pair against the same
rows and demand the same numbers. The Python copy is gone, so what is left
here has two jobs instead: pin the picker's rules to the printings they must
name, over a catalogue built to contain every shape it distinguishes — a card
priced on both sides of the foil line, one priced on only one side, one with
several foil printings at different money, one catalogued but priced nowhere,
and one the catalogue has never heard a price for — and hold `position`, which
states the rule a third time as a grouped aggregate and genuinely can drift.

The picker's cases came here from `tests/test_inventory.py`, where they ran
against `pick_printing` without a database. That is the price of one
implementation: the rules can only be driven where they now live, and this
file skips without Postgres. `scripts/check-tests.sh` fails on a skip for
exactly that reason.

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
        # A printing with no market price at all: the picker sorts it as zero
        # rather than dropping it, so the row still names the printing it
        # holds and the price falls back to `cards.market`.
        ("Null Market", 6.00, [("Normal", None, None)]),
        # Catalogued on both sides of the foil line, priced on one. Around
        # 2,700 cards in a real catalogue look like this, and they are what
        # separates "has a printing" from "has a price": the foil row is a
        # real printing that nothing will pay for, so a foil copy of this card
        # is priced off the Normal and `finish_unpriced` says so — the
        # lateral's `has_foil` and its ORDER BY answering one question each.
        ("Foil Unpriced", 3.00, [("Normal", 3.00, 2.75), ("Holofoil", None, None)]),
        ("Plain Unpriced", 40.00, [("Normal", None, None), ("Holofoil", 40.00, 36.00)]),
        # Catalogued on both sides and priced on neither, which is the one
        # shape where no price can break the tie and the foil line has to
        # decide alone — while still naming a printing, so a card nobody will
        # pay for does not lose the sub-type it holds.
        ("Neither Priced", 7.00, [("Normal", None, None), ("Foil", None, None)]),
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
        # A seller who says foil on a card whose foil printing has no price.
        # The row prices off the Normal and carries the warning; the two reads
        # must agree about both, since one paints the pill and the other the
        # card page it links to.
        ("Foil Unpriced", "foil", None, "NM", "stock"),
        ("Foil Unpriced", "nonfoil", None, "LP", "stock"),
        # And the one that names the unpriced printing outright: a declared
        # sub_type is the seller speaking, so it still wins over a price.
        ("Foil Unpriced", "foil", "Holofoil", "NM", "stock"),
        ("Plain Unpriced", "nonfoil", None, "NM", "stock"),
        ("Neither Priced", "foil", None, "NM", "stock"),
        ("Neither Priced", "nonfoil", None, "NM", "stock"),
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


# Which printing the picker must name, and the rule each case is there for.
#
# These were pure-function tests against `pick_printing` until that function
# was deleted as the second copy of this rule. The cases were always the
# valuable part rather than the direct call, and the fixture above was already
# built to hold every shape they name — so they moved rather than went. What
# changed is that they now drive the expression that actually prices
# `/inventory`, `/listings` and the topbar, instead of one that agreed with it.
PICKER_CASES = [
    # (card, finish, declared printing, the printing it must be priced at)
    ("Both Sides", "nonfoil", None, "Normal"),
    ("Both Sides", "foil", None, "Foil"),
    # A single printing serves both answers rather than pricing at nothing.
    ("Plain Only", "foil", None, "Normal"),
    ("Foil Only", "nonfoil", None, "Holofoil"),
    ("Foil Only", "foil", None, "Holofoil"),
    # Several foils at different money is a choice the seller has not made, so
    # it guesses high: unsold is noticed, undersold is found out from a payout.
    ("Three Foils", "foil", None, "1st Edition Holofoil"),
    ("Three Foils", "nonfoil", None, "Normal"),
    # A printing the seller named wins outright — including one the catalogue
    # will not price, because that is still a person speaking.
    ("Three Foils", "foil", "Holofoil", "Holofoil"),
    ("Both Sides", "foil", "Normal", "Normal"),
    ("Foil Unpriced", "foil", "Holofoil", "Holofoil"),
    # ...unless upstream has dropped it, where it falls back to the guess
    # rather than pricing the card at nothing.
    ("Three Foils", "foil", "Gone From Upstream", "1st Edition Holofoil"),
    # A price outranks the seller's side of the foil line. Taking the unpriced
    # printing instead sent the row to `cards.market` while `finish_unpriced`
    # claimed it had been "priced off the other finish", which was not true.
    ("Foil Unpriced", "foil", None, "Normal"),
    ("Plain Unpriced", "nonfoil", None, "Holofoil"),
    ("Foil Unpriced", "nonfoil", None, "Normal"),
    # Only where something is priced. With nothing to prefer, the foil line
    # decides again and the row still names the printing it holds.
    ("Neither Priced", "foil", None, "Foil"),
    ("Neither Priced", "nonfoil", None, "Normal"),
    ("Null Market", "nonfoil", None, "Normal"),
    # And a card the catalogue has no printings for at all names none.
    ("Unpriced", "nonfoil", None, None),
]


@pytest.fixture(scope="module")
def picked(priced_inventory):
    """Every seeded row's printing, keyed by what the row asked for."""
    from sqlalchemy import select

    from foilstack import db, inventory

    _, user_id = priced_inventory
    with db.session() as session:
        rows = {r["id"]: r for r in inventory.index(session, user_id, inventory.Pricing())}
        out = {}
        for item, card in session.execute(
            select(db.InventoryItem, db.Card)
            .join(db.Card, db.Card.id == db.InventoryItem.card_id)
            .where(db.InventoryItem.user_id == user_id)
        ):
            out[(card.name, item.finish, item.sub_type)] = rows[item.id]["sub_type"]
    return out


@pytest.mark.parametrize("card, finish, declared, expected", PICKER_CASES)
def test_the_picker_names_the_printing_its_rules_demand(picked, card, finish, declared, expected):
    """One rule of `priced_printing`'s ORDER BY per case.

    Pinned to the printing rather than to the price, because the printing is
    what the price, the `?` flag, the finish warning and the TCGplayer
    `Condition` column are all read off — a wrong pick that happens to carry
    the right number is still a wrong row in an upload file.
    """
    assert picked[(card, finish, declared)] == expected


def test_every_seeded_row_is_covered_by_a_picker_case(picked):
    """A case list is only a guard while it still covers the fixture.

    The fixture is shared with the paging and selection tests, so a row added
    for one of those would otherwise join the catalogue with nothing asserting
    what it prices at — which is how a shape stops being tested without anyone
    removing a test.
    """
    covered = {(card, finish, declared) for card, finish, declared, _ in PICKER_CASES}
    assert set(picked) - covered == set()


def test_index_agrees_with_items_on_every_shared_key(priced_inventory):
    """The thin read and the wide one must not disagree about anything.

    `index` exists to be cheaper, not to be different: the inventory screen
    reads it and the card page reads `items`, and a seller moving between the
    two must not see a card change its price, its printing or its warning
    triangle on the way.

    Both are `_read` now, so the shared keys agree by construction and this no
    longer guards two implementations. What it still guards is the `detail`
    branch: sixteen keys are built on one path and not the other, and a rule
    written inside that branch — or a shared key computed from a column only
    the detailed read selects — would be a difference again.
    """
    from foilstack import db, inventory

    _, user_id = priced_inventory
    with db.session() as session:
        wide = {r["id"]: r for r in inventory.items(session, user_id, inventory.Pricing())}
        thin = {r["id"]: r for r in inventory.index(session, user_id, inventory.Pricing())}

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
        stock = inventory.items(session, user_id, inventory.Pricing(), status="stock")
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
        copies = inventory.index(session, user_id, inventory.Pricing("market"))
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
                        inventory.Pricing("market"),
                        build_selection(sel="page", show=show, sort=sort, dir=direction, page=page),
                    )
                    assert got == expected, (show, sort, direction, page)
                    assert f"{len(window):,} line" in described

                everything = {i for line in found.rows for i in line["ids"]}
                got, described = _resolve(
                    session,
                    user_id,
                    inventory.Pricing("market"),
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
        copies = inventory.index(session, user_id, inventory.Pricing("market"))
        rows = inventory.narrow(copies, show="stock").rows
        last = rows[(math.ceil(len(rows) / 2) - 1) * 2 :]
        got, _ = _resolve(
            session,
            user_id,
            inventory.Pricing("market"),
            build_selection(sel="page", page=9999),
        )

    assert got == {i for line in last for i in line["ids"]}


def test_hand_picked_ids_are_not_widened_by_a_filter_riding_along(priced_inventory):
    """The mode decides, not the presence of parameters.

    The inventory form submits the filter on every run, including a run of
    three ticked rows, because one form serves both. So a selection with `sel`
    empty has to mean the ids even with a full filter beside it — otherwise
    ticking three rows under a facet would list everything under that facet.
    """
    from foilstack import db, inventory
    from foilstack.web.deps import build_selection
    from foilstack.web.routes.listings import _resolve

    _, user_id = priced_inventory
    with db.session() as session:
        got, described = _resolve(
            session,
            user_id,
            inventory.Pricing("market"),
            build_selection(ids=[3, 4], sel="", show="all", wire={"game": ["mtg"]}),
        )

    assert got == {3, 4}
    assert described == ""


def test_a_sku_finds_its_own_card(priced_inventory):
    """The search box has always offered to take a SKU and never read one.

    A SKU is what a marketplace hands back when something sells, so pasting
    one in to find the card is the search this screen most owes a seller — and
    what it answered was "nothing matches", which reads as inventory that is
    not there rather than as a box that was not looking.
    """
    from foilstack import db, inventory

    _, user_id = priced_inventory
    with db.session() as session:
        copies = inventory.index(session, user_id, inventory.Pricing())
        wanted = copies[0]
        found = inventory.narrow(copies, show="all", q=inventory.sku(wanted["id"]))

    assert [line["card_id"] for line in found.rows] == [wanted["card_id"]]
    assert wanted["id"] in found.rows[0]["ids"]
    # And the search still narrows: a fixture where every card matched would
    # pass this whatever the needle did.
    assert found.total_lines > 1


def test_scoped_items_match_the_unscoped_read(priced_inventory):
    """Narrowing in the query must answer what narrowing afterwards answered.

    `export_rows` used to fold `items()` over an account's whole stock and then
    skip the rows it had not selected, which made a twelve-row listing run cost
    what listing everything costs. The ids go into the statement now, and the
    only thing that may change is the price of asking — so every row is
    compared whole against what the old shape would have produced.
    """
    from foilstack import db, inventory

    _, user_id = priced_inventory
    with db.session() as session:
        every = inventory.items(session, user_id, inventory.Pricing())
        assert len(every) > 2, "a subset of one row proves nothing about scoping"

        wanted = {every[0]["id"], every[-1]["id"]}
        scoped = inventory.items(session, user_id, inventory.Pricing(), ids=wanted)

        assert scoped == [r for r in every if r["id"] in wanted]
        # Order is part of the answer: `items()` is read by callers that fold
        # it in sequence, and a chunked fetch orders each statement rather than
        # the concatenation of them.
        assert [r["id"] for r in scoped] == [r["id"] for r in every if r["id"] in wanted]

        # An empty selection is not a missing one. A run that resolved to no
        # rows must not price the whole account, which is the failure
        # `Selection` exists to keep off `/listings`.
        assert inventory.items(session, user_id, inventory.Pricing(), ids=set()) == []
        assert inventory.items(session, user_id, inventory.Pricing(), ids=None) == every

        # And an id belonging to nobody here is simply absent, rather than
        # widening the read or raising.
        assert inventory.items(session, user_id, inventory.Pricing(), ids={-1}) == []


def test_scoped_items_survive_more_ids_than_one_statement_binds(priced_inventory):
    """The real ceiling, driven rather than simulated.

    Postgres binds at most 65535 parameters in one message and `IN (...)`
    spends one per element, so a `sel=all` run over a large account was the
    case that broke — and it broke with an `OperationalError` naming the wire
    protocol. `db.id_in` sends an array instead, which has no such limit.

    The ids do the scaling here, not the rows: what has to exceed 65535 is the
    selection, so a handful of real rows padded with ids belonging to nobody
    drives the limit exactly as a large account would, in one query rather
    than the minutes it would take to insert them. Padding with *absent* ids
    also pins the other half — a selection must narrow to what the account
    owns, never widen.
    """
    from foilstack import db, inventory

    _, user_id = priced_inventory
    with db.session() as session:
        every = inventory.items(session, user_id, inventory.Pricing())
        wanted = {r["id"] for r in every}
        assert len(wanted) > 2

        assert inventory.items(session, user_id, inventory.Pricing(), ids=wanted) == every

        padded = wanted | {-i for i in range(1, 70_001)}
        assert len(padded) > 65_535
        assert inventory.items(session, user_id, inventory.Pricing(), ids=padded) == every


def test_id_in_replaces_a_limit_that_in_still_has(priced_inventory):
    """The seam itself, and the cliff it exists to remove.

    Every id list in this codebase goes through `db.id_in` now, so this is the
    one place the protocol limit has to be pinned. It asserts the old spelling
    still fails, deliberately: `col.in_(ids)` is the obvious thing to write and
    reads as correct, so what stops it coming back is a test that names what
    happens when it does.
    """
    import pytest
    import sqlalchemy as sa
    from sqlalchemy.exc import OperationalError

    from foilstack import db

    _, user_id = priced_inventory
    with db.session() as session:
        mine = sorted(session.scalars(sa.select(db.InventoryItem.id)).all())
        assert len(mine) > 2

        # Same answer as `in_` where `in_` still works.
        assert sorted(
            session.scalars(
                sa.select(db.InventoryItem.id).where(db.id_in(db.InventoryItem.id, mine))
            )
        ) == sorted(
            session.scalars(sa.select(db.InventoryItem.id).where(db.InventoryItem.id.in_(mine)))
        )

        # And an answer at all where `in_` has none. 65535 is the whole message,
        # so the list is taken well past it rather than to the boundary.
        huge = mine + [-i for i in range(1, 100_001)]
        assert (
            sorted(
                session.scalars(
                    sa.select(db.InventoryItem.id).where(db.id_in(db.InventoryItem.id, huge))
                )
            )
            == mine
        )
        with pytest.raises(OperationalError):
            session.scalars(
                sa.select(db.InventoryItem.id).where(db.InventoryItem.id.in_(huge))
            ).all()
        session.rollback()

        # Two in one statement must not collide. The bind parameter is
        # anonymous for this reason, and a named one would silently make the
        # second filter overwrite the first.
        both = session.scalars(
            sa.select(db.InventoryItem.id).where(
                db.id_in(db.InventoryItem.id, mine),
                db.id_in(db.InventoryItem.user_id, [user_id]),
            )
        ).all()
        assert sorted(both) == mine


def test_export_rows_scopes_to_the_selection(priced_inventory):
    """The caller that motivated all of it, end to end."""
    from foilstack import db, inventory

    _, user_id = priced_inventory
    with db.session() as session:
        pricing = inventory.Pricing()
        every = inventory.export_rows(session, user_id, pricing)
        assert len(every) > 1

        wanted = set(every[0]["ids"])
        scoped = inventory.export_rows(session, user_id, pricing, ids=wanted)

        assert scoped == [every[0]]
        # A line is regrouped from its copies, so scoping to some of them has
        # to restate the line rather than hand back the whole one: a quantity
        # inherited from the unscoped read would export cards the seller did
        # not select.
        assert sum(r["quantity"] for r in scoped) == len(wanted)

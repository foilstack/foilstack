"""One account must never see another's cards.

This is the test that justifies the whole account system, so it exercises the
real application against a real Postgres rather than asserting on query text.
It builds its own throwaway database and skips cleanly when there is no server
to build it in, so `uv run pytest` stays green on a laptop with no Docker.

Every route that touches a seller's work is covered. A new one that forgets to
scope itself will pass every other test in this suite and fail here.
"""

from __future__ import annotations

import os
import re
import uuid

import pytest
from sqlalchemy import create_engine, select, text

ADMIN_URL = os.getenv(
    "FOILSTACK_TEST_DATABASE_URL",
    "postgresql+psycopg://foilstack:foilstack@localhost:5434/foilstack",
)


def _admin_engine():
    return create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT", future=True)


@pytest.fixture(scope="module")
def database_url():
    """A fresh database, migrated, dropped afterwards."""
    name = f"foilstack_test_{uuid.uuid4().hex[:8]}"
    try:
        engine = _admin_engine()
        with engine.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception as exc:  # noqa: BLE001 - no server, wrong password, anything
        pytest.skip(f"no Postgres for isolation tests: {type(exc).__name__}")

    url = ADMIN_URL.rsplit("/", 1)[0] + "/" + name
    os.environ["DATABASE_URL"] = url

    # Before alembic runs, not after. `migrations/env.py` takes its URL from
    # `get_settings()` regardless of what is set on the Config here, so a
    # settings object cached by any module imported earlier in the run sends
    # this migration at the developer's own database instead of the throwaway
    # one — and the failure names a password, which points nowhere near the
    # cause.
    from foilstack.config import get_settings

    get_settings.cache_clear()

    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    yield url

    with _admin_engine().connect() as conn:
        conn.execute(
            text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :n"),
            {"n": name},
        )
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))


@pytest.fixture(scope="module")
def app_and_data(database_url, tmp_path_factory):
    """A running app in multi-user mode, with one card and one seller's scan."""
    data_dir = tmp_path_factory.mktemp("data")
    (data_dir / "scans" / "1").mkdir(parents=True)
    (data_dir / "scans" / "1" / "card.jpg").write_bytes(b"not really a jpeg")

    os.environ.update(
        FOILSTACK_DATA_DIR=str(data_dir),
        FOILSTACK_MULTI_USER="true",
        FOILSTACK_SECRET_KEY="isolation-test-secret",
    )
    from foilstack.config import get_settings

    get_settings.cache_clear()

    from foilstack import db
    from foilstack.web import app as web
    from foilstack.web import auth

    app = web.app

    # Nothing to rebind any more: routes take settings through `settings_dep`,
    # which calls the cached `get_settings()` per request, so the cache_clear
    # above is enough. This used to need `web.settings = get_settings()`,
    # because a module global bound at import kept whichever database the first
    # importing module saw — and every test here then failed on a password
    # error that named nothing to do with test ordering. That cost hours twice.

    db.init(database_url)
    session = db.session()

    owner = db.User(
        email="owner@example.com",
        password_hash=auth.hash_password("owner-long-password"),
    )
    session.add(owner)
    session.commit()

    card = db.Card(source="t", source_id="t:1", name="Test Card", game="mtg", market=10.0)
    session.add(card)
    session.commit()

    job = db.ImportJob(user_id=owner.id, filename="a.zip", status="done", total=1, processed=1)
    session.add(job)
    session.commit()

    scan = db.Scan(
        job_id=job.id,
        user_id=owner.id,
        filename="card.jpg",
        stored_path="1/card.jpg",
        status="pending",
    )
    session.add(scan)
    session.commit()
    session.add(db.Candidate(scan_id=scan.id, card_id=card.id, score=0.99, rank=0))
    item = db.InventoryItem(user_id=owner.id, card_id=card.id, condition="NM")
    session.add(item)
    session.commit()

    ids = {"job": job.id, "scan": scan.id, "item": item.id, "card": card.id}
    session.close()
    yield app, ids
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def stranger(app_and_data):
    """A second account that owns nothing."""
    from fastapi.testclient import TestClient

    app, _ = app_and_data
    client = TestClient(app)
    client.post(
        "/register",
        data={"email": "stranger@example.com", "password": "stranger-long-password"},
    )
    return client


def test_anonymous_is_sent_to_the_login_screen(app_and_data):
    from fastapi.testclient import TestClient

    app, _ = app_and_data
    with TestClient(app) as anon:
        assert anon.get("/app", follow_redirects=False).status_code == 303


def test_anonymous_api_gets_401_not_a_redirect(app_and_data):
    """A fetch() that follows a redirect to the login page succeeds with an
    HTML body, and the caller reports success for a request that did nothing."""
    from fastapi.testclient import TestClient

    app, ids = app_and_data
    with TestClient(app) as anon:
        assert anon.post(f"/api/scans/{ids['scan']}/discard").status_code == 401


def test_stranger_sees_no_inventory(stranger):
    assert stranger.get("/inventory").text.count("<tr data-row") == 0


def test_stranger_sees_no_queue(stranger):
    assert 'class="qrow' not in stranger.get("/app").text


def test_stranger_cannot_read_a_scan_image(stranger, app_and_data):
    """Photographs of another person's property."""
    app, ids = app_and_data
    assert stranger.get(f"/scan/{ids['scan']}/image").status_code == 404

    # And the 404 above means "not yours", not "no such route". Without this
    # line the test passes just as happily against a build where the image
    # route was dropped altogether — which nearly happened when these routes
    # moved into their own module.
    assert _signed_in(app).get(f"/scan/{ids['scan']}/image").status_code == 200


def test_stranger_cannot_read_a_job(stranger, app_and_data):
    _, ids = app_and_data
    assert stranger.get(f"/api/jobs/{ids['job']}").status_code == 404


def test_stranger_cannot_discard_a_scan(stranger, app_and_data):
    _, ids = app_and_data
    assert stranger.post(f"/api/scans/{ids['scan']}/discard").status_code == 404


def test_stranger_cannot_confirm_a_scan(stranger, app_and_data):
    """The most damaging one: confirming writes a row into inventory, so an
    unscoped version would let a stranger put cards into someone's shop."""
    _, ids = app_and_data
    r = stranger.post(
        f"/api/scans/{ids['scan']}/confirm",
        data={"card_id": ids["card"], "condition": "NM", "quantity": 1},
    )
    assert r.status_code == 404


def test_stranger_cannot_mark_someone_elses_rows_listed(stranger, app_and_data):
    _, ids = app_and_data
    r = stranger.post(
        "/api/listings/mark",
        json={"ids": [ids["item"]], "channels": ["tcgplayer"]},
    )
    assert r.json()["marked"] == 0


def test_stranger_cannot_open_the_match_panel(stranger, app_and_data):
    """The panel names the scan's candidates, which is a statement about what
    someone else photographed."""
    _, ids = app_and_data
    assert stranger.get(f"/api/scans/{ids['scan']}/match-panel").status_code == 404


def test_stranger_cannot_repoint_someone_elses_row(stranger, app_and_data):
    """Correcting a match is an edit, and edits stop at the account boundary.

    Worth its own test because `card_id` was added to an endpoint that already
    existed and already had a scoping check — the kind of change that is
    covered by accident until the day it is not.
    """
    _, ids = app_and_data
    r = stranger.post(f"/api/inventory/{ids['item']}", json={"card_id": ids["card"]})
    assert r.status_code == 404


def test_stranger_cannot_search_the_catalogue_anonymously(app_and_data):
    """The catalogue is public reference data, so this is not about secrecy:
    an unauthenticated substring search is a free table scan for anyone who
    finds the URL."""
    from fastapi.testclient import TestClient

    app, _ = app_and_data
    anon = TestClient(app, follow_redirects=False)
    assert anon.get("/api/cards/search?q=test").status_code in (302, 303, 307, 401)


def test_the_owner_can_open_the_match_panel_and_search(app_and_data):
    """Renders the two fragments nothing else in the suite reaches.

    The stranger tests above only prove these endpoints return 404 to the
    wrong account, which a template with a syntax error does too.
    """
    from fastapi.testclient import TestClient

    app, ids = app_and_data
    client = TestClient(app)
    client.post("/login", data={"email": "owner@example.com", "password": "owner-long-password"})

    panel = client.get(f"/api/scans/{ids['scan']}/match-panel")
    assert panel.status_code == 200
    assert "Search the catalogue" in panel.text
    # Seeded from the top match, so the search opens one edit from the answer
    # rather than empty.
    assert 'value="Test Card"' in panel.text

    hit = client.get("/api/cards/search?q=Test")
    assert hit.status_code == 200
    assert f'data-pick="{ids["card"]}"' in hit.text

    miss = client.get("/api/cards/search?q=nothingcalledthis")
    assert "no card by that name" in miss.text


def test_bulk_delete_is_not_shadowed_by_the_single_item_route(app_and_data):
    """`/api/inventory/delete` and `/api/inventory/{item_id}` are the same
    shape, and FastAPI matches in declaration order.

    Declared the other way round, a POST to `delete` is captured by the
    `{item_id}` route, `"delete"` fails to parse as an integer, and bulk delete
    answers 422 — a routing bug that reads as a validation error. The other
    bulk-delete tests would catch it, but none of them says that is what they
    are for, so moving these routes between files looks safe right up until it
    is not.
    """
    from fastapi.testclient import TestClient

    app, _ = app_and_data
    client = TestClient(app)
    client.post("/login", data={"email": "owner@example.com", "password": "owner-long-password"})

    # Empty list: reaches the handler, which rejects it on its own terms. A 422
    # here would mean the path never got there at all.
    r = client.post("/api/inventory/delete", json={"ids": []})
    assert r.status_code != 422, "the {item_id} route is shadowing bulk delete"
    assert r.status_code == 400


def test_a_game_with_no_price_sync_is_named_in_the_status_bar(app_and_data):
    """The bug that made "synced 4 hr ago" a lie.

    `max(last_run_at)` reported the freshest game and asked nothing about the
    rest, so a catalogue that had never been price-synced once was invisible
    behind a healthy-looking footer. The claim the line makes is about all of
    the prices on screen, so the figure has to be the oldest, and a game with
    no sync at all has to be said out loud.
    """
    from fastapi.testclient import TestClient

    from foilstack import db

    app, _ = app_and_data
    session = db.session()
    session.add(
        db.Card(source="t", source_id="t:unpriced", name="Unpriced", game="neverpriced", market=1.0)
    )
    session.commit()
    session.close()

    client = TestClient(app)
    client.post("/login", data={"email": "owner@example.com", "password": "owner-long-password"})
    body = client.get("/inventory").text
    assert "neverpriced" in body
    assert "no prices:" in body

    session = db.session()
    session.delete(session.scalars(select(db.Card).where(db.Card.game == "neverpriced")).one())
    session.commit()
    session.close()


def test_a_chosen_card_survives_a_reload(app_and_data):
    """The bug this column exists to fix.

    Picking a card used to update the browser and nothing else, so the row
    looked corrected until the page was reloaded and the encoder's guess came
    back. A choice that does not outlive a refresh is not a choice.
    """
    from fastapi.testclient import TestClient

    from foilstack import db

    app, ids = app_and_data
    session = db.session()
    other = db.Card(source="t", source_id="t:chosen", name="Chosen Card", game="mtg", market=7.0)
    session.add(other)
    session.commit()
    other_id = other.id
    session.close()

    client = TestClient(app)
    client.post("/login", data={"email": "owner@example.com", "password": "owner-long-password"})

    r = client.post(f"/api/scans/{ids['scan']}/choose", data={"card_id": other_id})
    assert r.status_code == 200

    # Read back through the page the seller actually reloads, not the column.
    queue = client.get("/app?filter=review").text
    assert "Chosen Card" in queue
    assert f'data-scan="{ids["scan"]}" data-card="{other_id}"' in queue

    # And it is a choice, not a commitment: nothing entered inventory.
    session = db.session()
    scan = session.get(db.Scan, ids["scan"])
    assert scan.chosen_card_id == other_id
    assert scan.status == "pending"
    session.close()

    # Put it back, so later tests in this module see the fixture they expect.
    client.post(f"/api/scans/{ids['scan']}/choose", data={"card_id": ids["card"]})
    session = db.session()
    session.get(db.Scan, ids["scan"]).chosen_card_id = None
    session.commit()
    session.close()


def test_stranger_cannot_choose_a_card_for_someone_elses_scan(stranger, app_and_data):
    _, ids = app_and_data
    r = stranger.post(f"/api/scans/{ids['scan']}/choose", data={"card_id": ids["card"]})
    assert r.status_code == 404


def test_a_row_can_be_corrected_to_another_card(app_and_data):
    """The point of the feature: a row pointing at the wrong card is fixable
    without deleting it, which would take the scan with it.

    On its own account and its own row rather than the shared fixture's. The
    other tests in this module read that row and count that account's job-log
    entries, so mutating either from here fails two unrelated tests several
    hundred lines away.
    """
    from fastapi.testclient import TestClient

    from foilstack import db
    from foilstack.web import auth

    app, _ = app_and_data
    session = db.session()
    user = db.User(
        email="fixer@example.com",
        password_hash=auth.hash_password("fixer-long-password"),
    )
    wrong = db.Card(source="t", source_id="t:wrong", name="Wrong Card", game="mtg", market=1.0)
    right = db.Card(source="t", source_id="t:right", name="Right Card", game="mtg", market=4.0)
    session.add_all([user, wrong, right])
    session.commit()
    # A printing chosen on the card being replaced. It must not survive the
    # correction: it would price the new card by a name that may not exist in
    # its price rows, and look deliberate while doing it.
    item = db.InventoryItem(user_id=user.id, card_id=wrong.id, condition="NM", sub_type="Holofoil")
    session.add(item)
    session.commit()
    item_id, right_id = item.id, right.id
    session.close()

    client = TestClient(app)
    client.post("/login", data={"email": "fixer@example.com", "password": "fixer-long-password"})
    assert client.post(f"/api/inventory/{item_id}", json={"card_id": right_id}).status_code == 200

    session = db.session()
    fixed = session.get(db.InventoryItem, item_id)
    assert fixed.card_id == right_id
    assert fixed.sub_type is None
    session.close()


def test_correcting_to_a_card_that_does_not_exist_is_refused(app_and_data):
    from fastapi.testclient import TestClient

    app, ids = app_and_data
    client = TestClient(app)
    client.post(
        "/login",
        data={"email": "owner@example.com", "password": "owner-long-password"},
    )
    r = client.post(f"/api/inventory/{ids['item']}", json={"card_id": 10_000_000})
    assert r.status_code == 400


def test_stranger_exports_an_empty_csv(stranger):
    """Header only. An unscoped export is a one-click dump of everyone's
    inventory, and it looks like a working feature."""
    assert len(stranger.get("/export/tcgplayer").text.splitlines()) == 1


def test_owner_still_sees_their_own(app_and_data):
    from fastapi.testclient import TestClient

    app, ids = app_and_data
    client = TestClient(app)
    client.post(
        "/login",
        data={"email": "owner@example.com", "password": "owner-long-password"},
    )
    assert client.get("/inventory").text.count("<tr data-row") == 1
    assert client.get(f"/scan/{ids['scan']}/image").status_code == 200
    assert client.get(f"/api/jobs/{ids['job']}").status_code == 200


def test_wrong_password_is_refused(app_and_data):
    from fastapi.testclient import TestClient

    app, _ = app_and_data
    client = TestClient(app)
    r = client.post(
        "/login",
        data={"email": "owner@example.com", "password": "not-the-password"},
    )
    assert r.status_code == 400
    assert client.get("/app", follow_redirects=False).status_code == 303


def test_stranger_cannot_open_someone_elses_card_panel(stranger, app_and_data):
    _, ids = app_and_data
    assert stranger.get(f"/api/inventory/{ids['item']}").status_code == 404


def test_stranger_cannot_edit_someone_elses_card(stranger, app_and_data):
    _, ids = app_and_data
    r = stranger.post(f"/api/inventory/{ids['item']}", json={"condition": "DMG"})
    assert r.status_code == 404


def test_stranger_cannot_delete_someone_elses_card(stranger, app_and_data):
    """The most destructive route in the application."""
    _, ids = app_and_data
    assert stranger.delete(f"/api/inventory/{ids['item']}").status_code == 404


def test_owner_can_edit_mark_sold_and_the_row_leaves_stock(app_and_data):
    from fastapi.testclient import TestClient

    app, ids = app_and_data
    client = TestClient(app)
    client.post("/login", data={"email": "owner@example.com", "password": "owner-long-password"})

    ok = client.post(
        f"/api/inventory/{ids['item']}",
        json={
            "condition": "LP",
            "finish": "foil",
            "cost": "4.50",
            "notes": "edge wear",
        },
    )
    assert ok.status_code == 200
    panel = client.get(f"/api/inventory/{ids['item']}").text
    assert "edge wear" in panel

    # Sold rows must not reach a marketplace export.
    client.post(f"/api/inventory/{ids['item']}", json={"status": "sold", "sold_price": "12.00"})
    assert len(client.get("/export/tcgplayer").text.splitlines()) == 1
    assert client.get("/inventory?show=stock").text.count("<tr data-row") == 0
    assert client.get("/inventory?show=sold").text.count("<tr data-row") == 1

    # And returning it to stock clears the sale rather than leaving it counted.
    client.post(f"/api/inventory/{ids['item']}", json={"status": "stock"})
    assert client.get("/inventory?show=stock").text.count("<tr data-row") == 1


def test_committing_clears_the_queue_and_fills_inventory(app_and_data):
    """The import screen is for deciding, not for keeping a record.

    Before this, a committed scan stayed in the queue forever: you pressed
    Commit and the page looked exactly as it had, with no end state.
    """
    from fastapi.testclient import TestClient

    app, ids = app_and_data
    client = TestClient(app)
    client.post("/login", data={"email": "owner@example.com", "password": "owner-long-password"})

    queue_before = client.get("/app").text.count('class="qrow')
    assert queue_before == 1, "fixture scan should be waiting"

    stock_before = client.get("/inventory?show=stock").text.count("<tr data-row")
    r = client.post(
        "/api/scans/commit",
        json={
            "rows": [
                {
                    "scan_id": ids["scan"],
                    "card_id": ids["card"],
                    "condition": "NM",
                    "finish": "foil",
                }
            ]
        },
    )
    assert r.status_code == 200

    assert client.get("/app").text.count('class="qrow') == 0
    # The fixture's card already had a copy, so committing a second one adds
    # to that row rather than creating another.
    assert client.get("/inventory?show=stock").text.count("<tr data-row") == stock_before
    # And the finish chosen at commit time survives into inventory.
    assert "Foil" in client.get("/inventory?show=stock").text


def test_commit_rejects_an_unknown_finish(app_and_data):
    from fastapi.testclient import TestClient

    app, ids = app_and_data
    client = TestClient(app)
    client.post("/login", data={"email": "owner@example.com", "password": "owner-long-password"})
    r = client.post(
        "/api/scans/commit",
        json={
            "rows": [
                {
                    "scan_id": ids["scan"],
                    "card_id": ids["card"],
                    "condition": "NM",
                    "finish": "prismatic",
                }
            ]
        },
    )
    assert r.status_code == 400


def _fresh_line(source_id: str, name: str, conditions: list[str]) -> dict:
    """A card of its own with one copy per condition given.

    Self-contained on purpose. These tests used to lean on the shared fixture
    row, which earlier tests in this module edit — so they passed alone and
    failed in sequence, which is the least useful way for a test to fail.
    """
    from sqlalchemy import select as sa_select

    from foilstack import db

    session = db.session()
    owner = session.scalars(sa_select(db.User).where(db.User.email == "owner@example.com")).one()
    card = db.Card(source="t", source_id=source_id, name=name, game="mtg", market=10.0)
    session.add(card)
    session.commit()
    for condition in conditions:
        session.add(
            db.InventoryItem(
                user_id=owner.id,
                card_id=card.id,
                condition=condition,
                finish="nonfoil",
            )
        )
    session.commit()
    out = {"card_id": card.id}
    session.close()
    return out


def _signed_in(app):
    from fastapi.testclient import TestClient

    client = TestClient(app)
    client.post("/login", data={"email": "owner@example.com", "password": "owner-long-password"})
    return client


def test_identical_cards_consolidate_into_one_stock_line(app_and_data):
    """Two scans of the same card in the same condition are one line, qty 2.

    This is the bug that prompted the model change: the inventory list showed
    the same card twice, quantity 1 each, with two different scan thumbnails
    and nothing to say they were the same thing.
    """
    app, _ = app_and_data
    line = _fresh_line("t:dup", "Consolidate Me", ["NM", "NM"])
    client = _signed_in(app)

    page = client.get("/inventory?show=stock&q=Consolidate").text
    assert page.count("<tr data-row") == 1, "two copies must be one line"
    assert ">2<" in page, "the line must show a quantity of 2"

    card = client.get(f"/inventory/{line['card_id']}")
    assert card.status_code == 200
    assert card.text.count("data-open=") == 2, "both copies listed individually"


def test_mixed_conditions_are_one_inventory_row_but_two_export_lines(app_and_data):
    """The two groupings answer different questions and both must hold.

    On screen, "how many of this card do I own" is 2. In a marketplace upload,
    an NM copy and an LP copy are separate listings, because condition sets the
    price — collapsing them would sell a played card at a near-mint price.
    """
    app, _ = app_and_data
    _fresh_line("t:split", "Split Me", ["NM", "LP"])
    client = _signed_in(app)

    page = client.get("/inventory?show=stock&q=Split Me").text
    assert page.count("<tr data-row") == 1
    assert "1 NM, 1 LP" in page or "1 LP, 1 NM" in page

    rows = [r for r in client.get("/export/tcgplayer").text.splitlines() if "Split Me" in r]
    assert len(rows) == 2, f"condition must split the export: {rows}"


def test_export_emits_one_line_per_stock_line_with_a_real_quantity(app_and_data):
    """Two copies used to become two marketplace listings of quantity 1."""
    app, _ = app_and_data
    _fresh_line("t:export", "Export Me", ["NM", "NM", "NM"])
    client = _signed_in(app)

    rows = [r for r in client.get("/export/tcgplayer").text.splitlines() if "Export Me" in r]
    assert len(rows) == 1, f"three copies must be one line, got {rows}"
    assert ",3," in rows[0], f"quantity must be 3: {rows[0]}"


def test_stranger_cannot_open_someone_elses_card_page(stranger, app_and_data):
    _, ids = app_and_data
    assert stranger.get(f"/inventory/{ids['card']}").status_code == 404


def test_naming_a_printing_prices_the_card_at_it(app_and_data):
    """The whole point of the picker.

    Base Set Charizard is "1st Edition Holofoil" at $10,000 and "Holofoil" at
    $855. Ticking "foil" chooses between neither, so pricing guesses the dearer
    one — and a seller holding the $855 card lists it at ten thousand dollars.
    """
    from sqlalchemy import select as sa_select

    from foilstack import db

    app, _ = app_and_data
    session = db.session()
    owner = session.scalars(sa_select(db.User).where(db.User.email == "owner@example.com")).one()
    card = db.Card(source="t", source_id="t:zard", name="Pricey", game="mtg", market=855.0)
    session.add(card)
    session.commit()
    for sub, market in (("1st Edition Holofoil", 10000.0), ("Holofoil", 855.0)):
        session.add(db.CardPrice(card_id=card.id, sub_type=sub, market=market, low=market * 0.9))
    item = db.InventoryItem(user_id=owner.id, card_id=card.id, condition="NM", finish="foil")
    session.add(item)
    session.commit()
    card_id, item_id = card.id, item.id
    session.close()

    client = _signed_in(app)

    # Guessed: the dearest matching printing, and the page says so.
    page = client.get(f"/inventory/{card_id}").text
    assert "1st Edition Holofoil" in page
    assert "guessed" in page

    # Named: priced at the printing actually held, and no longer flagged.
    ok = client.post(f"/api/inventory/{item_id}", json={"sub_type": "Holofoil"})
    assert ok.status_code == 200
    page = client.get(f"/inventory/{card_id}").text
    assert "guessed" not in page

    from foilstack import inventory

    session = db.session()
    row = next(r for r in inventory.items(session, owner.id) if r["id"] == item_id)
    assert row["sub_type"] == "Holofoil"
    assert row["market"] == 855.0
    assert row["printing_declared"] is True
    session.close()


def test_a_printing_the_card_does_not_have_is_refused(app_and_data):
    """Free text here would let a typo price the card off the guess forever,
    looking for all the world like a deliberate choice."""
    from sqlalchemy import select as sa_select

    from foilstack import db

    app, _ = app_and_data
    session = db.session()
    owner = session.scalars(sa_select(db.User).where(db.User.email == "owner@example.com")).one()
    item = session.scalars(
        sa_select(db.InventoryItem).where(db.InventoryItem.user_id == owner.id)
    ).first()
    item_id = item.id
    session.close()

    client = _signed_in(app)
    r = client.post(f"/api/inventory/{item_id}", json={"sub_type": "Holgraphic Foyle"})
    assert r.status_code == 400


def test_committing_reports_how_many_need_a_printing(app_and_data):
    """A card priced on a guess between printings looks exactly like a card
    priced on a decision. The commit response says how many, so the UI can send
    the seller straight at them."""
    from sqlalchemy import select as sa_select

    from foilstack import db

    app, _ = app_and_data
    session = db.session()
    owner = session.scalars(sa_select(db.User).where(db.User.email == "owner@example.com")).one()
    card = db.Card(source="t", source_id="t:ambig", name="Ambiguous", game="mtg", market=5.0)
    session.add(card)
    session.commit()
    for sub, market in (("1st Edition Holofoil", 900.0), ("Holofoil", 5.0)):
        session.add(db.CardPrice(card_id=card.id, sub_type=sub, market=market))
    job = db.ImportJob(user_id=owner.id, filename="b.zip", status="done", total=1, processed=1)
    session.add(job)
    session.commit()
    scan = db.Scan(
        job_id=job.id,
        user_id=owner.id,
        filename="a.jpg",
        stored_path="1/card.jpg",
        status="pending",
    )
    session.add(scan)
    session.commit()
    scan_id, card_id = scan.id, card.id
    session.close()

    client = _signed_in(app)
    r = client.post(
        "/api/scans/commit",
        json={
            "rows": [
                {
                    "scan_id": scan_id,
                    "card_id": card_id,
                    "condition": "NM",
                    "finish": "foil",
                }
            ]
        },
    )
    assert r.status_code == 200
    assert r.json()["needs_printing"] >= 1

    # And the filter gathers them in one place.
    page = client.get("/inventory?show=printing").text
    assert "Ambiguous" in page


def test_the_topbar_total_agrees_with_the_inventory_table(app_and_data):
    """These were computed two different ways — a sum of `cards.market` in the
    chrome and per-printing prices in the table — so a foil made the header and
    the page below it quote different totals."""
    import re

    from sqlalchemy import select as sa_select

    from foilstack import db, inventory

    app, _ = app_and_data
    client = _signed_in(app)
    page = client.get("/inventory").text

    session = db.session()
    owner = session.scalars(sa_select(db.User).where(db.User.email == "owner@example.com")).one()
    rows = inventory.items(session, owner.id, status="stock")
    expected = sum(r["market"] or 0 for r in rows)
    session.close()

    shown = re.search(r"([\d,]+\.\d\d) at market", page)
    assert shown, page[:400]
    assert float(shown.group(1).replace(",", "")) == round(expected, 2)


def _stock_item(email: str, name: str, source_id: str, sold: bool = False) -> tuple[int, int]:
    """One card with one copy, optionally already sold."""
    import datetime as dt

    from sqlalchemy import select as sa_select

    from foilstack import db

    session = db.session()
    owner = session.scalars(sa_select(db.User).where(db.User.email == email)).one()
    card = db.Card(source="t", source_id=source_id, name=name, game="mtg", market=3.0)
    session.add(card)
    session.commit()
    item = db.InventoryItem(
        user_id=owner.id,
        card_id=card.id,
        condition="NM",
        finish="nonfoil",
        status="sold" if sold else "stock",
        sold_price=2.0 if sold else None,
        sold_at=dt.datetime.now(dt.UTC) if sold else None,
    )
    session.add(item)
    session.commit()
    out = (item.id, card.id)
    session.close()
    return out


def test_bulk_delete_removes_the_selected_rows(app_and_data):
    app, _ = app_and_data
    a, _ = _stock_item("owner@example.com", "Bulk One", "t:bulk1")
    b, _ = _stock_item("owner@example.com", "Bulk Two", "t:bulk2")
    client = _signed_in(app)

    r = client.post("/api/inventory/delete", json={"ids": [a, b]})
    assert r.status_code == 200 and r.json()["deleted"] == 2
    page = client.get("/inventory?show=all").text
    assert "Bulk One" not in page and "Bulk Two" not in page


def test_bulk_delete_refuses_sold_rows(app_and_data):
    """A sold row is the only record that the sale happened and carries the
    cost basis behind realised profit. A card still in stock can be
    re-imported; that one cannot."""
    from foilstack import db

    app, _ = app_and_data
    keep, _ = _stock_item("owner@example.com", "Still Here", "t:keep")
    sold, _ = _stock_item("owner@example.com", "Already Sold", "t:sold", sold=True)
    client = _signed_in(app)

    r = client.post("/api/inventory/delete", json={"ids": [keep, sold]})
    assert r.status_code == 400
    assert "sold" in r.json()["detail"].lower()

    # And nothing was deleted — not even the stock row alongside it.
    session = db.session()
    assert session.get(db.InventoryItem, keep) is not None
    assert session.get(db.InventoryItem, sold) is not None
    session.close()


def test_bulk_delete_cannot_reach_another_account(stranger, app_and_data):
    from foilstack import db

    mine, _ = _stock_item("owner@example.com", "Not Yours", "t:mine")
    r = stranger.post("/api/inventory/delete", json={"ids": [mine]})
    assert r.status_code == 404

    session = db.session()
    assert session.get(db.InventoryItem, mine) is not None
    session.close()


def test_deleting_a_row_stops_its_scan_claiming_to_be_confirmed(app_and_data):
    """Otherwise the card is in neither the queue nor the inventory, visible
    nowhere, while the database still says the scan was accepted."""
    from sqlalchemy import select as sa_select

    from foilstack import db

    app, _ = app_and_data
    session = db.session()
    owner = session.scalars(sa_select(db.User).where(db.User.email == "owner@example.com")).one()
    card = db.Card(source="t", source_id="t:orphan", name="Orphan", game="mtg", market=1.0)
    session.add(card)
    session.commit()
    job = db.ImportJob(user_id=owner.id, filename="o.zip", status="done")
    session.add(job)
    session.commit()
    scan = db.Scan(
        job_id=job.id, user_id=owner.id, filename="o.jpg", stored_path="1/o.jpg", status="confirmed"
    )
    session.add(scan)
    session.commit()
    item = db.InventoryItem(
        user_id=owner.id, card_id=card.id, scan_id=scan.id, condition="NM", finish="nonfoil"
    )
    session.add(item)
    session.commit()
    item_id, scan_id = item.id, scan.id
    session.close()

    client = _signed_in(app)
    assert client.post("/api/inventory/delete", json={"ids": [item_id]}).status_code == 200

    session = db.session()
    assert session.get(db.Scan, scan_id).status == "discarded"
    session.close()


def test_the_job_log_does_not_show_one_account_what_another_just_did(stranger, app_and_data):
    """The job log is a feed of activity, and activity names things.

    Filenames, SKUs, row counts and export sizes are all somebody's business
    data. A log that is process-wide rather than per-account hands each of
    those to whoever loads the listings page next.
    """
    app, _ = app_and_data
    owner = _signed_in(app)

    # What the stranger can already see, legitimately: their own actions from
    # earlier tests. The question is whether the owner's next move adds to it.
    before = _job_log(stranger.get("/listings").text)

    owner_before = _job_log(owner.get("/listings").text)
    owner.get("/export/tcgplayer")

    # The owner sees their own action...
    assert len(_job_log(owner.get("/listings").text)) == len(owner_before) + 1
    # ...and the stranger's log is untouched by it.
    assert _job_log(stranger.get("/listings").text) == before


def _job_log(html: str) -> list[str]:
    """The messages in the job log panel, in order."""
    panel = html.split('<div class="joblog">', 1)[1].split("</div>\n  </div>", 1)[0]
    lines = re.findall(r'<div class="line">(.*?)</div>', panel, re.S)
    return [re.sub(r"<[^>]+>", "", ln).strip() for ln in lines if "nothing yet" not in ln]


def test_login_starts_refusing_after_a_run_of_wrong_passwords(app_and_data):
    """Unlimited guesses against a real account, and every one of them paying
    for an argon2 verify, is both how the password goes and how the machine
    does."""
    from fastapi.testclient import TestClient

    app, _ = app_and_data
    from foilstack.web.routes import accounts

    accounts._login_ip.clear()
    accounts._login_account.clear()

    client = TestClient(app)
    codes = [
        client.post(
            "/login", data={"email": "owner@example.com", "password": f"wrong-{i}"}
        ).status_code
        for i in range(14)
    ]

    assert 429 in codes, "the login form never started refusing"
    assert codes.index(429) <= 11, "it refused far later than the configured budget"

    # And the refusal outlasts the right password, so a guesser cannot simply
    # keep going until they stumble on it.
    blocked = client.post(
        "/login", data={"email": "owner@example.com", "password": "owner-long-password"}
    )
    assert blocked.status_code == 429

    from foilstack.web.routes import accounts

    accounts._login_ip.clear()
    accounts._login_account.clear()


def test_a_good_password_still_works_once_the_window_is_clear(app_and_data):
    """The limiter must not be a way to lock the real owner out for good."""

    app, _ = app_and_data
    from foilstack.web.routes import accounts

    accounts._login_ip.clear()
    accounts._login_account.clear()
    client = _signed_in(app)
    assert client.get("/app", follow_redirects=False).status_code == 200


def test_registration_can_be_closed(app_and_data, monkeypatch):
    from fastapi.testclient import TestClient

    from foilstack.web import app as web

    app, _ = app_and_data
    _with_setting(monkeypatch, web, allow_registration=False)
    from foilstack.web.routes import accounts

    accounts._register_ip.clear()

    with TestClient(app) as anon:
        assert anon.get("/register").status_code == 403
        made = anon.post(
            "/register",
            data={"email": "opportunist@example.com", "password": "a-long-enough-password"},
        )
        assert made.status_code == 403

    # And the account really was not created.
    from sqlalchemy import select

    from foilstack import db

    session = db.session()
    assert session.scalar(select(db.User).where(db.User.email == "opportunist@example.com")) is None
    session.close()


def test_an_invite_code_is_required_when_one_is_set(app_and_data, monkeypatch):
    from fastapi.testclient import TestClient

    from foilstack.web import app as web

    app, _ = app_and_data
    _with_setting(monkeypatch, web, invite_code="open-sesame")
    from foilstack.web.routes import accounts

    accounts._register_ip.clear()

    with TestClient(app) as anon:
        refused = anon.post(
            "/register",
            data={"email": "nocode@example.com", "password": "a-long-enough-password"},
        )
        assert refused.status_code == 403

        allowed = anon.post(
            "/register",
            data={
                "email": "withcode@example.com",
                "password": "a-long-enough-password",
                "invite": "open-sesame",
            },
            follow_redirects=False,
        )
        assert allowed.status_code == 303

    from foilstack.web.routes import accounts

    accounts._register_ip.clear()


def test_the_security_headers_are_on_every_response(app_and_data):
    from fastapi.testclient import TestClient

    app, _ = app_and_data
    with TestClient(app) as anon:
        headers = anon.get("/login").headers

    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["referrer-policy"] == "strict-origin-when-cross-origin"
    # Not sent over plain HTTP: a self-hoster on a LAN would be locked out of
    # their own deployment by a header they never asked for.
    assert "strict-transport-security" not in headers


def test_an_account_over_its_quota_cannot_upload_more(app_and_data, monkeypatch):
    """Registration is open, so the amount of disk one account may take is
    otherwise decided by that account."""
    import io
    import zipfile

    from foilstack.web import app as web

    app, ids = app_and_data
    _with_setting(monkeypatch, web, max_account_mb=1)

    from foilstack import db

    session = db.session()
    scan = session.get(db.Scan, ids["scan"])
    before = scan.size_bytes
    scan.size_bytes = 2 * 1024 * 1024  # already over the 1 MB ceiling
    session.commit()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("card.jpg", b"x" * 128)

    client = _signed_in(app)
    response = client.post(
        "/api/import",
        files={"archive": ("cards.zip", buf.getvalue(), "application/zip")},
        data={"default_condition": "NM", "default_finish": "nonfoil"},
    )
    assert response.status_code == 413
    assert "limit" in response.json()["detail"]

    scan.size_bytes = before
    session.commit()
    session.close()


def test_the_quota_is_off_by_default(app_and_data):
    """A self-hosted install is one person and their own disk; making them
    configure a limit against themselves would be a worse default."""
    from foilstack.config import Settings

    assert Settings.max_account_mb == 0


def _import(app, files):
    """POST files to /api/import as the owner, and return the response."""
    return _signed_in(app).post(
        "/api/import",
        files=[("archive", f) for f in files],
        data={"default_condition": "NM", "default_finish": "nonfoil"},
    )


def _unpacked(job_id):
    """The image filenames an import job actually landed on disk.

    Read from the scans directory rather than from `scans` rows: run_import
    unpacks the archive and records the count before it looks for catalogue
    vectors, and there are none in this fixture, so it stops before writing a
    single row. The files on disk are what the unpacking step produced.
    """
    from foilstack.config import get_settings

    out = get_settings().scans_dir / str(job_id)
    return sorted(p.name for p in out.iterdir()) if out.is_dir() else []


def test_loose_images_import_without_being_zipped_first(app_and_data):
    """The screen offers jpg/png/tif, so handing it a jpg has to work.

    It used to accept nothing but a .zip while advertising the image formats
    that go *inside* one, which read as a broken uploader rather than as a
    label about archive contents.
    """
    app, _ = app_and_data
    response = _import(
        app,
        [("front.jpg", b"x" * 64, "image/jpeg"), ("back.png", b"y" * 64, "image/png")],
    )
    assert response.status_code == 200, response.text

    from foilstack import db

    session = db.session()
    job = session.get(db.ImportJob, response.json()["job_id"])
    # `total` is committed by run_import once the archive is unpacked, before
    # it gives up for want of catalogue vectors — so it proves the wrapping
    # and the extraction both worked without needing an encoder here.
    assert job.total == 2
    assert job.filename == "2 images"
    assert _unpacked(job.id) == ["back.png", "front.jpg"]
    session.close()


def test_one_loose_image_is_named_after_itself(app_and_data):
    """A single scan should say what it is, not "1 images"."""
    app, _ = app_and_data
    response = _import(app, [("solo.jpg", b"x" * 64, "image/jpeg")])
    assert response.status_code == 200, response.text

    from foilstack import db

    session = db.session()
    assert session.get(db.ImportJob, response.json()["job_id"]).filename == "solo.jpg"
    session.close()


def test_loose_images_sharing_a_filename_both_survive(app_and_data):
    """Two phone folders both hold IMG_0001.jpg, and both are real scans.

    Packing loose uploads into an archive means the extractor's existing
    de-duplication covers this case too, rather than one of them silently
    overwriting the other.
    """
    app, _ = app_and_data
    response = _import(
        app,
        [("IMG.jpg", b"aaaa", "image/jpeg"), ("IMG.jpg", b"bbbb", "image/jpeg")],
    )
    assert response.status_code == 200, response.text

    from foilstack import db

    session = db.session()
    job_id = response.json()["job_id"]
    assert session.get(db.ImportJob, job_id).total == 2
    assert _unpacked(job_id) == ["IMG-1.jpg", "IMG.jpg"]
    session.close()


def test_an_archive_is_still_accepted(app_and_data):
    """The zip path is the original one and must not regress."""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a.jpg", b"x" * 32)
        zf.writestr("notes.txt", b"ignored")

    app, _ = app_and_data
    response = _import(app, [("cards.zip", buf.getvalue(), "application/zip")])
    assert response.status_code == 200, response.text

    from foilstack import db

    session = db.session()
    job = session.get(db.ImportJob, response.json()["job_id"])
    assert job.filename == "cards.zip"
    assert _unpacked(job.id) == ["a.jpg"]
    session.close()


@pytest.mark.parametrize(
    "files",
    [
        pytest.param([("notes.txt", b"x", "text/plain")], id="not-a-scan"),
        pytest.param(
            [
                ("a.zip", b"PK\x05\x06" + b"\0" * 18, "application/zip"),
                ("b.jpg", b"x", "image/jpeg"),
            ],
            id="archive-plus-image",
        ),
        pytest.param(
            [
                ("a.zip", b"PK\x05\x06" + b"\0" * 18, "application/zip"),
                ("b.zip", b"PK\x05\x06" + b"\0" * 18, "application/zip"),
            ],
            id="two-archives",
        ),
    ],
)
def test_uploads_that_are_neither_one_archive_nor_images_are_refused(app_and_data, files):
    """Guessing at a muddled upload is worse than saying what was expected."""
    app, _ = app_and_data
    response = _import(app, files)
    assert response.status_code == 400
    assert "expected one .zip archive" in response.json()["detail"]


def _with_setting(monkeypatch, web, **changes):
    """Run the app with `changes` applied to its Settings.

    Overrides the `settings_dep` dependency rather than reassigning a module
    global. The global only ever reached routes that lived in the same module
    as it, so this stopped working the moment a route moved to its own file —
    and it failed by silently using the real settings rather than by erroring.

    Settings is frozen, which is right for a value read at boot and never meant
    to drift under a running request, so a test replaces the whole object
    rather than poking a field.
    """
    import dataclasses

    from foilstack.config import get_settings
    from foilstack.web import deps

    changed = dataclasses.replace(get_settings(), **changes)
    # `setitem`, not a plain assignment: monkeypatch undoes it at the end of
    # the test. An override left in the dict applies to every later test in
    # the module, and the failure then surfaces somewhere unrelated.
    monkeypatch.setitem(web.app.dependency_overrides, deps.settings_dep, lambda: changed)
    return changed


def test_the_landing_page_knows_who_is_reading_it(app_and_data):
    """Inviting somebody already signed in to create an account is the front
    door admitting it has no idea who is at it — and withholding the one link
    they want, which is the way back to their inventory."""
    from fastapi.testclient import TestClient

    app, _ = app_and_data

    with TestClient(app) as anon:
        cold = anon.get("/").text
    assert "Create an account" in cold
    assert "Open your inventory" not in cold

    warm = _signed_in(app).get("/").text
    assert "Open your inventory" in warm
    assert "Create an account" not in warm
    assert "owner@example.com" in warm


def test_the_landing_page_offers_no_signup_when_registration_is_closed(app_and_data, monkeypatch):
    from fastapi.testclient import TestClient

    from foilstack.web import app as web

    app, _ = app_and_data
    _with_setting(monkeypatch, web, allow_registration=False)

    with TestClient(app) as anon:
        body = anon.get("/").text

    assert "Create an account" not in body
    assert "Sign in" in body


def test_the_landing_proof_images_are_visible_signed_out(app_and_data):
    """The front door argues with two card images, so a signed-out visitor has
    to be able to load them."""
    from fastapi.testclient import TestClient

    from foilstack import db
    from foilstack.web import proof

    app, _ = app_and_data
    session = db.session()
    name, set_name = proof.PROOF_CARDS[0]
    card = db.Card(
        source="t",
        source_id="t:proof",
        name=name,
        set_name=set_name,
        game="pokemon",
        image_url="https://example.invalid/charizard.jpg",
    )
    # An ordinary catalogue card, with an image, that is *not* one of the two.
    # A card that simply does not exist would 404 whether the allowlist works or
    # not, which is a test that cannot fail.
    ordinary = db.Card(
        source="t",
        source_id="t:ordinary",
        name="Some Other Card",
        set_name="Some Other Set",
        game="pokemon",
        image_url="https://example.invalid/other.jpg",
    )
    session.add_all([card, ordinary])
    session.commit()
    card_id, ordinary_id = card.id, ordinary.id
    session.close()

    try:
        with TestClient(app) as anon:
            # Not bounced to the login screen: it gets as far as trying to
            # fetch, which is all this half is about.
            assert anon.get(f"/card/{card_id}/image", follow_redirects=False).status_code != 303

            # And the exception is those two cards, not the catalogue.
            assert anon.get(f"/card/{ordinary_id}/image", follow_redirects=False).status_code == 404
    finally:
        session = db.session()
        for cid in (card_id, ordinary_id):
            row = session.get(db.Card, cid)
            if row is not None:
                session.delete(row)
        session.commit()
        session.close()


def test_an_uningested_catalogue_shows_no_proof_thumbnails(app_and_data):
    """A fresh install has no Charizard, and the table renders without images
    rather than with two broken ones."""
    from fastapi.testclient import TestClient

    app, _ = app_and_data
    with TestClient(app) as anon:
        body = anon.get("/").text

    # The proof rows are there; the thumbnails are not, because nothing matched.
    assert "Base Set · Holofoil" in body
    assert "/card/None/image" not in body


def test_a_slow_image_fetch_does_not_hold_a_database_connection(app_and_data, monkeypatch):
    """This took the site down.

    The route held a pooled connection for the whole upstream fetch. With a
    pool of five plus ten overflow, fifteen concurrent thumbnails against a
    slow CDN starved every other route — `/healthz` included — for thirty
    seconds. It survived a small catalogue because misses were rare, and
    stopped surviving at thirty thousand cards, where many promo entries have
    no image behind them at all.
    """
    import httpx as _httpx

    from foilstack import db
    from foilstack.web.routes import media

    app, _ = app_and_data

    session = db.session()
    card = db.Card(
        source="t",
        source_id="t:slow",
        name="Slow Card",
        set_name="Slow Set",
        game="pokemon",
        image_url="https://example.invalid/slow_200w.jpg",
    )
    session.add(card)
    session.commit()
    card_id = card.id
    session.close()

    in_flight_connections = []

    class _Hang:
        """An upstream that never answers, and records the pool while it hangs."""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            engine = db._engine
            in_flight_connections.append(engine.pool.checkedout())
            raise _httpx.ConnectTimeout("upstream is not answering")

    monkeypatch.setattr(media.httpx, "AsyncClient", lambda **kw: _Hang())

    try:
        client = _signed_in(app)
        assert client.get(f"/card/{card_id}/image").status_code == 404

        # The whole point: nothing was checked out of the pool while the
        # request sat on the network.
        assert in_flight_connections, "the fetch never ran"
        assert max(in_flight_connections) == 0, (
            f"held {max(in_flight_connections)} database connection(s) across the fetch"
        )
    finally:
        session = db.session()
        row = session.get(db.Card, card_id)
        if row is not None:
            session.delete(row)
        session.commit()
        session.close()


def test_serving_a_scan_does_not_hold_a_database_connection(app_and_data):
    """The other half of the outage.

    `scan_image` is a `def` route, so FastAPI runs it in a threadpool forty
    threads wide, and each one held a pooled connection through a Pillow
    resize. Forty threads against a pool of fifteen empties it, and every other
    route — `/healthz` included — then waits for a connection that is not
    coming.
    """
    from foilstack import db

    app, ids = app_and_data
    client = _signed_in(app)

    # Warm anything lazy, so the measurement below is the route and not setup.
    client.get(f"/scan/{ids['scan']}/image")

    before = db._engine.pool.checkedout()
    response = client.get(f"/scan/{ids['scan']}/image")
    after = db._engine.pool.checkedout()

    assert response.status_code == 200
    assert after == before, f"leaked {after - before} connection(s) serving a scan"

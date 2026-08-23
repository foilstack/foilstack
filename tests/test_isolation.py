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
from sqlalchemy import create_engine, text

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

    # `web.settings` is bound once, when the module is first imported. If any
    # earlier test module imported it, that binding already happened against
    # the developer's own database and clearing the cache above does not undo
    # it — the app then talks to the wrong database and every test here fails
    # on a password error that names nothing to do with test ordering.
    web.settings = get_settings()

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

    from foilstack.web import app as web

    app, _ = app_and_data
    web._login_ip.clear()
    web._login_account.clear()

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

    web._login_ip.clear()
    web._login_account.clear()


def test_a_good_password_still_works_once_the_window_is_clear(app_and_data):
    """The limiter must not be a way to lock the real owner out for good."""
    from foilstack.web import app as web

    app, _ = app_and_data
    web._login_ip.clear()
    web._login_account.clear()
    client = _signed_in(app)
    assert client.get("/app", follow_redirects=False).status_code == 200


def test_registration_can_be_closed(app_and_data, monkeypatch):
    from fastapi.testclient import TestClient

    from foilstack.web import app as web

    app, _ = app_and_data
    _with_setting(monkeypatch, web, allow_registration=False)
    web._register_ip.clear()

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
    web._register_ip.clear()

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

    web._register_ip.clear()


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


def _with_setting(monkeypatch, web, **changes):
    """Swap the module's Settings for one with `changes` applied.

    Settings is frozen, which is the right call for a value read once at boot
    and never meant to drift underneath a running request. It does mean a test
    replaces the whole object rather than poking a field.
    """
    import dataclasses

    monkeypatch.setattr(web, "settings", dataclasses.replace(web.settings, **changes))

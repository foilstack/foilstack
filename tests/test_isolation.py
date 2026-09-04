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
from contextlib import contextmanager

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


def test_stranger_cannot_unmark_someone_elses_rows(stranger, app_and_data):
    """Unlisting somebody's card is as much a write as listing it, and the
    quieter of the two: it puts a card back into an export they already ran."""
    _, ids = app_and_data
    r = stranger.post(
        "/api/listings/unmark",
        json={"ids": [ids["item"]], "channels": ["tcgplayer"]},
    )
    assert r.json()["unmarked"] == 0


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


def test_the_panel_offers_the_encoders_guess_back_after_a_correction(app_and_data):
    """Correcting a row must not be a one-way door.

    "Wrong card?" used to drop rank zero from its list unconditionally, which
    is right only while rank zero is what the row is showing. Once a person has
    picked something else — or the batch has moved the row — the encoder's
    guess is a runner-up like any other, and leaving it out meant a mis-click
    could only be undone by discarding the scan.

    On its own account and its own scan: the shared fixture's row is read by a
    dozen tests several hundred lines away.
    """
    from fastapi.testclient import TestClient

    from foilstack import db
    from foilstack.web import auth

    app, _ = app_and_data
    session = db.session()
    user = db.User(
        email="panel@example.com",
        password_hash=auth.hash_password("panel-long-password"),
    )
    guess = db.Card(source="t", source_id="t:guess", name="Encoder Guess", game="mtg", market=2.0)
    picked = db.Card(
        source="t", source_id="t:picked", name="Picked Instead", game="mtg", market=3.0
    )
    session.add_all([user, guess, picked])
    session.commit()
    job = db.ImportJob(user_id=user.id, filename="p.zip", status="done", total=1, processed=1)
    session.add(job)
    session.commit()
    scan = db.Scan(
        job_id=job.id,
        user_id=user.id,
        filename="p.jpg",
        stored_path=f"{job.id}/p.jpg",
        status="pending",
    )
    session.add(scan)
    session.commit()
    session.add_all(
        [
            db.Candidate(scan_id=scan.id, card_id=guess.id, score=0.97, rank=0),
            db.Candidate(scan_id=scan.id, card_id=picked.id, score=0.91, rank=1),
        ]
    )
    session.commit()
    scan_id = scan.id
    session.close()

    client = TestClient(app)
    client.post("/login", data={"email": "panel@example.com", "password": "panel-long-password"})

    # On what is offered to click, not on the names: both names are on this
    # panel either way, because the shown card's name is also what seeds the
    # search box.
    guess_id, picked_id = guess.id, picked.id

    # Before the correction the panel offers the runner-up and not the guess,
    # because the guess is what the row is showing.
    panel = client.get(f"/api/scans/{scan_id}/match-panel").text
    assert f'data-pick="{picked_id}"' in panel
    assert f'data-pick="{guess_id}"' not in panel

    client.post(f"/api/scans/{scan_id}/choose", data={"card_id": picked_id})

    # After it, the two swap places for exactly the same reason.
    panel = client.get(f"/api/scans/{scan_id}/match-panel").text
    assert f'data-pick="{guess_id}"' in panel
    assert f'data-pick="{picked_id}"' not in panel


def test_a_batch_moved_row_shows_the_match_it_was_moved_off(app_and_data):
    """The queue's account of why a row is not pointing where the encoder said.

    Read through the page rather than the column: the row is the whole point of
    recording `cohort_card_id` separately, and a value stored but not rendered
    would pass a column assertion and show the seller nothing.
    """
    from fastapi.testclient import TestClient

    from foilstack import db
    from foilstack.web import auth

    app, _ = app_and_data
    session = db.session()
    user = db.User(
        email="batch@example.com",
        password_hash=auth.hash_password("batch-long-password"),
    )
    stray = db.Card(
        source="t",
        source_id="t:stray",
        name="Stray Match",
        game="pokemon",
        set_name="Jungle",
        market=2.0,
    )
    inset = db.Card(
        source="t",
        source_id="t:inset",
        name="In Set",
        game="pokemon",
        set_name="Base Set",
        market=3.0,
    )
    session.add_all([user, stray, inset])
    session.commit()
    job = db.ImportJob(
        user_id=user.id,
        filename="b.zip",
        status="done",
        total=1,
        processed=1,
        same_game=True,
        same_set=True,
        cohort_game="pokemon",
        cohort_set="Base Set",
    )
    session.add(job)
    session.commit()
    scan = db.Scan(
        job_id=job.id,
        user_id=user.id,
        filename="b.jpg",
        stored_path=f"{job.id}/b.jpg",
        status="pending",
        cohort_card_id=inset.id,
    )
    session.add(scan)
    session.commit()
    session.add_all(
        [
            db.Candidate(scan_id=scan.id, card_id=stray.id, score=0.97, rank=0),
            db.Candidate(scan_id=scan.id, card_id=inset.id, score=0.88, rank=1),
        ]
    )
    session.commit()
    scan_id, inset_id = scan.id, inset.id
    session.close()

    client = TestClient(app)
    client.post("/login", data={"email": "batch@example.com", "password": "batch-long-password"})
    queue = client.get("/app").text

    assert f'data-scan="{scan_id}" data-card="{inset_id}"' in queue
    assert "moved to the batch" in queue
    assert "encoder said: Stray Match 97%" in queue
    # The cohort the batch settled on, said once on the section rather than on
    # every row inside it.
    assert "batch is pokemon · Base Set" in queue
    # And the score under the bar is the moved-to card's, not rank zero's.
    assert "88% match" in queue


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
    out = {
        "card_id": card.id,
        "item_ids": [
            i.id
            for i in session.scalars(
                sa_select(db.InventoryItem).where(db.InventoryItem.card_id == card.id)
            ).all()
        ],
    }
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


def test_the_queue_seeds_a_foil_only_card_as_foil(app_and_data):
    """A batch imported as non-foil still contains cards the catalogue only
    prices as foil. The row starts on foil rather than on the batch's answer
    wearing a warning, because the default was never a decision about that
    card — and the batch default stays on the row so a corrected match can be
    resolved again."""
    from sqlalchemy import select as sa_select

    from foilstack import db
    from foilstack.web.routes.scans import _queue_rows

    session = db.session()
    owner = session.scalars(sa_select(db.User).where(db.User.email == "owner@example.com")).one()
    card = db.Card(source="t", source_id="t:foilonly", name="Foil Only", game="mtg", market=9.0)
    session.add(card)
    session.commit()
    session.add(db.CardPrice(card_id=card.id, sub_type="Holofoil", market=9.0))
    job = db.ImportJob(
        user_id=owner.id,
        filename="f.zip",
        status="done",
        total=1,
        processed=1,
        default_finish="nonfoil",
    )
    session.add(job)
    session.commit()
    scan = db.Scan(
        job_id=job.id,
        user_id=owner.id,
        filename="f.jpg",
        stored_path="1/f.jpg",
        status="pending",
    )
    session.add(scan)
    session.commit()
    session.add(db.Candidate(scan_id=scan.id, card_id=card.id, score=0.97, rank=0))
    session.commit()
    scan_id = scan.id

    row = next(r for r in _queue_rows(session, owner.id) if r["scan_id"] == scan_id)
    assert row["finish"] == "foil"
    assert row["default_finish"] == "nonfoil"
    session.close()


def test_the_queue_keeps_the_default_when_both_finishes_are_priced(app_and_data):
    """The counterpart. Where the catalogue prices both sides, the seller's
    default is the only answer there is and nothing may overrule it."""
    from sqlalchemy import select as sa_select

    from foilstack import db
    from foilstack.web.routes.scans import _queue_rows

    session = db.session()
    owner = session.scalars(sa_select(db.User).where(db.User.email == "owner@example.com")).one()
    card = db.Card(source="t", source_id="t:both", name="Both Ways", game="mtg", market=4.0)
    session.add(card)
    session.commit()
    for sub, market in (("Normal", 4.0), ("Holofoil", 40.0)):
        session.add(db.CardPrice(card_id=card.id, sub_type=sub, market=market))
    job = db.ImportJob(
        user_id=owner.id,
        filename="g.zip",
        status="done",
        total=1,
        processed=1,
        default_finish="nonfoil",
    )
    session.add(job)
    session.commit()
    scan = db.Scan(
        job_id=job.id,
        user_id=owner.id,
        filename="g.jpg",
        stored_path="1/g.jpg",
        status="pending",
    )
    session.add(scan)
    session.commit()
    session.add(db.Candidate(scan_id=scan.id, card_id=card.id, score=0.97, rank=0))
    session.commit()
    scan_id = scan.id

    row = next(r for r in _queue_rows(session, owner.id) if r["scan_id"] == scan_id)
    assert row["finish"] == "nonfoil"
    session.close()


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


def test_the_list_button_ships_disabled_so_a_run_needs_a_selection(app_and_data):
    """No selection must not be a listing run over everything the seller owns.

    The button submits the selection form, and an unticked row submits no id at
    all — which `/listings` reads as "the whole inventory" and prices
    accordingly. The screen it was pressed from may well have been narrowed to
    the eighteen unlisted lines, and none of that narrowing is on the form, so
    the run answers for cards the seller was not looking at. It ships disabled
    for that reason rather than merely being relabelled, and it starts that way
    in the markup rather than waiting for a script to catch up.
    """
    app, _ = app_and_data
    client = _signed_in(app)

    tag = re.search(r"<button[^>]*id=\"listbtn\"[^>]*>", client.get("/inventory").text)
    assert tag, "the inventory bar no longer has a #listbtn"
    assert "disabled" in tag.group(0), tag.group(0)

    # And the thing it is guarding: no ids is not an empty run, it is all of
    # them. If this ever starts answering zero, the button may go back to being
    # a plain link and this whole test is moot.
    body = client.get("/listings").text
    assert "whole inventory" in body


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


def _queue_row(html: str, scan_id: int) -> str:
    """The one row for this scan, sliced out of the import screen.

    Asserting against the whole page would pass on a chip belonging to some
    other row, which is precisely the mistake the tests below exist to catch.
    """
    for chunk in html.split('class="qrow'):
        if f'id="scan-{scan_id}"' in chunk:
            return chunk
    raise AssertionError(f"scan {scan_id} is not in the queue")


@contextmanager
def _waiting_scan(ids, **job_settings):
    """A second batch waiting in the queue, torn down afterwards.

    Its own job rather than the fixture's, because the fixture is module-scoped
    and a test that edited its import settings in place would hand them to
    every test that ran after it.
    """
    from foilstack import db

    session = db.session()
    owner_id = session.get(db.Scan, ids["scan"]).user_id
    job = db.ImportJob(
        user_id=owner_id, filename="b.zip", status="done", total=1, processed=1, **job_settings
    )
    session.add(job)
    session.commit()
    scan = db.Scan(
        job_id=job.id,
        user_id=owner_id,
        filename="second.jpg",
        stored_path="1/card.jpg",
        status="pending",
    )
    session.add(scan)
    session.commit()
    session.add(db.Candidate(scan_id=scan.id, card_id=ids["card"], score=0.71, rank=0))
    session.commit()
    scan_id = scan.id
    try:
        yield scan_id
    finally:
        session.delete(session.get(db.Scan, scan_id))
        session.delete(session.get(db.ImportJob, job.id))
        session.commit()
        session.close()


def test_a_queue_row_opens_on_the_defaults_its_import_was_given(app_and_data):
    """ "Default condition" reached only the scans that auto-accepted.

    Which is to say: only the ones nobody ever looks at. A batch graded DMG
    came back to the review queue as NM, so the setting changed nothing about
    the cards actually being confirmed — and grading each row by hand is the
    work the default exists to save.
    """
    from foilstack import inventory

    app, ids = app_and_data
    client = _signed_in(app)

    with _waiting_scan(ids, default_condition="DMG", default_finish="foil") as scan_id:
        row = _queue_row(client.get("/app").text, scan_id)

    assert 'data-cond="DMG"' in row
    assert 'data-finish="foil"' in row
    # Not just the dataset the commit reads: the chip a person sees selected
    # and the value that would be committed have to be the same claim.
    assert 'chip-sm on" type="button" data-cond="DMG"' in row
    assert ">Foil<" in row
    assert ">Non-foil<" not in row
    # Filled, because the row is still showing what the import asked for. The
    # class used to be pinned to foil, so a batch imported as non-foil had no
    # row on the screen marked as set to anything.
    assert "foil-toggle on" in row
    # Every grade is reachable from the row. DMG used to be sliced off the end
    # of the chips to save horizontal room, which left the worst grade settable
    # in the import defaults and on the card page but not while confirming —
    # so a damaged card could only be marked damaged once it was inventory.
    for condition in inventory.CONDITIONS:
        assert f'data-cond="{condition}"' in row


def test_one_batch_of_defaults_does_not_leak_onto_another(app_and_data):
    """Two imports can be waiting at once, and they are graded separately.

    The reason these are read off each scan's own job rather than off the
    settings panel, which knows only about the next import.
    """
    app, ids = app_and_data
    client = _signed_in(app)

    with (
        _waiting_scan(ids, default_condition="DMG", default_finish="foil") as damaged,
        _waiting_scan(ids, default_condition="LP", default_finish="nonfoil") as played,
    ):
        html = client.get("/app").text
        damaged_row, played_row = _queue_row(html, damaged), _queue_row(html, played)

    assert 'data-cond="DMG"' in damaged_row
    assert 'data-finish="foil"' in damaged_row
    assert 'data-cond="LP"' in played_row
    assert 'data-finish="nonfoil"' in played_row
    # Both are filled: each is showing its own batch's answer, opposite though
    # those answers are.
    assert "foil-toggle on" in damaged_row
    assert "foil-toggle on" in played_row


@contextmanager
def _waiting_scans_worth(ids, markets, filename="worth.zip"):
    """A batch waiting in the queue, one scan per value in `markets`.

    Its own cards, because the thing under test is an ordering by price and the
    fixture's single card would give every row the same key. A `None` market
    means a scan that matched nothing at all: no card, no candidate.

    `filename` names the upload, so two of these nested are two batches the
    queue has to keep apart.
    """
    from foilstack import db

    session = db.session()
    owner_id = session.get(db.Scan, ids["scan"]).user_id
    job = db.ImportJob(
        user_id=owner_id,
        filename=filename,
        status="done",
        total=len(markets),
        processed=len(markets),
    )
    session.add(job)
    session.commit()

    made = []
    for i, market in enumerate(markets):
        card = None
        if market is not None:
            card = db.Card(
                source="t",
                source_id=f"worth:{uuid.uuid4().hex[:10]}",
                name=f"Card worth {market}",
                game="mtg",
                market=market,
            )
            session.add(card)
            session.commit()
        scan = db.Scan(
            job_id=job.id,
            user_id=owner_id,
            filename=f"worth-{i}.jpg",
            stored_path="1/card.jpg",
            status="pending",
        )
        session.add(scan)
        session.commit()
        if card is not None:
            session.add(db.Candidate(scan_id=scan.id, card_id=card.id, score=0.8, rank=0))
            session.commit()
        made.append((scan.id, card.id if card else None))
    try:
        yield [scan_id for scan_id, _ in made]
    finally:
        for scan_id, _ in made:
            session.delete(session.get(db.Scan, scan_id))
        session.commit()
        for _, card_id in made:
            if card_id is not None:
                session.delete(session.get(db.Card, card_id))
        session.delete(session.get(db.ImportJob, job.id))
        session.commit()
        session.close()


def _queue_order(html: str) -> list[int]:
    """The scan ids of the queue rows, in the order the page puts them."""
    return [int(n) for n in re.findall(r'id="scan-(\d+)"', html)]


def test_the_queue_follows_the_order_the_scans_arrived_in(app_and_data):
    """The seller has the physical stack in their hand, in the order they
    photographed it. A queue in any other order makes them hunt for each card
    instead of working down the pile.

    This replaced dearest-first, so the values here are deliberately neither
    ascending nor descending: an ordering by price would fail this, and so
    would newest-first.
    """
    worth = [3.0, 40.0, 0.5, 12.0]
    app, ids = app_and_data
    client = _signed_in(app)

    with _waiting_scans_worth(ids, worth) as scans:
        order = _queue_order(client.get("/app").text)

    ours = [s for s in order if s in set(scans)]
    assert len(ours) == len(worth), "the batch is not all on the page"
    # `_waiting_scans_worth` creates them in list order, which is what an
    # import does with the files it extracts.
    assert ours == scans


def test_an_import_creates_its_scans_in_the_archives_order(app_and_data, tmp_path, monkeypatch):
    """The link between the two ends, which nothing else covers.

    `extract_archive` returns the archive's order and the queue renders scans
    by ascending id, but those only meet if `run_import` creates one row per
    file, in order, as it walks the list. It does that today because the loop
    is sequential — and that is exactly the kind of property a later change
    could break silently. Matching one image at a time is the slow part of an
    import, so somebody will eventually be tempted to run several at once; if
    they do, the ids interleave and the queue quietly stops matching the pile
    the seller is holding.
    """
    import asyncio
    import zipfile as zf

    from foilstack import db, importing
    from foilstack.config import get_settings

    app, ids = app_and_data
    client = _signed_in(app)
    settings = get_settings()

    # Alphabetically a, b, c, d — stored deliberately out of that order.
    names = ["d-fourth.jpg", "b-second.jpg", "a-first.jpg", "c-third.jpg"]
    archive = tmp_path / "stack.zip"
    with zf.ZipFile(archive, "w") as z:
        for n in names:
            z.writestr(n, b"pretend jpeg")

    # The encoder and the catalogue are not what is under test. Stubbed down to
    # nothing so every scan lands "unmatched", which still puts it in the queue.
    async def _no_vector(url, blob):
        return [0.0]

    monkeypatch.setattr(importing, "embed_image", _no_vector)
    monkeypatch.setattr(importing.search, "count", lambda *a, **k: 1)
    monkeypatch.setattr(importing.search, "search", lambda *a, **k: [])
    monkeypatch.setattr(importing.images, "make_display_copy", lambda *a, **k: None)

    session = db.session()
    owner_id = session.get(db.Scan, ids["scan"]).user_id
    job = db.ImportJob(user_id=owner_id, filename="stack.zip", status="pending")
    session.add(job)
    session.commit()
    job_id = job.id

    try:
        asyncio.run(importing.run_import(job_id, archive, settings))

        made = session.scalars(
            select(db.Scan).where(db.Scan.job_id == job_id).order_by(db.Scan.id)
        ).all()
        assert [s.filename for s in made] == names, "rows were not created in archive order"

        # And the screen agrees, which is the thing the seller actually sees.
        html = client.get("/app").text
        shown = [n for n in re.findall(r'<div class="qfile" title="([^"]+)"', html) if n in names]
        assert shown == names
        assert shown != sorted(names), "alphabetical would have passed by accident"
    finally:
        for scan in session.scalars(select(db.Scan).where(db.Scan.job_id == job_id)).all():
            session.delete(scan)
        session.commit()
        session.delete(session.get(db.ImportJob, job_id))
        session.commit()
        session.close()


def test_the_queue_keeps_each_upload_together(app_and_data):
    """Grouping outranks price, and only the order can show it.

    The two batches here are chosen to interleave under a flat sort — the
    older one holds the second-dearest card on the page — so a queue that
    still ranked every row by value alone would put it between the newer
    batch's two cards and fail here.
    """
    app, ids = app_and_data
    client = _signed_in(app)

    with (
        _waiting_scans_worth(ids, [1.0, 30.0], filename="older.zip") as older,
        _waiting_scans_worth(ids, [2.0, 50.0], filename="newer.zip") as newer,
    ):
        order = _queue_order(client.get("/app").text)

    mine = set(older) | set(newer)
    ours = [s for s in order if s in mine]
    assert len(ours) == 4, "the batches are not all on the page"

    # Oldest upload first, so the backlog drains from the front rather than
    # sinking further with every import.
    assert set(ours[:2]) == set(older)
    assert set(ours[2:]) == set(newer)

    # And inside each, the order the scans arrived in.
    assert ours[:2] == older
    assert ours[2:] == newer


def test_every_upload_section_starts_expanded(app_and_data):
    """Collapsing is the seller's move to make, not the page's.

    The sections are `<details>`, so the whole feature is one attribute — and
    losing it is not a visual blemish but a queue that renders with every card
    hidden and no indication that anything is waiting. Worth an assertion
    despite being markup: there is no behaviour to observe instead, and this
    is the one way the feature fails catastrophically rather than untidily.
    """
    app, ids = app_and_data
    client = _signed_in(app)

    with (
        _waiting_scans_worth(ids, [1.0], filename="older.zip"),
        _waiting_scans_worth(ids, [2.0], filename="newer.zip"),
    ):
        html = client.get("/app").text

    # Every opening `<details>` on the page, with its attributes — matched
    # tolerantly so that adding one does not fail this for the wrong reason.
    sections = [tag for tag in re.findall(r"<details\b[^>]*>", html) if "qgroup" in tag]
    assert len(sections) >= 2, "the uploads are not rendering as sections"
    assert all("open" in tag for tag in sections)


def _section(html: str, job_id: int) -> str:
    """The opening `<details>` tag for one upload's section."""
    found = [t for t in re.findall(r"<details\b[^>]*>", html) if f'data-job="{job_id}"' in t]
    assert len(found) == 1, f"expected one section for job {job_id}, got {len(found)}"
    return found[0]


def _job_of(scan_id: int) -> tuple[int, int]:
    from foilstack import db

    session = db.session()
    scan = session.get(db.Scan, scan_id)
    out = (scan.job_id, scan.user_id)
    session.close()
    return out


def test_a_folded_section_is_rendered_folded(app_and_data):
    """The fold is applied by the server, and that is the whole point of it.

    Restoring it in the browser is the obvious way and cannot be made to look
    right: localStorage is unreadable until the page has parsed, so the queue
    painted expanded and snapped shut once the script caught up — 56ms here,
    123ms with the CPU throttled. Sent as a cookie, the answer is known while
    the markup is being written and there is nothing to correct.
    """
    app, ids = app_and_data
    client = _signed_in(app)

    with (
        _waiting_scans_worth(ids, [1.0], filename="older.zip") as older,
        _waiting_scans_worth(ids, [2.0], filename="newer.zip") as newer,
    ):
        old_job, user_id = _job_of(older[0])
        new_job, _ = _job_of(newer[0])
        client.cookies.set(f"foilstack_folded_{user_id}", str(old_job))
        html = client.get("/app").text
        client.cookies.clear()

    assert "open" not in _section(html, old_job)
    # Only the one named. A fold is per upload, not a mode the screen is in.
    assert "open" in _section(html, new_job)


def test_a_mangled_fold_cookie_costs_a_fold_not_the_page(app_and_data):
    """It is edited by a browser and survives in one for a year.

    Anything at all can be in there by the time it comes back — a truncated
    write, a hand-edit, a leftover from a different version of this screen. It
    has to degrade to "nothing folded", because a 500 on the queue would mean
    a seller could not reach their cards until they thought to clear cookies.
    """
    app, ids = app_and_data
    client = _signed_in(app)

    with _waiting_scans_worth(ids, [1.0], filename="older.zip") as older:
        job_id, user_id = _job_of(older[0])
        # None of these name this job. A value that does — including one with
        # an empty element beside it, like "4,,7" — is not junk but a fold,
        # and is covered above. Nor is `f"{job_id};DROP"`: a semicolon ends a
        # cookie value in the header, so the server is handed a bare id and is
        # right to fold on it. The digits have to be glued to something to
        # stay junk.
        junk = ("", "not-a-number", ",,,", "-3", "9" * 400, f"{job_id}x", "<script>")
        for junk_value in junk:
            client.cookies.set(f"foilstack_folded_{user_id}", junk_value)
            got = client.get("/app")
            assert got.status_code == 200, f"{junk_value!r} took the page down"
            assert "open" in _section(got.text, job_id), f"{junk_value!r} folded it"
        client.cookies.clear()


def test_price_does_not_disturb_the_arrival_order(app_and_data):
    """Cards of wildly different value keep the order they were scanned in.

    This is the old dearest-first rule's own test, inverted. It earned its
    place then and keeps it now for the opposite reason: the sort key changed,
    and the cheapest card leading the page is the proof.
    """
    app, ids = app_and_data
    client = _signed_in(app)

    with _waiting_scans_worth(ids, [0.05, 250.0, 7.0]) as scans:
        order = _queue_order(client.get("/app").text)

    ours = [s for s in order if s in set(scans)]
    assert ours == scans
    assert ours[0] == scans[0], "the five-cent card was scanned first"


def test_a_scan_that_matched_nothing_keeps_its_place_in_the_pile(app_and_data):
    """It used to be sorted to the bottom, because it has no value to confirm.

    Under an arrival order it stays where it was scanned, and that is the more
    useful answer: the card is somewhere in the stack the seller is holding,
    and a row that has moved is one they cannot pair with the card in their
    hand. The "No match" tab is still how you go looking for these on purpose.
    """
    app, ids = app_and_data
    client = _signed_in(app)

    with _waiting_scans_worth(ids, [0.25, None, 4.0]) as scans:
        order = _queue_order(client.get("/app").text)

    ours = [s for s in order if s in set(scans)]
    assert ours == scans
    # Second in, second on the page — not last.
    assert ours[1] == scans[1]


@contextmanager
def _priced_card(printings, market=None):
    """One catalogue card with per-printing prices, torn down afterwards."""
    from foilstack import db

    session = db.session()
    card = db.Card(
        source="t",
        source_id=f"priced:{uuid.uuid4().hex[:10]}",
        name="Corrected Card",
        game="mtg",
        set_name="Some Set",
        number="42",
        market=market,
    )
    session.add(card)
    session.commit()
    for sub_type, price in printings.items():
        session.add(db.CardPrice(card_id=card.id, sub_type=sub_type, market=price))
    session.commit()
    try:
        yield card.id
    finally:
        session.query(db.CardPrice).filter(db.CardPrice.card_id == card.id).delete()
        session.commit()
        session.delete(session.get(db.Card, card.id))
        session.commit()
        session.close()


def test_correcting_a_match_hands_back_the_new_card_s_price(app_and_data):
    """The corrected row had nowhere to get it, so it showed nothing.

    Picking a card rewrites the row in the browser, and the panel it was picked
    from lists names and sets, not per-printing prices. The old code blanked
    the prices of the card being replaced — right, as far as it went — and
    never put anything back, so the one row a person had just taken a
    deliberate interest in was the only one on screen with no price under it,
    until it was committed and became inventory.
    """
    app, ids = app_and_data
    client = _signed_in(app)

    with _priced_card({"Normal": 3.5, "Foil": 21.0}, market=3.5) as card_id:
        saved = client.post(f"/api/scans/{ids['scan']}/choose", data={"card_id": card_id}).json()

    assert saved["ok"] is True
    assert saved["prices"] == {"Normal": 3.5, "Foil": 21.0}
    # The meta line, rendered by the server so the corrected row reads exactly
    # as one the queue drew itself — the price included, which is the half the
    # browser could not have built on its own.
    assert saved["meta"] == "mtg · Some Set · #42 · $3.50"


def test_a_printing_with_no_price_is_left_out_rather_than_sent_as_null(app_and_data):
    """The row's price rule takes the dearest of what it is given.

    A `null` in that map sorts as a number in JavaScript and would win or lose
    the comparison by accident, putting `$null` under a card's name.
    """
    app, ids = app_and_data
    client = _signed_in(app)

    with _priced_card({"Normal": 1.25, "Foil": None}) as card_id:
        saved = client.post(f"/api/scans/{ids['scan']}/choose", data={"card_id": card_id}).json()

    assert saved["prices"] == {"Normal": 1.25}
    # No market on the card either, so the meta line simply stops after the
    # number rather than trailing a separator with nothing behind it.
    assert saved["meta"] == "mtg · Some Set · #42"


# ==========================================================================
# The plugins screen
#
# It was a manifest of installed code for long enough that the enrichment
# registry shipped without it noticing — `foilstack plugins` listed MTGJSON,
# `docs/plugins.md` had a section for it, and the one screen named after
# plugins rendered two of the three kinds. These assert on what the page
# claims, not on how it draws it.
# ==========================================================================


@contextmanager
def _catalogue_game(game: str, name: str = "Backfilled", **sync):
    """One card in its own game, plus any `sync_state` rows it needs.

    Its own game rather than the fixture's, because the plugins page groups by
    game and a test that leant on `mtg` would read whatever the tests before it
    had left there. Cleans both tables up: a stray `sync_state` row is invisible
    here and changes the footer figure on every other test in the module.
    """
    from foilstack import db

    session = db.session()
    card = db.Card(source="t", source_id=f"t:{game}", name=name, game=game, market=1.0)
    session.add(card)
    for kind, row in sync.items():
        session.add(db.SyncState(kind=f"{kind}:{game}", **row))
    session.commit()
    card_id = card.id
    session.close()
    try:
        yield card_id
    finally:
        session = db.session()
        session.execute(text("DELETE FROM sync_state WHERE kind LIKE :k"), {"k": f"%:{game}"})
        session.execute(text("DELETE FROM cards WHERE id = :id"), {"id": card_id})
        session.commit()
        session.close()


def test_the_plugins_page_lists_enrichers_not_just_sources_and_exporters(app_and_data):
    """The regression that prompted the rewrite.

    `enrichment_plugins()` existed, the CLI printed it and the docs described
    it; the page passed `sources` and `exporters` and nothing else, so the
    whole enrichment feature was invisible in the interface. A page named after
    plugins has to name every kind of plugin there is.
    """
    app, _ = app_and_data
    body = _signed_in(app).get("/plugins").text

    assert "mtgjson" in body, "the installed enricher is not on the page"
    assert "tcgcsv" in body, "the installed source is not on the page"
    assert "foilstack enrich --source mtgjson --game magic" in body, (
        "the page has to say how to run the enricher it names"
    )


def test_the_plugins_page_names_games_rather_than_slugs(app_and_data):
    """`dragonballfusion` is a cache key; the game is called Dragon Ball Fusion World.

    The plugin contract carries `labels` precisely so a screen never has to
    hardcode one. This page printed thirteen raw slugs into a single cell,
    which is both wrong to read and what made the table wider than its panel.
    """
    app, _ = app_and_data
    with _catalogue_game("dragonballfusion"):
        body = _signed_in(app).get("/plugins").text

    # The source's game list, which used to be thirteen raw slugs in one cell.
    assert "Pokémon, Magic: The Gathering, Yu-Gi-Oh!" in body
    assert "pokemon, magic, yugioh" not in body
    # And the catalogue row, which would otherwise title-case the slug it
    # groups by. `Dragonballfusion` is what "no label was consulted" looks like.
    assert "Dragon Ball Fusion World" in body
    assert "Dragonballfusion" not in body


def test_a_game_with_no_price_sync_is_named_on_the_plugins_page(app_and_data):
    """ "never" here is data loss in progress, not a stale cache.

    TCGCSV mirrors the current day only, so a game whose sync has never run is
    losing history permanently. The row says so in the tooltip a reader gets,
    rather than leaving a blank cell to be interpreted.
    """
    app, _ = app_and_data
    with _catalogue_game("neversynced"):
        body = _signed_in(app).get("/plugins").text

    assert "no price sync has ever run for neversynced" in body


def test_a_backfill_that_has_run_is_reported_with_what_it_recorded(app_and_data):
    """The one question `foilstack enrich` leaves an operator with.

    It is the command most likely to be run twice by someone unsure whether the
    first attempt finished, and until this page there was nowhere to look but
    the logs. `sync_state` has held the answer the whole time.
    """
    import datetime as dt

    app, _ = app_and_data
    ran = dt.datetime.now(dt.UTC) - dt.timedelta(days=3)
    with _catalogue_game(
        "backfilled",
        backfill={
            "source": "mtgjson",
            "upstream_stamp": "5.2.2+20260826",
            "last_run_at": ran,
            "rows_changed": 1842,
            "message": "2,904 daily prices read, 1,842 recorded",
        },
    ):
        body = _signed_in(app).get("/plugins").text

    assert "3 d ago" in body
    assert "2,904 daily prices read, 1,842 recorded" in body
    assert "5.2.2+20260826" in body, "the upstream build is what makes a re-run decidable"


def test_a_game_no_enricher_covers_is_not_reported_as_a_job_nobody_ran(app_and_data):
    """Pokemon has no past to recover, and that is a fact rather than a failure.

    MTGJSON is the only enricher and it is a Magic project. Printing "never"
    against every other game would read as a backlog, and send someone looking
    for a command that does not exist.
    """
    app, _ = app_and_data
    with _catalogue_game("pokemon"):
        body = _signed_in(app).get("/plugins").text

    assert "no installed enricher covers pokemon" in body
    assert "foilstack enrich --source mtgjson --game pokemon" not in body


def test_encoded_counts_only_vectors_the_configured_model_can_search(app_and_data):
    """A model swap leaves the old vectors in place and `search` cannot see them.

    Counting every row in `card_embeddings` would report a fully encoded
    catalogue that matching silently misses on — the exact failure the `model`
    column exists to make visible.
    """
    from foilstack import db
    from foilstack.config import get_settings

    app, _ = app_and_data
    with _catalogue_game("stalevectors") as card_id:
        session = db.session()
        session.add(
            db.CardEmbedding(
                card_id=card_id,
                embedding=[0.0] * db.EMBEDDING_DIM,
                model=get_settings().embed_model + "-previous",
            )
        )
        session.commit()
        session.close()
        body = _signed_in(app).get("/plugins").text

    # Scoped to this game by name. The assertion used to be "1 of 1 not
    # encoded", which is equally true of every other unencoded game in the
    # table — so it passed with the model filter deleted, which is the one
    # thing it exists to catch.
    assert "stalevectors: 1 of 1 cards not encoded" in body, (
        "a vector from a retired model must not count as encoded"
    )


def test_the_footer_says_never_rather_than_inventing_a_sync_that_did_not_happen(app_and_data):
    """`max(cards.updated_at)` is the freshness of the catalogue, not of its prices.

    With no `sync_state` row at all the footer used to fall back to it and
    report "synced just now" — on precisely the install where the claim is
    most wrong, because those cards hold the prices they were ingested with
    and every day that passes is history TCGCSV will not sell back.
    """
    app, _ = app_and_data
    with _catalogue_game("unsyncedgame"):
        body = _signed_in(app).get("/inventory").text

    assert "synced never" in body
    assert "synced just now" not in body, "an ingest is not a price sync"


def test_the_footer_reports_the_oldest_run_once_one_has_happened(app_and_data):
    """The fallback going away must not take the real figure with it.

    `min`, not `max`: the line makes a claim about every price on screen, so it
    has to be as old as the stalest game that has been synced at all.
    """
    import datetime as dt

    app, _ = app_and_data
    now = dt.datetime.now(dt.UTC)
    with (
        _catalogue_game(
            "freshgame",
            prices={
                "source": "tcgcsv",
                "last_run_at": now - dt.timedelta(hours=2),
                "rows_changed": 1,
            },
        ),
        _catalogue_game(
            "stalegame",
            prices={
                "source": "tcgcsv",
                "last_run_at": now - dt.timedelta(hours=9),
                "rows_changed": 1,
            },
        ),
    ):
        body = _signed_in(app).get("/inventory").text

    assert "synced 9 hr ago" in body, "the oldest synced game is the honest figure"
    assert "synced 2 hr ago" not in body


def _pricing_export(*rows: str) -> bytes:
    from foilstack import tcgplayer

    return (",".join(tcgplayer.HEADER) + "\r\n" + "".join(r + "\r\n" for r in rows)).encode()


def test_the_tcgplayer_round_trip_returns_the_sellers_own_sku_ids(app_and_data):
    """A pricing export up, the matching rows back with our numbers on.

    The end-to-end version of `tests/test_tcgplayer_match.py`: this one proves
    the route reaches the matcher with rows scoped to the signed-in account and
    a `source_name` that actually joins.
    """
    from sqlalchemy import select as sa_select

    from foilstack import db

    _fresh_line("t:roundtrip", "Ancestors Chosen", ["NM"])
    session = db.session()
    card = session.scalars(sa_select(db.Card).where(db.Card.source_id == "t:roundtrip")).one()
    card.game = "magic"
    card.set_name = "10th Edition"
    card.number = "1"
    # The cleaned spelling is in `name`; only this column can match their file.
    card.source_name = "Ancestor's Chosen"
    session.commit()
    session.close()

    upload = _pricing_export(
        '"4591","Magic","10th Edition","Ancestor\'s Chosen","","1","U","Near Mint",'
        '"0.16","","1.6000","0.1100","","0","",""'
    )
    response = _signed_in(app_and_data[0]).post(
        "/export/tcgplayer/match", files={"file": ("pricing.csv", upload, "text/csv")}
    )
    assert response.status_code == 200
    body = response.text
    assert '"4591"' in body, f"the SKU id is the point of the round trip: {body!r}"
    assert body.count("\r\n") == 2, "one header and one matched row"


def test_a_stranger_gets_no_rows_from_the_tcgplayer_round_trip(stranger, app_and_data):
    """The upload is theirs; the inventory it is matched against must not be.

    A route that took the file and matched it against everyone's stock would
    hand a stranger the whole install's positions, priced.
    """
    _fresh_line("t:notyours", "Abundance", ["NM"])
    upload = _pricing_export(
        '"4519","Magic","10th Edition","Abundance","","249","R","Near Mint",'
        '"1.68","","2.9000","1.4100","","0","",""'
    )
    response = stranger.post(
        "/export/tcgplayer/match", files={"file": ("pricing.csv", upload, "text/csv")}
    )
    assert response.status_code == 400, "an account with no stock has nothing to list"
    assert "Abundance" not in response.text


def test_the_wrong_tcgplayer_file_is_refused_with_a_reason(app_and_data):
    _fresh_line("t:wrongfile", "Abundance", ["NM"])
    response = _signed_in(app_and_data[0]).post(
        "/export/tcgplayer/match",
        files={"file": ("orders.csv", b"Order Number,Quantity\n1,2\n", "text/csv")},
    )
    assert response.status_code == 400
    assert "Export Filtered CSV" in response.text


def _run(client, line, **params):
    """The listing run for one card's copies and nothing else.

    A list value repeats the key, because `channel` is genuinely repeated in
    the query string and a run with two channels ticked is the state the
    button labels have to get right.
    """
    query = "".join(f"&id={i}" for i in line["item_ids"])
    for key, value in params.items():
        for one in value if isinstance(value, list) else [value]:
            query += f"&{key}={one}"
    return client.get("/listings?rule=market" + query).text


def test_the_mark_button_counts_cards_not_listings(app_and_data):
    """Two copies of one card are one marketplace listing and two cards.

    The button used to count the rows on screen, so a seller holding 41 cards
    across 37 listings was offered "Mark 37 as listed" beside a topbar badge
    reading 41 and an inventory screen reading 41. Nothing was broken except
    the only number that said what the button would do.
    """
    app, _ = app_and_data
    line = _fresh_line("t:countcards", "Count Me Twice", ["NM", "NM"])
    page = _run(_signed_in(app), line)

    assert "Mark 2 on TCGplayer" in page, "two cards, however many listings they make"
    assert "Mark 1 on TCGplayer" not in page


def test_everything_marked_leaves_nothing_to_mark(app_and_data):
    """The complaint that started this: all of it listed, still being asked."""
    app, _ = app_and_data
    line = _fresh_line("t:allmarked", "Already Listed", ["NM", "LP"])
    client = _signed_in(app)

    assert (
        client.post(
            "/api/listings/mark",
            json={"ids": line["item_ids"], "channels": ["tcgplayer"]},
        ).json()["marked"]
        == 2
    )

    page = _run(client, line)
    assert "All 2 listed on TCGplayer" in page
    assert "Mark 2 on" not in page, "there is nothing left to mark"


def test_a_second_channel_reopens_the_button(app_and_data):
    """Listed on TCGplayer is not listed on eBay, and the run knows which."""
    app, _ = app_and_data
    line = _fresh_line("t:twochannels", "Also On eBay", ["NM"])
    client = _signed_in(app)
    client.post(
        "/api/listings/mark",
        json={"ids": line["item_ids"], "channels": ["tcgplayer"]},
    )

    page = _run(client, line, channel="ebay")
    assert "Mark 1 on eBay" in page, "unlisted on eBay is still work to do"


def test_marking_a_second_channel_keeps_the_first(app_and_data):
    """A card on both marketplaces is on both.

    Overwriting the label said the seller had taken the TCGplayer listing
    down — which they never said. Saying it is `unmark`, which names the
    channel it removes rather than implying one from a second marking.
    """
    from foilstack import db

    app, _ = app_and_data
    line = _fresh_line("t:keepfirst", "On Both", ["NM"])
    client = _signed_in(app)
    for channel in ("tcgplayer", "ebay"):
        client.post(
            "/api/listings/mark",
            json={"ids": line["item_ids"], "channels": [channel]},
        )

    session = db.session()
    item = session.get(db.InventoryItem, line["item_ids"][0])
    assert item.listed_channels == "ebay, tcgplayer"
    session.close()


def test_a_half_marked_line_says_which_half(app_and_data):
    """One copy listed and one not is neither "listed" nor "ready".

    The line inherited whichever copy was read first, so the same two cards
    reported either state depending on row order.
    """
    app, _ = app_and_data
    line = _fresh_line("t:halfmarked", "Half Listed", ["NM", "NM"])
    client = _signed_in(app)
    client.post(
        "/api/listings/mark",
        json={"ids": line["item_ids"][:1], "channels": ["tcgplayer"]},
    )

    page = _run(client, line)
    assert "1 of 2 listed" in page
    assert "Mark 1 on TCGplayer" in page, "only the unmarked copy is left"


def test_unmarking_one_channel_leaves_the_other(app_and_data):
    """A card taken off eBay is still on TCGplayer.

    The mirror of the marking rule: removing the label wholesale would say the
    seller had ended both listings, and they named one.
    """
    from foilstack import db

    app, _ = app_and_data
    line = _fresh_line("t:unmarkone", "Off eBay Only", ["NM"])
    client = _signed_in(app)
    client.post(
        "/api/listings/mark",
        json={"ids": line["item_ids"], "channels": ["tcgplayer", "ebay"]},
    )
    assert (
        client.post(
            "/api/listings/unmark",
            json={"ids": line["item_ids"], "channels": ["ebay"]},
        ).json()["unmarked"]
        == 1
    )

    session = db.session()
    item = session.get(db.InventoryItem, line["item_ids"][0])
    assert item.listed_channels == "tcgplayer"
    assert item.listed == 1, "still on a marketplace, so still listed"
    assert item.listed_at is not None
    session.close()


def test_unmarking_the_last_channel_makes_the_card_ready_again(app_and_data):
    """Nowhere listed is not listed, and the run offers it again.

    The point of the feature: a card whose listing ended has to come back into
    the export, or the seller's next upload silently omits it forever.
    """
    from foilstack import db

    app, _ = app_and_data
    line = _fresh_line("t:unmarklast", "Back On The Shelf", ["NM"])
    client = _signed_in(app)
    client.post(
        "/api/listings/mark",
        json={"ids": line["item_ids"], "channels": ["tcgplayer"]},
    )
    client.post(
        "/api/listings/unmark",
        json={"ids": line["item_ids"], "channels": ["tcgplayer"]},
    )

    session = db.session()
    item = session.get(db.InventoryItem, line["item_ids"][0])
    assert item.listed == 0
    assert item.listed_channels is None
    assert item.listed_at is None, "a timestamp here dates a listing that is gone"
    session.close()

    page = _run(client, line)
    assert "Mark 1 on TCGplayer" in page, "it is work to do again"
    assert "Unmark" not in page, "nothing left on a picked channel"


def test_the_unmark_button_counts_only_what_is_on_a_picked_channel(app_and_data):
    """One copy listed of two, and the button says one — not two, not none."""
    app, _ = app_and_data
    line = _fresh_line("t:unmarkcount", "Half Off", ["NM", "NM"])
    client = _signed_in(app)
    client.post(
        "/api/listings/mark",
        json={"ids": line["item_ids"][:1], "channels": ["tcgplayer"]},
    )

    page = _run(client, line)
    assert "Unmark 1 on TCGplayer" in page
    # Both buttons live at once, and they are about different copies.
    assert "Mark 1 on TCGplayer" in page


def test_unmarking_ignores_a_channel_the_card_is_not_on(app_and_data):
    """Taking a card off a marketplace it was never on changes nothing.

    Reported as changed it would tell the seller a listing had come down, and
    the count in the job log is the only confirmation this screen gives.
    """
    app, _ = app_and_data
    line = _fresh_line("t:unmarkabsent", "Never On eBay", ["NM"])
    client = _signed_in(app)
    client.post(
        "/api/listings/mark",
        json={"ids": line["item_ids"], "channels": ["tcgplayer"]},
    )

    r = client.post(
        "/api/listings/unmark",
        json={"ids": line["item_ids"], "channels": ["ebay"]},
    )
    assert r.json()["unmarked"] == 0


def test_both_buttons_name_the_channels_they_act_on(app_and_data):
    """A count with no channel beside it is ambiguous once two are ticked.

    "Mark 1" against a row already reading "listed · tcgplayer" looks like a
    bug until you notice the eBay box — the number is about the channel you
    just ticked, and the button never said so.
    """
    app, _ = app_and_data
    line = _fresh_line("t:bothchannels", "Named On Both", ["NM"])
    client = _signed_in(app)
    client.post(
        "/api/listings/mark",
        json={"ids": line["item_ids"], "channels": ["tcgplayer"]},
    )

    page = _run(client, line, channel=["tcgplayer", "ebay"])
    assert "Mark 1 on TCGplayer, eBay" in page, "still to do on eBay"
    assert "Unmark 1 on TCGplayer, eBay" in page, "already on TCGplayer"

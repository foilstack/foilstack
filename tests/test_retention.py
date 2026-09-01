"""Confirming twice, what discarding actually frees, and surviving a restart.

Three rules that had nothing holding them up. One row in `inventory` is one
physical card, so confirming the same scan twice must not make two of them.
The storage quota tells a full account to discard something, which has to be
advice that works. And an import only runs inside the web process, so a job
that process was working on when it died has to be marked failed by the next
one — left active, it hangs the import screen on work nobody is doing.

All three need a real database: one is enforced partly by a unique index, one
counts bytes with a SQL sum, and one is a sweep over rows at startup. So this
builds its own, the way `test_isolation` and `test_cohort` do, and skips
cleanly where there is none.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, func, select, text

ADMIN_URL = os.getenv(
    "FOILSTACK_TEST_DATABASE_URL",
    "postgresql+psycopg://foilstack:foilstack@localhost:5434/foilstack",
)


@pytest.fixture(scope="module")
def app_and_data(tmp_path_factory):
    """A migrated throwaway database, one signed-in seller, one card."""
    name = f"foilstack_retention_{uuid.uuid4().hex[:8]}"
    try:
        engine = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT", future=True)
        with engine.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception as exc:  # noqa: BLE001 - no server, wrong password, anything
        pytest.skip(f"no Postgres for retention tests: {type(exc).__name__}")

    url = ADMIN_URL.rsplit("/", 1)[0] + "/" + name
    data_dir = tmp_path_factory.mktemp("data")
    os.environ.update(
        DATABASE_URL=url,
        FOILSTACK_DATA_DIR=str(data_dir),
        FOILSTACK_MULTI_USER="true",
        FOILSTACK_SECRET_KEY="retention-test-secret",
    )

    # Before alembic, not after — see the note in test_isolation.
    from foilstack.config import get_settings

    get_settings.cache_clear()

    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")

    from foilstack import db
    from foilstack.web import auth

    db.init(url)
    session = db.session()
    user = db.User(email="r@example.com", password_hash=auth.hash_password("a-long-password"))
    session.add(user)
    card = db.Card(source="t", source_id="t:1", name="Test Card", game="mtg", market=10.0)
    session.add(card)
    session.commit()
    ids = {"user": user.id, "card": card.id}
    session.close()

    from foilstack.web import app as web

    yield web.app, ids, data_dir

    get_settings.cache_clear()
    with create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT", future=True).connect() as conn:
        conn.execute(
            text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :n"),
            {"n": name},
        )
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))


@pytest.fixture
def client(app_and_data):
    from fastapi.testclient import TestClient

    app, _, _ = app_and_data
    c = TestClient(app)
    c.post("/login", data={"email": "r@example.com", "password": "a-long-password"})
    return c


def _scan(app_and_data, *, contents: bytes = b"pretend jpeg", status: str = "pending"):
    """A scan with a real file behind it, in its own job.

    Self-contained on purpose: every test here creates the scan it acts on, so
    no test depends on what an earlier one did to a shared row.
    """
    from foilstack import db

    _, ids, data_dir = app_and_data
    session = db.session()
    job = db.ImportJob(user_id=ids["user"], filename="a.zip", status="done", total=1, processed=1)
    session.add(job)
    session.commit()

    stored = f"{job.id}/card.jpg"
    path = data_dir / "scans" / stored
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)

    scan = db.Scan(
        job_id=job.id,
        user_id=ids["user"],
        filename="card.jpg",
        stored_path=stored,
        size_bytes=len(contents),
        status=status,
    )
    session.add(scan)
    session.commit()
    scan_id = scan.id
    session.close()
    return scan_id, path


def _inventory_rows(scan_id: int) -> int:
    from foilstack import db

    session = db.session()
    try:
        return session.scalar(
            select(func.count())
            .select_from(db.InventoryItem)
            .where(db.InventoryItem.scan_id == scan_id)
        )
    finally:
        session.close()


# --- one scan is one card -------------------------------------------------


def test_confirming_twice_makes_one_card(client, app_and_data):
    """The replay that shipped duplicates: same request, sent again."""
    _, ids, _ = app_and_data
    scan_id, _ = _scan(app_and_data)
    body = {"card_id": ids["card"], "condition": "NM", "finish": "nonfoil"}

    first = client.post(f"/api/scans/{scan_id}/confirm", data=body)
    second = client.post(f"/api/scans/{scan_id}/confirm", data=body)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["created"] is True
    # Answered, not silently ignored: the caller can tell the second request
    # changed nothing, which is what makes this idempotent rather than lossy.
    assert second.json()["created"] is False
    assert _inventory_rows(scan_id) == 1


def test_the_same_scan_twice_in_one_commit_makes_one_card(client, app_and_data):
    """The queue builds its rows from the DOM, so a re-render can send a
    duplicate — and the unique index would fail the entire commit."""
    _, ids, _ = app_and_data
    scan_id, _ = _scan(app_and_data)
    row = {"scan_id": scan_id, "card_id": ids["card"], "condition": "NM", "finish": "nonfoil"}

    out = client.post("/api/scans/commit", json={"rows": [row, dict(row)]})

    assert out.status_code == 200
    assert out.json()["committed"] == 1
    assert _inventory_rows(scan_id) == 1


def test_the_database_refuses_a_second_row_for_one_scan(client, app_and_data):
    """The half of the guard that survives two requests racing.

    The route check reads before it writes, so it cannot see a row another
    transaction has not committed yet. This asserts the index is really there,
    by going around the route entirely.
    """
    from sqlalchemy.exc import IntegrityError

    from foilstack import db

    _, ids, _ = app_and_data
    scan_id, _ = _scan(app_and_data)
    client.post(
        f"/api/scans/{scan_id}/confirm",
        data={"card_id": ids["card"], "condition": "NM", "finish": "nonfoil"},
    )

    session = db.session()
    session.add(
        db.InventoryItem(user_id=ids["user"], card_id=ids["card"], scan_id=scan_id, condition="NM")
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
    session.close()
    assert _inventory_rows(scan_id) == 1


def test_rows_with_no_scan_are_not_limited_to_one(app_and_data):
    """The index is partial. A row whose scan was deleted holds NULL, and any
    number of those may exist — they say nothing about how many cards there
    are."""
    from foilstack import db

    _, ids, _ = app_and_data
    session = db.session()
    session.add_all(
        [
            db.InventoryItem(user_id=ids["user"], card_id=ids["card"], condition="NM"),
            db.InventoryItem(user_id=ids["user"], card_id=ids["card"], condition="NM"),
        ]
    )
    session.commit()  # must not raise
    session.close()


# --- discarding frees what it says it frees -------------------------------


def test_discarding_releases_the_quota_and_the_file(client, app_and_data):
    from foilstack import db, importing

    _, ids, _ = app_and_data
    scan_id, path = _scan(app_and_data, contents=b"x" * 4096)

    session = db.session()
    before = importing.usage_bytes(session, ids["user"])
    session.close()

    assert client.post(f"/api/scans/{scan_id}/discard").status_code == 200

    session = db.session()
    after = importing.usage_bytes(session, ids["user"])
    session.close()

    assert before - after == 4096
    assert not path.exists()


def test_discarding_keeps_the_row_and_its_evidence(client, app_and_data):
    """The candidate list outlives the photograph. It is the record of why the
    scan was rejected, and it costs almost nothing to keep."""
    from foilstack import db

    _, ids, _ = app_and_data
    scan_id, _ = _scan(app_and_data)
    session = db.session()
    session.add(db.Candidate(scan_id=scan_id, card_id=ids["card"], score=0.71, rank=0))
    session.commit()
    session.close()

    client.post(f"/api/scans/{scan_id}/discard")

    session = db.session()
    scan = session.get(db.Scan, scan_id)
    candidates = session.scalars(select(db.Candidate).where(db.Candidate.scan_id == scan_id)).all()
    assert scan is not None
    assert scan.status == "discarded"
    assert scan.size_bytes == 0
    assert len(candidates) == 1
    session.close()


def test_discard_all_frees_every_row_it_discarded(client, app_and_data):
    from foilstack import db, importing

    _, ids, _ = app_and_data
    first, path_a = _scan(app_and_data, contents=b"a" * 1000)
    second, path_b = _scan(app_and_data, contents=b"b" * 2000)

    session = db.session()
    before = importing.usage_bytes(session, ids["user"])
    session.close()

    out = client.post("/api/scans/discard-all", json={"scan_ids": [first, second]})
    assert out.status_code == 200

    session = db.session()
    after = importing.usage_bytes(session, ids["user"])
    session.close()

    assert before - after == 3000
    assert not path_a.exists()
    assert not path_b.exists()


def test_a_confirmed_card_keeps_its_photograph(client, app_and_data):
    """A scan an inventory row still points at is not purged, whatever its
    status says. The inventory table renders that image."""
    from foilstack import db, importing

    _, ids, _ = app_and_data
    scan_id, path = _scan(app_and_data, contents=b"y" * 512)
    client.post(
        f"/api/scans/{scan_id}/confirm",
        data={"card_id": ids["card"], "condition": "NM", "finish": "nonfoil"},
    )

    # Discarding a confirmed scan is not something the queue offers, so reach
    # past it: the guard has to hold wherever the call comes from.
    session = db.session()
    from foilstack.config import get_settings

    scan = session.get(db.Scan, scan_id)
    released = importing.purge_scans(session, get_settings(), [scan])
    session.commit()
    session.close()

    assert released == 0
    assert path.exists()


def test_a_full_account_can_upload_after_discarding(client, app_and_data, monkeypatch):
    """The whole point of the fix, stated the way the 413 states it.

    The quota message tells a full account to discard some scans first. This
    is that instruction, followed — and before the fix the second upload was
    refused exactly like the first, because nothing about discarding moved the
    number the check reads.
    """
    from foilstack.config import get_settings

    # Over a 1 MB ceiling on its own.
    scan_id, _ = _scan(app_and_data, contents=b"z" * (1024 * 1024 + 1))

    monkeypatch.setenv("FOILSTACK_MAX_ACCOUNT_MB", "1")
    get_settings.cache_clear()
    try:
        archive = {"archive": ("a.zip", b"PK\x03\x04not-a-real-zip", "application/zip")}
        blocked = client.post("/api/import", files=archive)
        assert blocked.status_code == 413
        assert "discard some scans first" in blocked.text

        assert client.post(f"/api/scans/{scan_id}/discard").status_code == 200

        # Not asserting the upload succeeds — the payload is not a real archive
        # and the import runs in the background. What matters is that the quota
        # is no longer what stops it.
        assert client.post("/api/import", files=archive).status_code != 413
    finally:
        monkeypatch.delenv("FOILSTACK_MAX_ACCOUNT_MB", raising=False)
        get_settings.cache_clear()


def _job(app_and_data, *, status: str, total: int = 0, processed: int = 0) -> int:
    """One import job in a given state. Self-contained, like `_scan`."""
    from foilstack import db

    _, ids, _ = app_and_data
    session = db.session()
    job = db.ImportJob(
        user_id=ids["user"],
        filename="stack.zip",
        status=status,
        total=total,
        processed=processed,
    )
    session.add(job)
    session.commit()
    job_id = job.id
    session.close()
    return job_id


def _job_row(job_id: int):
    from foilstack import db

    session = db.session()
    try:
        job = session.get(db.ImportJob, job_id)
        return job.status, job.message
    finally:
        session.close()


def _reap():
    from foilstack import db, importing

    session = db.session()
    try:
        return importing.reap_interrupted_jobs(session)
    finally:
        session.close()


@pytest.mark.parametrize("status", ["pending", "matching", "grouping"])
def test_reap_fails_every_active_status(app_and_data, status):
    """All three, not just the two the import screen used to know about.

    `grouping` is the one that was missed everywhere, and it fails differently
    from the others: the screen never called it active, so a job killed during
    the cohort pass vanished instead of hanging.
    """
    job_id = _job(app_and_data, status=status, total=10, processed=10)

    assert _reap() >= 1

    state, message = _job_row(job_id)
    assert state == "failed"
    assert "restart" in message


def test_reap_leaves_finished_jobs_alone(app_and_data):
    """A done job is not a corpse, and a failed one keeps the reason it failed."""
    done = _job(app_and_data, status="done", total=4, processed=4)
    already = _job(app_and_data, status="failed", total=4, processed=1)

    from foilstack import db

    session = db.session()
    session.get(db.ImportJob, already).message = "encoder unreachable"
    session.commit()
    session.close()

    _reap()

    assert _job_row(done) == ("done", None)
    assert _job_row(already) == ("failed", "encoder unreachable")


def test_reap_message_says_what_landed(app_and_data):
    """The counts, because the queue cannot answer "how much of it arrived".

    Matched scans sit in the queue among every other pending scan, so a seller
    reading it back cannot tell which half of their upload is missing.
    """
    partial = _job(app_and_data, status="matching", total=300, processed=47)
    nothing = _job(app_and_data, status="pending", total=0, processed=0)
    grouped = _job(app_and_data, status="grouping", total=300, processed=300)

    _reap()

    assert "47 of 300" in _job_row(partial)[1]

    # Nothing was extracted, so there is no partial state to explain — just
    # do it again.
    assert "before any scan was matched" in _job_row(nothing)[1]

    # Every scan matched here. Telling this seller to re-upload would be
    # telling them to duplicate all three hundred.
    grouped_message = _job_row(grouped)[1]
    assert "upload" not in grouped_message
    assert "game or set" in grouped_message


def test_import_screen_stops_polling_a_reaped_job(app_and_data, client):
    """The behaviour the whole change is for.

    Before: a killed job stayed `matching`, the screen picked it as `active`
    and the browser polled it every 700ms forever under a sweeping bar. The
    assertion is on the poll call rather than on any markup around it, because
    starting that loop is the thing that was wrong.
    """
    job_id = _job(app_and_data, status="matching", total=300, processed=47)

    assert f"poll({job_id})" in client.get("/app").text

    _reap()

    body = client.get("/app").text
    assert f"poll({job_id})" not in body
    # And the seller is told why, rather than the job simply going quiet.
    assert "47 of 300" in body


def test_import_screen_follows_a_grouping_job(app_and_data, client):
    """The other half of the same omission.

    The screen's status list left `grouping` out, so a job in the cohort pass
    was not `active` and the page stopped following it — the seller watched
    the bar reach the end and then saw nothing more, on a job still working.
    """
    job_id = _job(app_and_data, status="grouping", total=300, processed=300)
    try:
        assert f"poll({job_id})" in client.get("/app").text
    finally:
        _reap()

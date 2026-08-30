"""A batch that says it is one game or one set gets held to it.

The estimator is unit-tested against fakes because it is arithmetic over
candidate lists and needs nothing else. The pass that acts on it is not: it
writes candidates, moves scans, accepts some and refuses to accept others, and
a fake session that agreed with all of it would prove none of it. So that half
runs against a real database, and skips when there is none — check the skip
count before believing a green run, as with `test_isolation.py`.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from foilstack.importing import _cohort_key, _cohort_votes


class _Card:
    def __init__(self, game, set_name):
        self.game, self.set_name = game, set_name


def test_the_key_is_the_game_alone_unless_the_set_was_asked_for():
    card = _Card("pokemon", "Base Set")
    assert _cohort_key(card, same_set=False) == ("pokemon",)
    assert _cohort_key(card, same_set=True) == ("pokemon", "Base Set")


def test_a_set_key_carries_its_game():
    """Half the catalogues here have something called "Promo"."""
    a = _Card("mtg", "Promo")
    b = _Card("pokemon", "Promo")
    assert _cohort_key(a, same_set=True) != _cohort_key(b, same_set=True)


def test_the_batchs_game_wins_over_a_scattering_of_others():
    cards = {
        1: _Card("pokemon", "Base Set"),
        2: _Card("pokemon", "Jungle"),
        3: _Card("mtg", "Alpha"),
    }
    pool = {
        1: [(1, 0.98), (3, 0.71)],
        2: [(2, 0.97), (3, 0.70)],
        3: [(3, 0.93), (1, 0.72)],
    }
    votes = _cohort_votes(pool, cards, same_set=False)
    assert max(votes, key=lambda k: votes[k]) == ("pokemon",)


def test_a_set_that_rarely_comes_first_still_wins_on_being_everywhere():
    """The case plurality-of-top-matches gets wrong, and the reason it is not
    the rule.

    Four scans of Magic reprints. Each one's *best* match lands in a different
    set — so no set has more than one first place — but every scan also has the
    set the seller actually opened somewhere in its list. Counting first places
    picks whichever set got lucky; scoring presence picks the one they opened.
    """
    cards = {
        1: _Card("mtg", "Opened"),
        2: _Card("mtg", "Alpha"),
        3: _Card("mtg", "Beta"),
        4: _Card("mtg", "Unlimited"),
        5: _Card("mtg", "Revised"),
    }
    pool = {
        1: [(2, 0.96), (1, 0.95)],
        2: [(3, 0.96), (1, 0.95)],
        3: [(4, 0.96), (1, 0.95)],
        4: [(5, 0.96), (1, 0.95)],
    }
    votes = _cohort_votes(pool, cards, same_set=True)
    assert max(votes, key=lambda k: votes[k]) == ("mtg", "Opened")


def test_one_scan_votes_once_for_a_set_however_many_printings_it_matched():
    """A common creature has eight near-identical printings and a big core set
    can hold seven of them. Counting candidates would let that set win on one
    scan's worth of evidence."""
    cards = {n: _Card("mtg", "Core") for n in range(1, 8)}
    cards[8] = _Card("mtg", "Small")
    crowded = {1: [(n, 0.9) for n in range(1, 8)]}
    votes = _cohort_votes(crowded, cards, same_set=True)
    assert votes[("mtg", "Core")] == pytest.approx(0.9)


# --------------------------------------------------------------------------
# The pass itself, against a real database.

ADMIN_URL = os.getenv(
    "FOILSTACK_TEST_DATABASE_URL",
    "postgresql+psycopg://foilstack:foilstack@localhost:5434/foilstack",
)


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    """A migrated throwaway database with a catalogue in two games."""
    name = f"foilstack_cohort_{uuid.uuid4().hex[:8]}"
    try:
        engine = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT", future=True)
        with engine.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception as exc:  # noqa: BLE001 - no server, wrong password, anything
        pytest.skip(f"no Postgres for cohort tests: {type(exc).__name__}")

    url = ADMIN_URL.rsplit("/", 1)[0] + "/" + name
    os.environ["DATABASE_URL"] = url
    os.environ["FOILSTACK_DATA_DIR"] = str(tmp_path_factory.mktemp("data"))

    # Cleared before alembic runs, not after: `migrations/env.py` reads
    # `get_settings()`, so a settings object cached by an earlier import sends
    # this migration at the developer's own database.
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
    user = db.User(email="c@example.com", password_hash=auth.hash_password("a-long-password"))
    session.add(user)
    session.commit()

    cards = {
        "base_a": db.Card(
            source="t",
            source_id="t:1",
            name="Alakazam",
            game="pokemon",
            set_name="Base Set",
            number="1",
            market=40.0,
        ),
        "base_b": db.Card(
            source="t",
            source_id="t:2",
            name="Blastoise",
            game="pokemon",
            set_name="Base Set",
            number="2",
            market=50.0,
        ),
        "jungle": db.Card(
            source="t",
            source_id="t:3",
            name="Alakazam",
            game="pokemon",
            set_name="Jungle",
            number="9",
            market=6.0,
        ),
        "mtg": db.Card(
            source="t",
            source_id="t:4",
            name="Shivan Dragon",
            game="mtg",
            set_name="Alpha",
            number="3",
            market=900.0,
        ),
    }
    session.add_all(cards.values())
    session.commit()
    ids = {k: c.id for k, c in cards.items()}
    session.close()

    yield url, user.id, ids

    get_settings.cache_clear()
    with create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT", future=True).connect() as conn:
        conn.execute(
            text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :n"),
            {"n": name},
        )
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))


def _batch(session, user_id, pool_by_index, *, same_game=False, same_set=True, auto_accept=0.94):
    """One import job with a scan per entry in `pool_by_index`.

    Each entry is the hit list that scan's search returned; the stored
    candidates are its first five, exactly as `_match_one` would have written
    them. Returns the job and the pool `apply_cohort` expects.
    """
    from foilstack import db
    from foilstack.importing import CANDIDATE_COUNT

    job = db.ImportJob(
        user_id=user_id,
        filename="batch.zip",
        status="grouping",
        same_game=same_game or same_set,
        same_set=same_set,
        auto_accept=auto_accept,
        default_condition="NM",
        default_finish="nonfoil",
    )
    session.add(job)
    session.commit()

    pool = {}
    for n, hits in enumerate(pool_by_index):
        scan = db.Scan(
            job_id=job.id,
            user_id=user_id,
            filename=f"{n}.jpg",
            stored_path=f"{job.id}/{n}.jpg",
            status="pending",
            best_score=hits[0][1],
        )
        session.add(scan)
        session.commit()
        for rank, (card_id, score) in enumerate(hits[:CANDIDATE_COUNT]):
            session.add(db.Candidate(scan_id=scan.id, card_id=card_id, score=score, rank=rank))
        session.commit()
        pool[scan.id] = hits
    return job, pool


def test_a_stray_game_is_pulled_back_to_the_batchs_own(seeded):
    from foilstack import db
    from foilstack.config import get_settings
    from foilstack.importing import apply_cohort

    _, user_id, ids = seeded
    session = db.session()
    job, pool = _batch(
        session,
        user_id,
        [
            [(ids["base_a"], 0.97)],
            [(ids["base_b"], 0.96)],
            # This one's best match is a Magic card; the Pokemon answer is
            # behind it and is what the batch says it should be.
            [(ids["mtg"], 0.93), (ids["base_a"], 0.88)],
        ],
        same_game=True,
        same_set=False,
    )
    apply_cohort(session, job, pool, get_settings())

    assert job.cohort_game == "pokemon"
    assert job.cohort_set is None
    moved = session.get(db.Scan, max(pool))
    assert moved.cohort_card_id == ids["base_a"]
    session.close()


def test_a_moved_scan_is_never_auto_accepted(seeded):
    """It scores far above the threshold and beats its runner-up outright. The
    reason it still goes to review is that a guess about the neighbours decided
    it, and that is a reason to show somebody, not a reason to skip them."""
    from foilstack import db
    from foilstack.config import get_settings
    from foilstack.importing import apply_cohort

    _, user_id, ids = seeded
    session = db.session()
    job, pool = _batch(
        session,
        user_id,
        [
            [(ids["base_a"], 0.99)],
            [(ids["base_b"], 0.99)],
            [(ids["mtg"], 0.995), (ids["base_a"], 0.60)],
        ],
        same_game=True,
        same_set=False,
    )
    apply_cohort(session, job, pool, get_settings())

    stray = session.get(db.Scan, max(pool))
    assert stray.status == "pending"
    assert stray.auto_accepted == 0
    # The two that agreed with the batch were decided on their own merits.
    kept = session.get(db.Scan, min(pool))
    assert kept.status == "confirmed"
    session.close()


def test_a_scan_with_nothing_conforming_keeps_the_encoders_answer(seeded):
    from foilstack import db
    from foilstack.config import get_settings
    from foilstack.importing import apply_cohort

    _, user_id, ids = seeded
    session = db.session()
    job, pool = _batch(
        session,
        user_id,
        [
            [(ids["base_a"], 0.97)],
            [(ids["base_b"], 0.96)],
            [(ids["mtg"], 0.95)],
        ],
    )
    apply_cohort(session, job, pool, get_settings())

    stranded = session.get(db.Scan, max(pool))
    assert stranded.cohort_card_id is None
    assert stranded.status == "pending"
    assert "nothing in it" in (job.message or "")
    session.close()


def test_a_pick_from_below_the_stored_five_gets_a_candidate_row(seeded):
    """The queue reads scores off candidates, so a pick from deeper in the
    search would otherwise render with no score and no way back to it."""
    from foilstack import db
    from foilstack.config import get_settings
    from foilstack.importing import apply_cohort

    _, user_id, ids = seeded
    session = db.session()
    deep = [(ids["mtg"], 0.99)] + [(ids["jungle"], 0.98 - n / 100) for n in range(5)]
    deep.append((ids["base_a"], 0.80))
    job, pool = _batch(
        session,
        user_id,
        [
            [(ids["base_a"], 0.97)],
            [(ids["base_b"], 0.96)],
            deep,
        ],
    )
    apply_cohort(session, job, pool, get_settings())

    scan = session.get(db.Scan, max(pool))
    session.refresh(scan)
    assert scan.cohort_card_id == ids["base_a"]
    written = [c for c in scan.candidates if c.card_id == ids["base_a"]]
    assert len(written) == 1
    # Its real place in the search, not an index appended after the stored five.
    assert written[0].rank == 6
    assert written[0].score == pytest.approx(0.80)
    session.close()


def test_the_same_batch_settles_the_same_way_twice(seeded):
    """Two cohorts tied on score are broken by name, so a re-import does not
    quietly land on the other one."""
    from foilstack import db
    from foilstack.config import get_settings
    from foilstack.importing import apply_cohort

    _, user_id, ids = seeded
    settled = set()
    for _ in range(2):
        session = db.session()
        job, pool = _batch(
            session,
            user_id,
            [[(ids["base_a"], 0.90)], [(ids["jungle"], 0.90)]],
        )
        apply_cohort(session, job, pool, get_settings())
        settled.add(job.cohort_set)
        session.close()
    assert len(settled) == 1

"""Which upload the review queue opens, when several are waiting.

The screen lists every upload that has something waiting and renders the cards
of exactly one of them, so this is the only decision about what gets built.
The bug behind the rule it replaced was the other kind: a four-hundred-row cap
taken from the newest end while the screen presented the oldest end first, so
the queue served the front of the backlog twenty cards at a time and every
count on the page — the section heading included — was computed from the
survivors and said nothing about the rest. Nothing is held back now; a batch
is either open or shut, and a shut one still states its size.
"""

from __future__ import annotations

from foilstack.web.routes.scans import _open_job


def _waiting(*job_ids: int) -> list[dict]:
    return [{"job_id": job_id, "cards": 5} for job_id in job_ids]


def test_nothing_waiting_opens_nothing():
    assert _open_job(None, None, []) is None


def test_the_newest_upload_opens_by_default():
    """An import returns the seller to this page. A page whose cards are all
    some older batch's reads as an import that failed — and the older ones are
    a click away on the same screen, which is what makes that safe."""
    assert _open_job(None, None, _waiting(1, 2, 3)) == 3


def test_a_batch_asked_for_by_name_wins():
    assert _open_job(1, None, _waiting(1, 2, 3)) == 1


def test_the_batch_this_browser_had_open_comes_back():
    assert _open_job(None, 2, _waiting(1, 2, 3)) == 2


def test_asking_outranks_remembering():
    assert _open_job(1, 2, _waiting(1, 2, 3)) == 1


def test_clearing_a_batch_advances_to_the_next_one_down_the_queue():
    """What a cleared batch looks like from here: the last row is confirmed,
    the page reloads, and the id in hand names an upload that is finished.

    Falling back to the newest would throw a seller working through a backlog
    to the far end of it on every batch they finished.
    """
    assert _open_job(2, None, _waiting(1, 3, 4)) == 3


def test_clearing_the_newest_batch_falls_back_to_the_newest_left():
    assert _open_job(9, None, _waiting(1, 2)) == 2


def test_an_id_from_another_account_cannot_open_anything():
    """The ids are already scoped to the account, so one that names nothing
    waiting is treated as a batch that has been cleared — never as a reason to
    render rows the query did not return."""
    assert _open_job(999, None, _waiting(1, 2)) == 2

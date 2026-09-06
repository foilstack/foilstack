"""Which uploads the review queue renders when more is waiting than fits.

The rule is that a section is whole. The bug these are against was the other
kind: a row cap taken from the newest end while the screen presented the
oldest end first, so the queue served the front of the backlog twenty cards at
a time and every count on the page — the section heading included — was
computed from the survivors and said nothing about the rest.
"""

from __future__ import annotations

from foilstack.web.routes.scans import QUEUE_ROWS, _queue_jobs


def test_nothing_waiting_renders_nothing():
    assert _queue_jobs([]) == []


def test_everything_fits():
    waiting = [(1, 50), (2, 50), (3, 50)]
    assert _queue_jobs(waiting) == [1, 2, 3]


def test_the_oldest_uploads_are_kept_and_kept_whole():
    """The real shape of the bug: nine batches, the oldest one truncated.

    380 rows in the eight newest and 50 in the oldest is 430 against a 400
    budget. The old rule showed twenty cards of that oldest batch under a
    heading reading `20 cards`; the batch is now either all there or held
    back with a number beside it.
    """
    waiting = [
        (55, 50),
        (56, 49),
        (57, 48),
        (58, 45),
        (59, 46),
        (60, 49),
        (61, 48),
        (62, 50),
        (63, 45),
    ]
    shown = _queue_jobs(waiting)
    assert 55 in shown, "the front of the backlog is what the screen sends you to work"
    for job_id in shown:
        assert job_id in dict(waiting)
    rendered = sum(n for job_id, n in waiting if job_id in shown)
    assert rendered <= QUEUE_ROWS


def test_the_newest_upload_is_always_rendered():
    """An import returns the seller to this page. A page with no sign of it
    reads as an import that failed."""
    waiting = [(1, QUEUE_ROWS), (2, 30)]
    # The budget is already spent by the older batch, so it is the one held
    # back — the newest is in regardless of what that costs.
    assert _queue_jobs(waiting) == [2]


def test_one_upload_larger_than_the_budget_is_still_whole():
    waiting = [(1, 40), (2, QUEUE_ROWS * 2)]
    assert _queue_jobs(waiting) == [2], "the budget yields to the whole section, not the other way"


def test_a_batch_that_does_not_fit_stops_the_list_rather_than_being_skipped():
    """Stepping over a big batch to reach a smaller one behind it would put
    the screen out of the order the seller is working in."""
    waiting = [(1, 10), (2, QUEUE_ROWS), (3, 10), (4, 10)]
    assert _queue_jobs(waiting) == [1, 4]


def test_held_back_uploads_are_the_middle():
    waiting = [(1, 300), (2, 300), (3, 300), (4, 20)]
    shown = _queue_jobs(waiting)
    assert shown == [1, 4]
    assert [j for j, _ in waiting if j not in shown] == [2, 3]

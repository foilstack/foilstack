"""The guards that only matter once strangers can reach the server.

Each of these was written by breaking the thing first: the test is here
because the behaviour it asserts did not exist, and it fails against the
version of the application that shipped without it.
"""

from __future__ import annotations

import pytest

from foilstack.web import ratelimit


def test_limiter_lets_the_budget_through_then_refuses():
    limiter = ratelimit.Limiter(limit=3, window=60)
    for _ in range(3):
        assert limiter.check("k") == 0.0
        limiter.record("k")
    assert limiter.check("k") > 0


def test_limiter_forgets_a_key_after_the_window():
    limiter = ratelimit.Limiter(limit=1, window=60)
    limiter.record("k")
    assert limiter.check("k") > 0
    # Rather than sleeping a minute, move the window's start into the past.
    started, count = limiter._hits["k"]
    limiter._hits["k"] = (started - 61, count)
    assert limiter.check("k") == 0.0


def test_a_success_clears_the_budget():
    """Four typos then the right password must not cost the rest of the day."""
    limiter = ratelimit.Limiter(limit=5, window=60)
    for _ in range(4):
        limiter.record("someone@example.com")
    limiter.reset("someone@example.com")
    assert limiter.check("someone@example.com") == 0.0


def test_checking_does_not_itself_spend_an_attempt():
    """Otherwise the refusal page is a way to keep someone locked out."""
    limiter = ratelimit.Limiter(limit=2, window=60)
    for _ in range(50):
        limiter.check("k")
    limiter.record("k")
    assert limiter.check("k") == 0.0


def test_tracking_is_bounded():
    """The number of addresses that can reach a login form is not ours to
    decide, so the structure that counts them has to have a ceiling."""
    limiter = ratelimit.Limiter(limit=1, window=60)
    for i in range(ratelimit.MAX_TRACKED + 500):
        limiter.record(f"key-{i}")
    assert len(limiter._hits) <= ratelimit.MAX_TRACKED


@pytest.mark.parametrize("seconds,expected", [(1, "1 minute"), (61, "2 minutes"), (900, "15 min")])
def test_the_refusal_names_a_wait(seconds, expected):
    assert expected in ratelimit.wait_message(seconds)

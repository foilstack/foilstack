"""The import screen's match panel, remembered between batches.

A seller works through a shelf of boxes one zip at a time, and every finished
import reloads the screen. These are the guards on what comes back: the panel
has to return the seller's own answers, and it has to survive a cookie that a
browser held for a year and something else edited.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from starlette.requests import Request

from foilstack.web.routes.scans import _match_prefs

THRESHOLDS = [0.88, 0.92, 0.94, 0.96]
SETTINGS = SimpleNamespace(auto_accept=0.94)


def _prefs(cookie: str | None, user_id: int = 7):
    headers = []
    if cookie is not None:
        headers.append((b"cookie", f"foilstack_match_{user_id}={cookie}".encode()))
    request = Request({"type": "http", "headers": headers})
    return _match_prefs(request, user_id, SETTINGS, THRESHOLDS)


def test_no_cookie_is_the_shipped_default():
    """Off, not the configured threshold. A browser that has never been here
    must not auto-accept anything."""
    assert _prefs(None) == {
        "cond": "NM",
        "finish": "nonfoil",
        "thr": None,
        "same_game": False,
        "same_set": False,
    }


def test_every_answer_comes_back():
    assert _prefs("cond:LP,finish:foil,thr:0.92,game:1,set:1") == {
        "cond": "LP",
        "finish": "foil",
        "thr": 0.92,
        "same_game": True,
        "same_set": True,
    }


def test_another_accounts_cookie_is_not_read():
    """One browser, two accounts. The condition is somebody else's answer."""
    assert _prefs("cond:HP,finish:foil", user_id=7)["cond"] == "HP"
    request = Request({"type": "http", "headers": [(b"cookie", b"foilstack_match_7=cond:HP")]})
    assert _match_prefs(request, 8, SETTINGS, THRESHOLDS)["cond"] == "NM"


@pytest.mark.parametrize(
    "cookie",
    [
        "cond:XX",  # not a condition
        "cond:",  # empty
        "cond",  # no separator at all
        "garbage",
        "cond:NM;finish:foil",  # the wrong separator, so one unknown field
    ],
)
def test_a_condition_that_is_not_one_falls_back(cookie):
    assert _prefs(cookie)["cond"] == "NM"


def test_one_bad_field_does_not_cost_the_others():
    """Field at a time, because a panel that resets four answers to punish a
    fifth is worse than the bug this whole feature fixes."""
    prefs = _prefs("cond:ZZ,finish:foil,thr:0.88,game:1,set:0")
    assert prefs["cond"] == "NM"
    assert (prefs["finish"], prefs["thr"], prefs["same_game"]) == ("foil", 0.88, True)


def test_off_comes_back_as_off():
    assert _prefs("cond:LP,thr:off")["thr"] is None


@pytest.mark.parametrize("raw", ["thr:1.5", "thr:0.5", "thr:abc", "thr:", "cond:LP"])
def test_a_threshold_the_screen_does_not_offer_is_off(raw):
    """It has to be one of the chips, and everything else is Off rather than
    some other number. A remembered 50% would auto-accept under a threshold
    with no chip painted on to admit to it."""
    assert _prefs(raw)["thr"] is None


def test_a_configured_threshold_off_the_chip_grid_still_lands_on_a_chip():
    """The rounding has to agree with the grid, or a seller who picks the
    configured chip gets Off back on the next import."""
    settings = SimpleNamespace(auto_accept=0.945)
    request = Request({"type": "http", "headers": [(b"cookie", b"foilstack_match_7=thr:0.945")]})
    assert _match_prefs(request, 7, settings, THRESHOLDS)["thr"] == 0.94


def test_a_set_cohort_always_carries_its_game():
    """A set belongs to one game. The screen widens the pair on click and the
    import route widens it again; a cookie is the third way in and cannot be
    the one that gets through with half of it."""
    prefs = _prefs("game:0,set:1")
    assert prefs["same_game"] and prefs["same_set"]


def test_the_cohort_stays_off_unless_the_cookie_says_otherwise():
    """The one setting that overrules a confident match. Anything short of a
    seller's own tick leaves it alone."""
    for cookie in [None, "cond:LP", "game:0,set:0", "game:true,set:yes"]:
        prefs = _prefs(cookie)
        assert not prefs["same_game"] and not prefs["same_set"]

"""A short, in-memory log of what one account just did.

Deliberately not persisted and deliberately tiny. Its job is to answer "did
that button do anything" in the second after you press it — the same question
the terminal answers when you run the CLI. Anything worth keeping longer than
a process lifetime is already a row in the database.

Keyed by account, and that is not a detail. The messages name filenames, SKUs,
row counts and export sizes; every one of those is somebody's business data.
A single process-wide log would read fine on a self-hosted install and hand a
hosted seller's activity to whoever loaded the listings page next.

Memory is bounded on both axes: a fixed number of entries per account, and a
fixed number of accounts, the least recently active being dropped first. An
unbounded dict keyed by user id is a slow leak on a public deployment, where
the number of accounts is decided by strangers.
"""

from __future__ import annotations

import datetime as dt
from collections import OrderedDict, deque

# Per account. Twelve is about one screen of the panel that renders it.
MAX_ENTRIES = 12

# How many accounts keep a log at once. Well past a busy moment on a shared
# deployment, and small enough that the whole structure stays trivial.
MAX_ACCOUNTS = 256

_EVENTS: OrderedDict[int, deque[dict[str, str]]] = OrderedDict()


def add(user_id: int, message: str) -> None:
    events = _EVENTS.get(user_id)
    if events is None:
        events = deque(maxlen=MAX_ENTRIES)
        _EVENTS[user_id] = events
        while len(_EVENTS) > MAX_ACCOUNTS:
            _EVENTS.popitem(last=False)
    _EVENTS.move_to_end(user_id)
    events.appendleft({"t": dt.datetime.now().strftime("%H:%M:%S"), "msg": message})


def entries(user_id: int) -> list[dict[str, str]]:
    return list(_EVENTS.get(user_id, ()))


def forget(user_id: int) -> None:
    """Drop an account's log. Called when its owner signs out."""
    _EVENTS.pop(user_id, None)

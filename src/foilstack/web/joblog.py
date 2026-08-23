"""A short, in-memory log of what the application just did.

Deliberately not persisted and deliberately tiny. Its job is to answer "did
that button do anything" in the second after you press it — the same question
the terminal answers when you run the CLI. Anything worth keeping longer than
a process lifetime is already a row in the database.
"""

from __future__ import annotations

import datetime as dt
from collections import deque

_EVENTS: deque[dict[str, str]] = deque(maxlen=12)


def add(message: str) -> None:
    _EVENTS.appendleft(
        {"t": dt.datetime.now().strftime("%H:%M:%S"), "msg": message}
    )


def entries() -> list[dict[str, str]]:
    return list(_EVENTS)

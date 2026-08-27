"""A small in-memory rate limiter for the routes strangers can reach.

Scope, stated plainly: this is per-process and it forgets everything on
restart. The compose file runs one uvicorn worker, so per-process is
per-deployment here — but add workers and each one gets its own counters, and
the effective limit multiplies. Anything beyond that wants shared state in
Postgres or Redis, and neither is worth the moving parts until this is running
behind more than one process.

What it is actually for: making a password guessable only at human speed, and
keeping the argon2 verify — which is expensive on purpose — from being a way
to burn the machine's CPU for free.
"""

from __future__ import annotations

import time
from collections import OrderedDict

# How many accounts or addresses keep a counter at once. The number of people
# who can reach the login form is decided by strangers, so the structure has to
# be bounded rather than merely small.
MAX_TRACKED = 4096


class Limiter:
    """Fixed-window counters keyed by an arbitrary string.

    Fixed windows let through up to twice the limit across a window boundary.
    That is a real property and it is fine here: the point is to turn millions
    of guesses into dozens, and 2x dozens is still dozens.
    """

    def __init__(self, limit: int, window: float) -> None:
        self.limit = limit
        self.window = window
        self._hits: OrderedDict[str, tuple[float, int]] = OrderedDict()

    def _now(self) -> float:
        return time.monotonic()

    def check(self, key: str) -> float:
        """Seconds until `key` may try again, or 0.0 if it may try now.

        Read-only: it reports the verdict but does not spend an attempt, so a
        route can answer "too many attempts" without that answer itself
        counting as one.
        """
        started, count = self._hits.get(key, (0.0, 0))
        now = self._now()
        if now - started >= self.window:
            return 0.0
        if count < self.limit:
            return 0.0
        return self.window - (now - started)

    def record(self, key: str) -> None:
        """Count one failed attempt against `key`."""
        now = self._now()
        started, count = self._hits.get(key, (0.0, 0))
        if now - started >= self.window:
            started, count = now, 0
        self._hits[key] = (started, count + 1)
        self._hits.move_to_end(key)
        while len(self._hits) > MAX_TRACKED:
            self._hits.popitem(last=False)

    def reset(self, key: str) -> None:
        """Forget `key`. Called on a success, so somebody who mistyped their
        password four times and then got it right starts clean rather than
        carrying the failures into their next session."""
        self._hits.pop(key, None)

    def clear(self) -> None:
        self._hits.clear()


def client_ip(request) -> str:
    """The address to attribute an attempt to.

    uvicorn runs with `--proxy-headers`, so `request.client.host` is already
    the address from `X-Forwarded-For` rather than the proxy's. That header is
    only trustworthy because the app is not reachable except through the
    tunnel; a deployment that publishes the port directly is one where a
    client picks its own value here and the per-address limit means nothing.

    Which is why the login route limits per *account* as well. Spoofing the
    address gets you a fresh address budget, not a fresh budget against the
    account you are trying to break into.
    """
    client = getattr(request, "client", None)
    return getattr(client, "host", None) or "unknown"


def wait_message(seconds: float) -> str:
    """How a refusal is worded.

    It names the wait rather than saying "blocked", because the person reading
    it is nearly always the account's real owner having a bad morning, not the
    attacker the limit is there for.
    """
    minutes = max(1, int(seconds // 60) + (1 if seconds % 60 else 0))
    return f"too many attempts. try again in about {minutes} minute" + ("s" if minutes > 1 else "")

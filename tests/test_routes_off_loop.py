"""No route may do its synchronous work on the event loop.

The web app is one uvicorn process with one event loop. A `def` route runs in
a threadpool; an `async def` route runs on the loop itself, so any query,
commit or file write inside it stops every other request in the process until
it finishes — other accounts, an import's progress poll, `/healthz`. Nothing
fails while that happens, so nothing says so. It reads as a slow site.

Six routes had that shape while awaiting nothing but `request.json()`, and
CLAUDE.md named three of them as known and left alone. A list in a document
is one a new route never gets added to, which is how it came to be three. So
the rule is a test: a route is `def` unless it is named here, with the reason
it has to wait on something, and whatever synchronous work it does is handed
to the threadpool explicitly.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator

from fastapi.routing import APIRoute

# (method, path) -> why this route awaits. Each of these must hand its
# database work to `run_in_threadpool`; being on this list is a promise that
# it does, not an exemption from it.
ASYNC_ROUTES = {
    ("GET", "/card/{card_id}/image"): "fetches the reference image from upstream",
    ("GET", "/plugins"): "asks the encoder service whether it is up",
}


def _api_routes(routes) -> Iterator[APIRoute]:
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
            continue
        # FastAPI keeps an included router behind a wrapper rather than
        # flattening it into the app, so its routes are one step down.
        inner = getattr(route, "original_router", route)
        yield from _api_routes(getattr(inner, "routes", ()))


def _async_routes() -> dict[tuple[str, str], str]:
    from foilstack.web.app import app

    found = {}
    for route in _api_routes(app.routes):
        if inspect.iscoroutinefunction(route.endpoint):
            for method in route.methods:
                found[(method, route.path)] = route.endpoint.__name__
    return found


def test_the_walk_reaches_routes_inside_routers():
    """A guard on the guard: a walk that stopped at the app's own routes
    would find no async routes in the routers and pass for that reason."""
    from foilstack.web.app import app

    paths = {route.path for route in _api_routes(app.routes)}
    assert "/api/scans/commit" in paths
    assert "/card/{card_id}/image" in paths


def test_no_route_does_its_work_on_the_event_loop():
    unexplained = {key: name for key, name in _async_routes().items() if key not in ASYNC_ROUTES}
    assert not unexplained, (
        f"{unexplained} are `async def`, so their synchronous work runs on the "
        "one event loop the whole process shares. Make them `def` — take a JSON "
        "body as `payload: dict[str, Any] = Body(...)` rather than awaiting "
        "`request.json()` — or, if they genuinely await I/O, add them to "
        "ASYNC_ROUTES with the reason and move their database work into "
        "`run_in_threadpool`."
    )


def test_every_exemption_is_still_needed():
    """An entry for a route that is now `def`, or no longer exists, is an
    exemption waiting for something else to arrive at that path."""
    stale = set(ASYNC_ROUTES) - set(_async_routes())
    assert not stale, f"{stale} are no longer async routes; remove them from ASYNC_ROUTES"

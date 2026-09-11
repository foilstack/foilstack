"""The guards that only matter once strangers can reach the server.

Each of these was written by breaking the thing first: the test is here
because the behaviour it asserts did not exist, and it fails against the
version of the application that shipped without it.
"""

from __future__ import annotations

import re
from pathlib import Path

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


def test_the_build_is_read_from_the_environment_first(monkeypatch):
    """The image bakes it in; the checkout is only the fallback."""
    from foilstack.config import get_settings

    monkeypatch.setenv("FOILSTACK_GIT_SHA", "abc1234")
    get_settings.cache_clear()
    try:
        assert get_settings().git_sha == "abc1234"
    finally:
        get_settings.cache_clear()


def test_the_build_falls_back_to_the_checkout(monkeypatch):
    """Running straight from a clone should still say which commit it is."""
    from foilstack.config import _git_sha_from_checkout, get_settings

    monkeypatch.delenv("FOILSTACK_GIT_SHA", raising=False)
    get_settings.cache_clear()
    try:
        sha = get_settings().git_sha
    finally:
        get_settings.cache_clear()

    assert sha == _git_sha_from_checkout()
    # This repository is a checkout, so there is a real answer to find.
    assert len(sha) == 7 and all(c in "0123456789abcdef" for c in sha)


def test_a_missing_checkout_reports_nothing(tmp_path):
    """No .git and no environment variable is an ordinary state — a container
    built without the argument — and must not raise on startup."""
    from foilstack.config import _git_sha_from_checkout

    assert _git_sha_from_checkout(tmp_path) == ""


def test_a_loose_ref_is_read(tmp_path):
    from foilstack.config import _git_sha_from_checkout

    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    (git / "refs" / "heads" / "main").write_text("0123456789abcdef0123456789abcdef01234567\n")

    assert _git_sha_from_checkout(tmp_path) == "0123456"


def test_a_packed_ref_is_read(tmp_path):
    """A freshly cloned repository has its refs packed, with no file to read —
    which is the state a self-hoster's clone is in."""
    from foilstack.config import _git_sha_from_checkout

    git = tmp_path / ".git"
    git.mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    (git / "packed-refs").write_text(
        "# pack-refs with: peeled fully-peeled sorted\n"
        "89abcdef0123456789abcdef0123456789abcdef refs/heads/main\n"
    )

    assert _git_sha_from_checkout(tmp_path) == "89abcde"


def test_a_detached_head_is_read(tmp_path):
    """HEAD holding a bare sha rather than a ref — what a CI checkout looks
    like, and what a `git checkout <tag>` deploy looks like."""
    from foilstack.config import _git_sha_from_checkout

    git = tmp_path / ".git"
    git.mkdir()
    (git / "HEAD").write_text("fedcba9876543210fedcba9876543210fedcba98\n")

    assert _git_sha_from_checkout(tmp_path) == "fedcba9"


@pytest.mark.parametrize(
    "path,expected",
    [
        # A landing still rather than the demo animation: the two animated
        # WebPs this used to name existed only for the landing hero, which
        # now uses stills, and were deleted with it. The mime question is
        # the same one and these are the files that answer it now.
        ("/static/shots/queue.webp", "image/webp"),
        ("/static/fonts/jetbrains-mono-latin.woff2", "font/woff2"),
        ("/static/app.css", "text/css"),
        ("/static/brand/mark.svg", "image/svg+xml"),
    ],
)
def test_static_assets_are_typed_correctly(path, expected):
    """`python:3.12-slim` has no mime table entry for webp or woff2, so these
    went out as application/octet-stream and application/json — and the
    nosniff header tells the browser not to second-guess that."""
    from fastapi.testclient import TestClient

    from foilstack.web.app import app

    with TestClient(app) as client:
        response = client.get(path)

    assert response.status_code == 200, path
    assert response.headers["content-type"].split(";")[0] == expected


def test_the_types_are_registered_rather_than_inherited():
    """The end-to-end check above passes on a development machine whether or
    not the registration exists, because /etc/mime.types already knows these.
    It only fails inside the slim image — which is no use as a test. This one
    starts from a registry that knows nothing and fails anywhere."""
    import mimetypes

    from foilstack.web.app import _register_mime_types

    db = mimetypes.MimeTypes(filenames=())
    assert db.guess_type("x.woff2")[0] is None

    _register_mime_types(db)

    assert db.guess_type("x.woff2")[0] == "font/woff2"
    assert db.guess_type("x.webp")[0] == "image/webp"


# ---------------------------------------------------------------------------
# What a request body may cost before a route has seen it.


def _refused_unread(scope):
    """Run `BodyLimit` on a request whose body must not be read, and return
    what it sent and whether anything behind it ran."""
    import asyncio

    from foilstack.web.bodylimit import BodyLimit

    reached = False
    sent: list[dict] = []

    async def inner(scope, receive, send):
        nonlocal reached
        reached = True

    async def receive():
        raise AssertionError("the body was read")

    async def send(message):
        sent.append(message)

    asyncio.run(BodyLimit(inner)(scope, receive, send))
    return sent, reached


def test_an_oversized_upload_is_refused_before_it_reaches_the_disk(monkeypatch):
    """The route counted every byte against the archive cap, and the count was
    right — but FastAPI resolves `File(...)` before calling the route, and
    Starlette spools a file part to disk with no ceiling of its own. So a 20 MB
    body against a 1 MB cap was refused, correctly, after all 20 MB had been
    written to `/tmp`. What matters is what reached the disk, so that is what
    this counts."""
    import starlette.formparsers
    from fastapi.testclient import TestClient

    from foilstack.config import get_settings
    from foilstack.web import bodylimit
    from foilstack.web.app import app

    written = 0

    class Counting(starlette.formparsers.SpooledTemporaryFile):
        def write(self, data):
            nonlocal written
            written += len(data)
            return super().write(data)

    monkeypatch.setattr(starlette.formparsers, "SpooledTemporaryFile", Counting)
    monkeypatch.setenv("FOILSTACK_MAX_ARCHIVE_MB", "1")
    get_settings.cache_clear()
    try:
        limit, _ = bodylimit.ceiling("POST", "/api/import", get_settings())
        boundary = "foilstack-test-boundary"
        body = (
            (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="archive"; filename="scans.zip"\r\n'
                "Content-Type: application/zip\r\n\r\n"
            ).encode()
            + b"\0" * (20 * 1024 * 1024)
            + f"\r\n--{boundary}--\r\n".encode()
        )
        # A generator, so no Content-Length goes out and the ceiling has to be
        # found by counting rather than read off a header.
        response = TestClient(app).post(
            "/api/import",
            content=iter([body]),
            headers={"content-type": f"multipart/form-data; boundary={boundary}"},
        )
    finally:
        get_settings.cache_clear()

    assert response.status_code == 413
    assert "1 MB" in response.json()["detail"]
    assert written <= limit, f"{written:,} bytes reached the disk past a {limit:,} ceiling"


def test_a_declared_length_past_the_ceiling_is_refused_without_reading_it():
    """A browser always says how large an upload is. When the answer is too
    large, there is no reason to take a single byte of it."""
    from foilstack.web import bodylimit

    sent, reached = _refused_unread(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/scans/commit",
            "headers": [(b"content-length", str(bodylimit.DEFAULT_BYTES + 1).encode())],
        }
    )

    assert sent[0]["status"] == 413
    assert not reached


def test_a_body_that_declares_no_length_is_cut_off_at_the_ceiling():
    """Chunked encoding sends no length at all, so a header check alone is a
    ceiling with a door beside it."""
    import asyncio

    from fastapi import HTTPException

    from foilstack.web import bodylimit

    seen = 0

    async def inner(scope, receive, send):
        nonlocal seen
        while True:
            seen += len((await receive()).get("body", b""))

    async def receive():
        return {"type": "http.request", "body": b"\0" * 65536, "more_body": True}

    async def send(message):
        pass

    scope = {"type": "http", "method": "POST", "path": "/api/scans/commit", "headers": []}
    with pytest.raises(HTTPException) as refused:
        asyncio.run(bodylimit.BodyLimit(inner)(scope, receive, send))

    assert refused.value.status_code == 413
    assert seen <= bodylimit.DEFAULT_BYTES


def test_an_archive_of_the_promised_size_fits_under_the_ceiling():
    """The import screen promises `max_mb` per upload, and a multipart body is
    larger than the files in it — by more for loose images, where every file is
    a part with its own headers. A ceiling that refused an honest upload for its
    framing would break that promise with arithmetic."""
    from dataclasses import replace

    from foilstack import importing
    from foilstack.config import get_settings
    from foilstack.web import bodylimit

    settings = replace(get_settings(), max_archive_mb=64)
    boundary = "----WebKitFormBoundary" + "a" * 16
    part = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="archive"; filename="{"x" * 251}.jpg"\r\n'
        "Content-Type: image/jpeg\r\n\r\n\r\n"
    )
    field = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="default_condition"\r\n\r\nNM\r\n'
    )
    honest = (
        64 * 1024 * 1024
        + importing.MAX_IMAGES * len(part)
        + 5 * len(field)
        + len(f"--{boundary}--\r\n")
    )

    limit, _ = bodylimit.ceiling("POST", "/api/import", settings)

    assert limit >= honest


def test_every_upload_ceiling_names_a_real_route():
    """A ceiling keyed by path goes stale silently when the route is renamed:
    the upload falls to the default cap, and every archive over eight megabytes
    is refused as too large — found by the first seller who tries."""
    from starlette.routing import Match

    from foilstack.web import bodylimit
    from foilstack.web.app import app

    # Asked of the router rather than read off `app.routes`, which holds each
    # included router as a wrapper rather than the routes inside it.
    def dispatched(path: str) -> bool:
        scope = {"type": "http", "method": "POST", "path": path, "root_path": "", "headers": []}
        return any(route.matches(scope)[0] == Match.FULL for route in app.routes)

    assert all(dispatched(path) for path in bodylimit.UPLOADS), [
        path for path in bodylimit.UPLOADS if not dispatched(path)
    ]
    assert not dispatched("/api/import-renamed"), "the check matches anything"


# ---------------------------------------------------------------------------
# Whose address a request is attributed to.

ROOT = Path(__file__).resolve().parents[1]


def _shipped_forwarded_allow_ips() -> str:
    """What the shipped compose file's uvicorn believes, by uvicorn's own
    precedence: a flag on the command line, then the environment, then its
    default. Read rather than hardcoded, so the test is about what ships."""
    compose = (ROOT / "docker-compose.yml").read_text()
    flag = re.search(r'"--forwarded-allow-ips",\s*"([^"]*)"', compose)
    if flag:
        return flag.group(1)
    env = re.search(
        r'FORWARDED_ALLOW_IPS:\s*"?\$\{FOILSTACK_FORWARDED_ALLOW_IPS:-([^}]*)\}', compose
    )
    return env.group(1) if env else "127.0.0.1"


def _seen_by_the_app(peer: str, headers: dict[str, str]) -> dict[str, str]:
    import asyncio

    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    seen: dict[str, str] = {}

    async def inner(scope, receive, send):
        seen["client"] = scope["client"][0]
        seen["scheme"] = scope["scheme"]

    scope = {
        "type": "http",
        "scheme": "http",
        "client": (peer, 40000),
        "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
    }
    trusted = _shipped_forwarded_allow_ips()
    asyncio.run(ProxyHeadersMiddleware(inner, trusted_hosts=trusted)(scope, None, None))
    return seen


def test_a_visitor_cannot_name_their_own_address_through_the_proxy():
    """Measured against the live site before this was fixed: a request carrying
    `X-Forwarded-For: 203.0.113.9` was logged as coming from 203.0.113.9.
    Cloudflare appends the real address to a header the visitor already sent,
    and `*` told uvicorn to believe the left-most entry — the visitor's."""
    seen = _seen_by_the_app(
        peer="172.19.0.7", headers={"x-forwarded-for": "203.0.113.9, 198.51.100.4"}
    )

    assert seen["client"] == "198.51.100.4"


def test_a_visitor_reaching_the_port_directly_cannot_name_one_either():
    seen = _seen_by_the_app(peer="198.51.100.4", headers={"x-forwarded-for": "203.0.113.9"})

    assert seen["client"] == "198.51.100.4"


def test_the_proxy_is_still_believed_about_the_scheme():
    """Trusting nothing would also have closed the hole, and quietly issued
    every session cookie without `Secure` behind the tunnel."""
    seen = _seen_by_the_app(
        peer="172.19.0.7",
        headers={"x-forwarded-for": "198.51.100.4", "x-forwarded-proto": "https"},
    )

    assert seen["scheme"] == "https"

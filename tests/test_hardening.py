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
        ("/static/demo/foilstack.webp", "image/webp"),
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

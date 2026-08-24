"""Cache-busting: every versioned asset has to be inside the version.

The `?v=` query string on a static URL is the only thing that makes a CDN
give up a cached copy. If a file carries that query string but is not hashed
into the value, its URL never changes when the file does, and the CDN keeps
serving the old one — which does not look like a caching problem from the
outside. It looks like the deploy silently did nothing.

That has happened twice on this codebase: once with a script added after the
stylesheet, and once with the demo animation held as a stale
`application/octet-stream`. Both were found by hand. This finds the next one.
"""

from __future__ import annotations

import re
import shutil

import pytest

from foilstack.web import chrome

# `/static/app.css?v={{ asset_v }}` -> `app.css`
VERSIONED = re.compile(r"/static/([A-Za-z0-9_./-]+)\?v=\{\{\s*asset_v\s*\}\}")


def _versioned_assets() -> set[str]:
    names: set[str] = set()
    for template in (chrome.BASE_DIR / "templates").rglob("*.html"):
        names |= set(VERSIONED.findall(template.read_text()))
    return names


def test_the_templates_actually_version_their_assets():
    """A guard on the guard: if this finds nothing, the test below proves nothing."""
    assets = _versioned_assets()
    assert "app.css" in assets
    assert "app.js" in assets, "the shared script must be cache-busted like the rest"


@pytest.mark.parametrize("name", sorted(_versioned_assets()))
def test_changing_a_versioned_asset_changes_the_version(name, tmp_path, monkeypatch):
    """Edit each versioned file in turn; the asset version must move."""
    # A copy, because the real files are bind-mounted in development and this
    # test writes to them.
    shutil.copytree(chrome.BASE_DIR / "static", tmp_path / "static")
    monkeypatch.setattr(chrome, "BASE_DIR", tmp_path)

    target = tmp_path / "static" / name
    if not target.exists():
        pytest.skip(f"{name} is not present in this checkout")

    before = chrome._asset_version()
    target.write_bytes(target.read_bytes() + b"\n/* moved */\n")
    after = chrome._asset_version()

    assert before != after, (
        f"{name} is served with ?v= but is not hashed into it, so a change to "
        f"it ships behind a version that never moves"
    )

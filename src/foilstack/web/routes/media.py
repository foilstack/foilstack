"""Serving images: a seller's own scans, and catalogue reference art.

Split from app.py because it shares nothing with the screens — no templates, no
page chrome, no inventory maths. What it does have is the check that matters
most on this deployment: an image route that forgets to scope itself hands out
photographs of another person's property, so both routes here take the owner
dependency and filter by it before they touch the filesystem.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from foilstack import db, images
from foilstack.config import get_settings
from foilstack.importing import scan_path
from foilstack.web.deps import db_session, owner

logger = logging.getLogger(__name__)
router = APIRouter()

# Resolved per call, not bound at import — see the note in web/deps.py. These
# routes read the data directory, so a stale settings object here serves images
# out of whichever directory the process first happened to see.


@router.get("/scan/{scan_id}/image")
def scan_image(
    scan_id: int,
    session=Depends(db_session),
    user: db.User = Depends(owner),
):
    scan = session.get(db.Scan, scan_id)
    # Ownership is checked before the file is: this route serves photographs
    # of another person's property, and "does this id exist" is not a question
    # a stranger gets to ask.
    if scan is None or scan.user_id != user.id:
        raise HTTPException(404, "not found")
    # `scan_path` resolves the stored location against the scans directory and
    # refuses anything that lands outside it. The extractor already rejects
    # such entries on the way in; this route turns a database value into a
    # filesystem read, so it checks again.
    settings = get_settings()
    path = scan_path(scan.stored_path, settings.scans_dir)
    if path is None:
        raise HTTPException(404, "not found")
    # Prefer the downscaled copy, building it on first request for scans
    # imported before display copies existed. Falls back to the original, which
    # is correct but large.
    display = images.make_display_copy(path, settings.display_dir, scan.stored_path)
    return FileResponse(
        display or path,
        headers={"Cache-Control": "private, max-age=86400"},
    )


# TCGplayer serves several sizes of the same product image, selected by a
# suffix on the URL. The catalogue stores the 200w thumbnail, which is 200x278
# — fine behind a 46px box and useless the moment anyone looks closer, and
# looking closer is the entire job of the review queue: printings differ by a
# set symbol a few pixels wide.
#
# Tried in order, first success wins. `1000x1000` answers 403; `in_1000x1000`
# is the variant that works, at 672x936.
_REF_VARIANTS = ("in_1000x1000", "400w")


def _reference_urls(url: str) -> list[str]:
    """Larger variants of a catalogue image, best first, original last."""
    candidates = [url.replace("_200w", f"_{v}") for v in _REF_VARIANTS if "_200w" in url]
    candidates.append(url)
    # dict.fromkeys keeps order and drops the duplicate when no swap happened.
    return list(dict.fromkeys(candidates))


@router.get("/card/{card_id}/image")
async def card_image(
    card_id: int,
    session=Depends(db_session),
    user: db.User = Depends(owner),
):
    """The catalogue's reference image, fetched once and cached on disk.

    Proxied rather than linked straight to the upstream CDN. Both the review
    queue and the inventory table put the reference next to the scan, and a
    direct `<img src>` would tell that CDN which cards this seller is looking
    at, every time a page loads.

    The catalogue is shared, so there is nothing here that belongs to one
    account — but it still requires a session, because an open image proxy on
    a public host is a free bandwidth donation to whoever finds it.
    """
    card = session.get(db.Card, card_id)
    url = card.image_url if card else None
    if not url:
        raise HTTPException(404, "no reference image")

    # `-lg` rather than the old bare `{card_id}.img`: the cache key has to change
    # when the thing being cached does, or every card viewed before this stays
    # pinned at 200px forever. Old files are simply orphaned and can be deleted.
    cache = get_settings().refs_dir / f"{card_id}-lg.img"
    if not cache.exists():
        body = None
        for candidate in _reference_urls(url):
            try:
                async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                    response = await client.get(candidate)
            except httpx.HTTPError as exc:
                logger.warning("reference fetch failed for %s at %s: %s", card_id, candidate, exc)
                continue
            if response.status_code == 200:
                body = response.content
                break
        if body is None:
            raise HTTPException(502, "could not fetch reference image")
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(body)

    return FileResponse(
        cache,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=86400"},
    )

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
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from foilstack import db, images
from foilstack.config import get_settings
from foilstack.importing import scan_path
from foilstack.web import auth, proof
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
    stored_path = scan.stored_path

    # Give the connection back before touching the filesystem.
    #
    # This route is `def`, not `async def`, so FastAPI runs it in a threadpool
    # forty threads wide — and every one of them was holding a pooled
    # connection through a Pillow resize. Forty threads against a pool of five
    # plus ten overflow is where the site went down: the pool empties, every
    # other route waits thirty seconds for a connection that is not coming, and
    # `/healthz` fails with it.
    session.close()

    path = scan_path(stored_path, settings.scans_dir)
    if path is None:
        raise HTTPException(404, "not found")
    # Prefer the downscaled copy, building it on first request for scans
    # imported before display copies existed. Falls back to the original, which
    # is correct but large.
    display = images.make_display_copy(path, settings.display_dir, stored_path)
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
    request: Request,
    card_id: int,
    session=Depends(db_session),
):
    """The catalogue's reference image, fetched once and cached on disk.

    Proxied rather than linked straight to the upstream CDN. Both the review
    queue and the inventory table put the reference next to the scan, and a
    direct `<img src>` would tell that CDN which cards this seller is looking
    at, every time a page loads.

    The catalogue is shared, so nothing here belongs to one account — but it
    still wants a session, because this fetches from upstream on a miss and
    caches to disk. Open to anyone, it is a stranger's ability to walk a
    hundred thousand ids and have this server pull every one of them from
    somebody else's CDN.

    The exception is the pair the landing page argues with, which a signed-out
    visitor has to be able to see. Two known ids is not an open proxy.
    """
    settings = get_settings()
    viewer = auth.current_user(request, session, settings)
    if viewer is None and not proof.is_proof_card(session, card_id):
        raise HTTPException(404, "no reference image")
    card = session.get(db.Card, card_id)
    url = card.image_url if card else None

    # Everything the database is needed for has now happened, so give the
    # connection back before going anywhere near the network.
    #
    # This took the site down. The fetch below can take twenty seconds per
    # candidate, and holding a pooled connection across it means fifteen
    # concurrent thumbnails exhaust a pool of five plus ten overflow — after
    # which every route, `/healthz` included, blocks for thirty seconds and
    # times out. It survived a small catalogue because misses were rare; it
    # stopped surviving at thirty thousand cards, where a good number of the
    # promo entries have no image behind them at all.
    session.close()

    if not url:
        raise HTTPException(404, "no reference image")

    # `-lg` rather than the old bare `{card_id}.img`: the cache key has to change
    # when the thing being cached does, or every card viewed before this stays
    # pinned at 200px forever. Old files are simply orphaned and can be deleted.
    cache = settings.refs_dir / f"{card_id}-lg.img"

    # A card whose image upstream refuses is not a transient failure, and a
    # page full of them must not re-ask on every load. The miss is remembered
    # the same way a hit is, as a file, so it survives a restart.
    missing = settings.refs_dir / f"{card_id}-lg.missing"
    if missing.exists():
        raise HTTPException(404, "no reference image")

    if not cache.exists():
        body = None
        # One client for all candidates rather than one per attempt: building a
        # fresh connection pool to the same host three times over is wasted
        # handshakes on the exact path that was already too slow.
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            for candidate in _reference_urls(url):
                try:
                    response = await client.get(candidate)
                except httpx.HTTPError as exc:
                    logger.warning(
                        "reference fetch failed for %s at %s: %s", card_id, candidate, exc
                    )
                    continue
                if response.status_code == 200:
                    body = response.content
                    break
        if body is None:
            # Remembered, so the next page view does not spend twenty seconds
            # rediscovering it. 404 rather than 502: from the browser's side
            # this card has no reference image, which is the truth and is not
            # worth a retry.
            cache.parent.mkdir(parents=True, exist_ok=True)
            missing.touch()
            raise HTTPException(404, "no reference image")
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(body)

    return FileResponse(
        cache,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=86400"},
    )

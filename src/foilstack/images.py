"""Display copies of scans.

A scan is a photograph of a card, and sellers photograph at whatever their
phone or scanner produces — the first real import here was 48 files averaging
680 KB at 1458x2016. The review queue renders them at 46x64 CSS pixels and the
inventory table at 24x33, so a page of that queue was shipping ~33 MB to draw
about a postage stamp of pixels.

So the original is kept untouched on disk — it is the evidence, and the thing
the encoder ran on — and a downscaled copy is written alongside it for the
browser. Nothing here is destructive.

The long edge is deliberately far larger than any current thumbnail. Printings
are separated by a set symbol a few pixels wide, and the moment this screen
grows a "look closer" control, a copy scaled to fit today's 64px box would be
useless for the one job the reviewer actually has.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

# Long edge, in pixels. A 745x1040 card image is what TCGplayer serves as its
# large variant, so this is comfortably above the reference material.
MAX_EDGE = 1024
QUALITY = 82


def display_path(display_dir: Path, stored_path: str) -> Path:
    """Mirror the scans directory, rather than key on the scan's row id.

    A row id is unique within one database, and the display directory is not
    scoped to one: point a second database at the same data directory — which
    `scripts/preview.py` does deliberately — and scan 7 there silently serves
    the cached copy of scan 7 here. That showed the wrong photographs under the
    right card names, which is the worst way for a cache to be wrong.

    The file is what is being copied, so the file's path is what identifies it.
    """
    return display_dir / stored_path


def make_display_copy(source: Path, display_dir: Path, stored_path: str) -> Path | None:
    """Write a browser-sized copy of `source`. Returns None if it could not.

    Never raises into the import: a scan that cannot be resized is still a scan
    that matched, and the original is always there to fall back on.
    """
    target = display_path(display_dir, stored_path)
    if target.exists():
        return target
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(source) as opened:
            im: Image.Image = opened
            # Phone cameras record orientation in EXIF rather than in the
            # pixels. Without this, portrait scans arrive rotated — and a
            # sideways card is one a reviewer cannot read at 64 pixels tall.
            im = ImageOps.exif_transpose(im) or im
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            im.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
            im.save(target, "JPEG", quality=QUALITY, optimize=True, progressive=True)
        return target
    except Exception as exc:  # noqa: BLE001 - display is never worth failing an import over
        logger.warning("could not build display copy for %s: %s", stored_path, exc)
        return None

"""Record the walkthrough that goes in the README.

Screenshot frames rather than a screen recording, assembled with Pillow. The
ffmpeg Playwright ships can only encode VP8, so there is no path from its
video to an mp4 or a GIF on this machine — and a GIF is what a README can
autoplay and loop without a player, on GitHub and everywhere the link gets
posted. Playwright's own webm is written alongside anyway; it is free, and it
is the better source if this is ever cut for YouTube.

Frames are captured deliberately, not on a timer: each beat of the story says
how many it wants, so a scroll gets enough to read as motion and a pause on a
screen worth reading gets enough to read it.

    uv run python scripts/preview.py --demo src/foilstack/web/static/demo    # disposable data
    uv run python scripts/demo.py --url http://localhost:8090 --out src/foilstack/web/static/demo

There is no narration and no captions. This has to make sense on mute, in a
Reddit sidebar, at whatever size the reader's browser decides.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeout

# 1000px wide reads at README width without the file becoming enormous. The
# 10:6.25 shape is the app's own proportions; anything squarer crops the rail
# or the queue, which are the two things worth seeing.
WIDTH, HEIGHT = 1000, 625

# Capture at twice the CSS size, then downsample into the GIF. Sampling at 1x
# and writing at 1x gave exactly enough pixels for a monitor nobody has owned
# for a decade; every modern laptop and phone doubles on the way to the screen,
# and the result was the soft, slightly smeared demo this replaced.
DEVICE_SCALE = 2

# Milliseconds per frame in the finished GIF. 100ms (10fps) is smooth enough
# for a cursor and a scroll, and coarse enough that a twenty second story does
# not become a forty megabyte file.
FRAME_MS = 100

# The GIF is written smaller than it is captured, and with a reduced palette.
# At full width and 192 colours it ran to five megabytes, which is a README
# image a reader watches load.
#
# The scroll beats are deliberately short for the same reason. Halving the
# frame rate would also fit under the limit, but a scroll at five frames a
# second reads as broken; less scrolling at ten reads as brisk.
# The GIF is the README's copy, and the README is where a stranger meets this
# project. 640px was chosen to fit the 2048 KB commit hook; that hook turned
# out never to have applied here — it only checks files being *added*, and the
# GIF has been tracked since the beginning — so the width was paying a tax that
# was not being collected.
#
# 1000px is what GitHub's content column actually renders, and the palette is
# close to free: at 800px, 192 colours came out fractionally *smaller* than
# 160. Width is the whole cost. Stopping at 1000 rather than 1200 is about
# camo, GitHub's image proxy, which has a size ceiling near 5 MB — 1200px at
# 256 lands at 5.2 and would be betting the README's only picture on it.
GIF_WIDTH = 1000
GIF_COLORS = 192

# The pointer the browser will not draw for us. Without it a click is a screen
# that changes for no visible reason, which reads as a cut rather than an
# action — and this has to be followable with the sound off.
CURSOR_JS = """
(() => {
  if (document.getElementById('__demo_cursor')) return;
  const c = document.createElement('div');
  c.id = '__demo_cursor';
  c.style.cssText = [
    'position:fixed', 'z-index:2147483647', 'width:22px', 'height:22px',
    'margin:-11px 0 0 -11px', 'border-radius:50%', 'pointer-events:none',
    'background:rgba(255,255,255,.92)',
    'box-shadow:0 0 0 2px rgba(0,0,0,.55), 0 2px 10px rgba(0,0,0,.45)',
    'transition:transform .18s ease-out', 'left:0', 'top:0',
    'transform:translate(-100px,-100px)',
  ].join(';');
  document.documentElement.appendChild(c);
})();
"""


class Recorder:
    """Captures frames and remembers where the cursor is."""

    def __init__(self, page: Page, frames_dir: Path) -> None:
        self.page = page
        self.dir = frames_dir
        self.n = 0
        self.x = WIDTH // 2
        self.y = HEIGHT // 2

    def frame(self, count: int = 1) -> None:
        for _ in range(count):
            self.page.screenshot(path=str(self.dir / f"{self.n:05d}.png"))
            self.n += 1

    def cursor_to(self, x: float, y: float, settle: int = 3) -> None:
        self.x, self.y = x, y
        self.page.evaluate(
            "([x, y]) => { const c = document.getElementById('__demo_cursor');"
            " if (c) c.style.transform = `translate(${x}px, ${y}px)`; }",
            [x, y],
        )
        self.frame(settle)

    def click(self, selector: str, settle: int = 4) -> bool:
        """Move the pointer onto an element, then click it.

        Returns False rather than raising when the element is not there. A
        seeded preview does not always contain every state — a queue can be
        empty — and a demo that dies two thirds of the way through is worse
        than one that skips a beat.
        """
        target = self.page.query_selector(selector)
        if target is None:
            print(f"demo: no {selector}, skipping that beat", file=sys.stderr)
            return False
        box = target.bounding_box()
        if box is None:
            return False
        self.cursor_to(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        target.click()
        self.page.wait_for_timeout(350)
        self.page.evaluate(CURSOR_JS)
        self.frame(settle)
        return True

    def wait_for(self, selector: str, timeout: int = 5000) -> bool:
        """Wait for something a previous click went to the server for.

        Returns False rather than raising, for the same reason `click` does: a
        beat that cannot play is worth skipping, and a demo that dies two
        thirds of the way through is worse than one that is a beat short.
        """
        try:
            self.page.wait_for_selector(selector, timeout=timeout)
        except PlaywrightTimeout:
            print(f"demo: {selector} never arrived, skipping that beat", file=sys.stderr)
            return False
        self.page.evaluate(CURSOR_JS)
        return True

    def scroll(self, total: int, steps: int = 10) -> None:
        """Scroll whichever pane on this screen actually scrolls.

        Finding it rather than naming it. Every screen puts its overflow on a
        different element — `.screen-scroll` on the queue, `.split` on a card,
        something else again on listings — and naming the wrong one produces no
        motion and no error: the frames come out identical, get collapsed on
        the way into the GIF, and the beat silently vanishes. That happened
        three times before this existed. So: pick the element with the most
        hidden content below the fold, and say so if there is none.
        """
        found = self.page.evaluate("""
            () => {
              let best = null, most = 0;
              for (const el of document.querySelectorAll('div, main, section, tbody')) {
                const over = el.scrollHeight - el.clientHeight;
                const style = getComputedStyle(el);
                const scrolls = /auto|scroll/.test(style.overflowY + style.overflow);
                if (scrolls && over > most) { most = over; best = el; }
              }
              if (!best) return null;
              best.setAttribute('data-demo-scroller', '1');
              return most;
            }
        """)
        if not found:
            print("demo: nothing to scroll on this screen", file=sys.stderr)
            return

        target = "[data-demo-scroller]"
        # Never scroll past the end: the last frames would be a still image
        # captioned as motion, and they get collapsed anyway.
        distance = min(total, found)
        for _ in range(steps):
            self.page.eval_on_selector(target, "(el, d) => el.scrollBy(0, d)", distance / steps)
            self.page.wait_for_timeout(40)
            self.frame(1)
        self.page.eval_on_selector(target, "el => el.removeAttribute('data-demo-scroller')")

    def goto(self, url: str, settle: int = 6) -> None:
        self.page.goto(url, wait_until="networkidle")
        self.page.evaluate(CURSOR_JS)
        self.page.wait_for_timeout(250)
        self.frame(settle)


def storyboard(rec: Recorder, base: str, card_id: int | None = None) -> None:
    """The argument, in order: a pile of cards becomes a priced spreadsheet.

    Each beat answers a question a seller would ask, and the order is the order
    they would ask them — what did it read, was it right, what are they worth,
    how do I sell them.
    """
    # 1. The review queue. The heart of it: a scan beside the card it matched,
    #    with the runners-up visible so the top match reads as evidence rather
    #    than an assertion.
    rec.goto(f"{base}/app", settle=12)
    rec.scroll(360, steps=8)
    rec.frame(6)

    # 2. Correcting one. The queue's real claim is not that the matcher is
    #    always right — it is that being wrong is recoverable in two clicks,
    #    which is the question every seller asks about a tool like this. The
    #    panel is fetched on demand and then runs its own search, so the beat
    #    waits for the options rather than guessing at a duration.
    #
    #    Aimed at a row that has runners-up, not just any row. `.qalt-swap`
    #    only renders when the scan has alternates, so `:has()` picks a scan
    #    whose panel will show the "Also matched this scan" strip — the first
    #    take corrected a scan with a single candidate, and a panel headed
    #    "Pick the right card" above one option argues against itself.
    corrected = False
    if rec.click(".qrow:has(.qalt-swap) [data-fix]", settle=4) and rec.wait_for(
        ".qrow .qfix .qfix-list:not([data-fix-results]) [data-pick]"
    ):
        rec.frame(8)
        # A runner-up, not a search result: the point of the beat is that the
        # answer was already on the page.
        corrected = rec.click(
            ".qrow .qfix .qfix-list:not([data-fix-results]) [data-pick]", settle=12
        )

    # 3. Confirming it. The row leaves the queue, which is the whole loop in a
    #    single visible move — and it is the row just corrected, so the two
    #    beats read as one thought rather than two features.
    #    A corrected row wears the chosen icon in place of a match bar, which
    #    is how this finds it again — confirming a different row would turn one
    #    thought into two unrelated demonstrations.
    rec.click(
        ".qrow:has(.qconf svg) [data-confirm]" if corrected else ".qrow [data-confirm]",
        settle=10,
    )

    # 4. Inventory: consolidated by card, with quantities and value.
    rec.click('.nav-item[href="/inventory"]', settle=12)
    rec.scroll(300, steps=7)
    rec.frame(6)

    # 5. Into a card. The price trend is the thing no spreadsheet gives you, so
    #    this beat holds longest.
    #
    #    The link, not the row: clicking the row lands on its checkbox and
    #    selects the line instead of opening it, which silently cost this beat
    #    the first time. `card` names a stock line known to carry price
    #    history, because a card page with an empty trend panel argues against
    #    the feature it is here to show.
    opened = False
    if card_id is not None:
        rec.cursor_to(360, 300)
        rec.goto(f"{base}/inventory/{card_id}", settle=16)
        opened = True
    if not opened:
        rec.click("a.rowname", settle=16)
    # The trend panel is below the fold on this viewport, so the beat is
    # worthless without this scroll.
    rec.scroll(280, steps=7)
    rec.frame(14)

    # 6. Back out, select everything, and take it to the listing screen —
    #    ending on the CSV, because that is what the tool is for.
    rec.goto(f"{base}/inventory", settle=4)
    rec.click("#all", settle=5)
    rec.click("#listbtn", settle=14)
    rec.scroll(200, steps=5)
    rec.frame(12)


def _resized(images, width):
    """Downscale a run of frames, or hand them back untouched at native width."""
    from PIL import Image

    if width >= images[0].width:
        return images
    scale = width / images[0].width
    return [im.resize((width, round(im.height * scale)), Image.Resampling.LANCZOS) for im in images]


def assemble(
    frames_dir: Path,
    out: Path,
    frame_ms: int = FRAME_MS,
    gif_width: int = GIF_WIDTH,
    gif_colors: int = GIF_COLORS,
) -> None:
    """Write the animation as a GIF, for the README.

    A GIF and nothing else, now. This used to write two animated WebPs as well
    — a full-size one and a pre-cropped phone copy — because the landing page
    played the animation in its hero and needed both. The landing page now uses
    stills, and those two files were 4.3 MB of a repository that nothing
    referenced any more.

    The GIF is the format that plays absolutely everywhere a link gets pasted,
    which is the whole job here: it is the README's only picture, and the README
    is where a stranger meets this project.
    """
    from PIL import Image

    files = sorted(frames_dir.glob("*.png"))
    if not files:
        raise SystemExit("demo: no frames were captured")

    images = [Image.open(f).convert("RGB") for f in files]
    out.parent.mkdir(parents=True, exist_ok=True)

    small = _resized(images, gif_width)
    # One palette for the whole run, not one per frame. Per-frame palettes make
    # the background shimmer between frames, which is far more distracting than
    # the banding a fixed palette costs.
    palette = small[0].quantize(colors=gif_colors, method=Image.Quantize.MEDIANCUT)
    frames = [im.quantize(palette=palette, dither=Image.Dither.FLOYDSTEINBERG) for im in small]
    gif = out.with_suffix(".gif")
    frames[0].save(
        gif,
        save_all=True,
        append_images=frames[1:],
        duration=frame_ms,
        loop=0,
        optimize=True,
        # disposal=1 leaves each frame in place and lets the next one write
        # only what changed, which is most of the file: 4.5 MB becomes 3.2 MB
        # for identical output. Safe here because the palette carries no
        # transparency, so nothing shows through from the frame underneath —
        # checked by pulling late frames back out and looking at them, since
        # compositing artifacts would only appear near the end.
        disposal=1,
    )
    print(f"  {gif}  {gif.stat().st_size / 1048576:.1f} MB  ({len(images)} frames)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default=os.getenv("FOILSTACK_SHOT_URL", "http://localhost:8090"))
    ap.add_argument("--out", type=Path, default=Path("src/foilstack/web/static/demo"))
    ap.add_argument("--email", default=os.getenv("FOILSTACK_SHOT_EMAIL"))
    ap.add_argument("--password", default=os.getenv("FOILSTACK_SHOT_PASSWORD"))
    ap.add_argument("--card", type=int, default=None, help="the stock line to open")
    ap.add_argument("--frame-ms", type=int, default=FRAME_MS)
    ap.add_argument("--gif-width", type=int, default=GIF_WIDTH)
    ap.add_argument("--gif-colors", type=int, default=GIF_COLORS)
    ap.add_argument("--scale", type=int, default=DEVICE_SCALE, help="device pixel ratio")
    ap.add_argument("--keep-frames", action="store_true")
    args = ap.parse_args(argv)

    frames_dir = Path(tempfile.mkdtemp(prefix="foilstack-demo-"))
    args.out.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            viewport={"width": WIDTH, "height": HEIGHT},
            device_scale_factor=args.scale,
            record_video_dir=str(args.out / "video"),
            record_video_size={"width": WIDTH, "height": HEIGHT},
        )
        page = context.new_page()

        if args.email:
            page.goto(f"{args.url}/login", wait_until="networkidle")
            page.fill('input[name="email"]', args.email)
            page.fill('input[name="password"]', args.password or "")
            page.click('button[type="submit"]')
            page.wait_for_load_state("networkidle")

        page.evaluate(CURSOR_JS)
        rec = Recorder(page, frames_dir)
        storyboard(rec, args.url, args.card)
        print(f"demo: captured {rec.n} frames")

        context.close()
        browser.close()

    assemble(
        frames_dir,
        args.out / "foilstack",
        args.frame_ms,
        args.gif_width,
        args.gif_colors,
    )
    if args.keep_frames:
        print(f"  frames kept in {frames_dir}")
    else:
        shutil.rmtree(frames_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

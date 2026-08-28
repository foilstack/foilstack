"""Shoot the stills the landing page puts beside its claims.

This replaced a twenty-second animation in the hero. The animation was 3.0 MB
of a 3.2 MB page and the largest thing painted inside the fold; it also carried
its own copy of the application's nav bar, version number and all, so it
announced a stale build in the middle of the hero for as long as nobody
re-recorded it. Stills cost a fortieth as much, go out of date one screen at a
time, and can sit next to the sentence that makes their claim.

    uv run python scripts/preview.py --landing src/foilstack/web/static/shots
    uv run python scripts/landing.py --url http://localhost:8090 \
        --out src/foilstack/web/static/shots

Re-shoot these whenever you change a screen one of them shows — the queue, a
card page, or the listing run. The same rule the README animation has, and for
the same reason: a screenshot of a screen that no longer exists is a promise
the application does not keep.

`og.png` is not a screenshot. A link preview is rendered about 500px wide in a
feed, where a 1440px application screen is an unreadable grey rectangle, so
that one is a composed card: wordmark, headline, and the one line that says
what this is.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import os
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeout

# The same 10:6.25 the demo used: the application's own proportions, and
# anything squarer crops off either the rail or the queue, which are the two
# things worth seeing. 1000 CSS pixels at a device scale of 2 gives the 2000px
# the template declares, which is 1:1 on the retina screens that are now the
# common case and a clean downscale everywhere else.
WIDTH, HEIGHT = 1000, 625

# The card page gets a taller window. Its two columns scroll together, so
# reaching the price trend by scrolling empties the other half of the frame;
# the room has to come from the viewport instead. See `capture`.
CARD_HEIGHT = 820
DEVICE_SCALE = 2

# 80 rather than the animation's 72. A still is read at leisure and can be
# looked at closely; it is also two hundred kilobytes rather than three
# megabytes, so the quality is nearly free here in a way it never was there.
WEBP_QUALITY = 80

# What a link preview is actually rendered at. 1200x630 is the size every
# platform asks for and roughly nobody displays at: the real viewing size is
# half that in a feed, which is why this is a composed card and not a
# screenshot of an application.
OG_WIDTH, OG_HEIGHT = 1200, 630

OG_CARD = """
<!DOCTYPE html><html><head><meta charset="utf-8"><style>
  @font-face { font-family: 'JetBrains Mono'; src: url('%(mono)s') format('woff2'); }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    width: %(w)spx; height: %(h)spx; background: #f4f4f2; color: #17171a;
    font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
    padding: 74px 80px; display: flex; flex-direction: column;
    justify-content: space-between;
    border-bottom: 10px solid #2f6b4f;
  }
  .top { display: flex; align-items: center; gap: 14px; }
  .top img { width: 40px; height: 40px; }
  .top b { font-size: 32px; letter-spacing: -0.02em; }
  h1 { font-size: 84px; line-height: 1.02; letter-spacing: -0.035em; font-weight: 700; }
  p { font-size: 28px; line-height: 1.45; color: #5d5d58; max-width: 900px; margin-top: 22px; }
  .foot {
    font-family: 'JetBrains Mono', ui-monospace, Menlo, monospace;
    font-size: 18px; letter-spacing: 0.1em; color: #70706a;
  }
</style></head><body>
  <div class="top"><img src="%(mark)s" alt=""><b>foilstack</b></div>
  <div>
    <h1>Scans in.<br>Priced CSV out.</h1>
    <p>Drop in a folder of card scans. Get back an identified, priced inventory
       and a marketplace CSV. %(games)s games, open source, self-hostable.</p>
  </div>
  <div class="foot">AGPL-3.0 &middot; SELF-HOSTABLE &middot; YOUR SCANS STAY PUT</div>
</body></html>
"""


def _settle(page: Page, ms: int = 600) -> None:
    """Let the fonts land and any lazy image decode before the shutter.

    `networkidle` is not enough on its own: it fires while a webfont is still
    swapping, and a screenshot taken then shows the fallback stack — which is
    the one difference a reader will not be able to name but will see, because
    the page around the image is set in the real font.
    """
    with contextlib.suppress(PlaywrightTimeout):
        page.wait_for_load_state("networkidle", timeout=8000)
    page.evaluate("document.fonts && document.fonts.ready")
    page.wait_for_timeout(ms)


def _shoot(page: Page, path: Path) -> None:
    page.screenshot(path=str(path))


def capture(page: Page, raw: Path, base: str, card_id: int | None) -> dict[str, Path]:
    """The three screens, in the order the landing page makes their claims."""
    shots: dict[str, Path] = {}

    # 1. The queue. The hero, and the one screen that is the argument: a scan
    #    beside the card it matched, with the runners-up visible, so the top
    #    match reads as evidence rather than as an assertion.
    page.goto(f"{base}/app")
    _settle(page)
    shots["queue"] = raw / "queue.png"
    _shoot(page, shots["queue"])

    # 2. A card, in a taller window rather than a scrolled one.
    #
    #    This screen's two columns share one scroller, so scrolling far enough
    #    to reach the price trend on the left also drags the copies table off
    #    the top on the right — the first take had a correct trend beside a
    #    blank white half-frame and a batch heading sliced through the middle.
    #    A taller viewport gets both without moving anything, which is also the
    #    honest picture: this is what the screen looks like.
    if card_id is not None:
        page.set_viewport_size({"width": WIDTH, "height": CARD_HEIGHT})
        page.goto(f"{base}/inventory/{card_id}")
        _settle(page)
        shots["card"] = raw / "card.png"
        _shoot(page, shots["card"])
        page.set_viewport_size({"width": WIDTH, "height": HEIGHT})
    else:
        print("landing: no card with history, skipping card.webp", file=sys.stderr)

    # 3. The listing run, reached the way a person reaches it: select the
    #    inventory, then take it to the export screen. Going straight to
    #    /listings gets the empty state, which illustrates nothing.
    #
    #    Not scrolled either. The top of this screen is the channel picker, and
    #    the channel picker carries the sentence the section around it is
    #    making — "csv upload · no api key held" — so scrolling past it to
    #    reach the download button trades the argument for the button.
    page.goto(f"{base}/inventory")
    _settle(page)
    for selector in ("#all", "#listbtn"):
        target = page.query_selector(selector)
        if target is None:
            print(f"landing: no {selector}, shooting /listings directly", file=sys.stderr)
            page.goto(f"{base}/listings")
            break
        target.click()
        page.wait_for_timeout(500)
    _settle(page)
    shots["listings"] = raw / "listings.png"
    _shoot(page, shots["listings"])

    return shots


def _data_uri(path: Path, mime: str) -> str:
    """Inline an asset, because a `file://` one will not load here.

    The card is built with `set_content`, so the page's origin is `about:blank`
    and every `file://` subresource is blocked as a cross-origin request. It
    fails silently, in the way that matters most: the first card rendered with
    a broken-image glyph where the wordmark should be, and would have gone out
    as the thumbnail on every link to the site.
    """
    if not path.exists():
        return ""
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def compose_og(page: Page, out: Path, games: int, static: Path) -> None:
    """The link-preview card, drawn rather than screenshotted. See the module docstring."""
    page.set_viewport_size({"width": OG_WIDTH, "height": OG_HEIGHT})
    page.set_content(
        OG_CARD
        % {
            "w": OG_WIDTH,
            "h": OG_HEIGHT,
            "mark": _data_uri(static / "brand" / "mark.svg", "image/svg+xml"),
            "mono": _data_uri(static / "fonts" / "jetbrains-mono-latin.woff2", "font/woff2"),
            "games": games,
        }
    )
    _settle(page, 400)
    # PNG, not WebP. This one is fetched by other people's crawlers, and the
    # long tail of them still does not take WebP — a preview that fails is
    # invisible rather than ugly, so it is not the place to save 40 KB.
    page.screenshot(path=str(out / "og.png"))


def to_webp(shots: dict[str, Path], out: Path, quality: int) -> None:
    from PIL import Image

    out.mkdir(parents=True, exist_ok=True)
    for name, src in shots.items():
        target = out / f"{name}.webp"
        Image.open(src).convert("RGB").save(target, quality=quality, method=6)
        print(f"  {target}  {target.stat().st_size / 1024:.0f} KB")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default=os.getenv("FOILSTACK_SHOT_URL", "http://localhost:8090"))
    ap.add_argument("--out", type=Path, default=Path("src/foilstack/web/static/shots"))
    ap.add_argument("--email", default=os.getenv("FOILSTACK_SHOT_EMAIL"))
    ap.add_argument("--password", default=os.getenv("FOILSTACK_SHOT_PASSWORD"))
    ap.add_argument("--card", type=int, default=None, help="the stock line to open")
    ap.add_argument("--games", type=int, default=None, help="games named on the og card")
    ap.add_argument("--scale", type=int, default=DEVICE_SCALE)
    ap.add_argument("--quality", type=int, default=WEBP_QUALITY)
    args = ap.parse_args(argv)

    if args.games is None:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        from foilstack.plugins import supported_games

        args.games = len(supported_games())

    static = Path(__file__).resolve().parents[1] / "src" / "foilstack" / "web" / "static"
    raw = Path(tempfile.mkdtemp(prefix="foilstack-shots-"))
    args.out.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            viewport={"width": WIDTH, "height": HEIGHT},
            device_scale_factor=args.scale,
        )
        page = context.new_page()

        if args.email:
            page.goto(f"{args.url}/login")
            page.fill('input[name="email"]', args.email)
            page.fill('input[name="password"]', args.password or "")
            page.click('button[type="submit"]')
            page.wait_for_load_state("networkidle")
            if "login" in page.url:
                print("landing: sign-in failed", file=sys.stderr)
                browser.close()
                return 1

        shots = capture(page, raw, args.url, args.card)
        to_webp(shots, args.out, args.quality)

        # The og card last: it resizes the viewport, so anything shot after it
        # would come out 1200x630 without saying so.
        compose_og(page, args.out, args.games, static)
        print(f"  {args.out / 'og.png'}  {(args.out / 'og.png').stat().st_size / 1024:.0f} KB")

        context.close()
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

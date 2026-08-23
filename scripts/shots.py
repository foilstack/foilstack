"""Screenshot the running app, signed in.

Exists because several UI bugs shipped that no test could have caught — an
oversized thumbnail, an unlabelled image column — and were only found by a
person looking at the page. This is the cheapest way to look at it.

    uv run python scripts/shots.py [--url http://localhost:8090] [--out ./shots]

Creates a throwaway account on the target unless FOILSTACK_SHOT_EMAIL and
FOILSTACK_SHOT_PASSWORD are set, so point it at a dev instance by preference.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

SHOTS = [
    ("import", "/app", None),
    ("inventory", "/inventory", None),
    ("listings", "/listings", None),
    ("analytics", "/analytics", None),
    ("plugins", "/plugins", None),
]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default=os.getenv("FOILSTACK_SHOT_URL", "http://localhost:8090"))
    ap.add_argument("--out", default="./shots", type=Path)
    ap.add_argument("--email", default=os.getenv("FOILSTACK_SHOT_EMAIL"))
    ap.add_argument("--password", default=os.getenv("FOILSTACK_SHOT_PASSWORD"))
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--height", type=int, default=900)
    ap.add_argument("--only", default=None, help="shoot just this screen by name")
    ap.add_argument("--card", type=int, default=None, help="also shoot /inventory/<id>")
    args = ap.parse_args(argv)

    args.out.mkdir(parents=True, exist_ok=True)
    shots = list(SHOTS)
    if args.card:
        shots.append(("card", f"/inventory/{args.card}", None))
    if args.only:
        shots = [s for s in shots if s[0] == args.only]

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": args.width, "height": args.height})

        page.goto(f"{args.url}/login")
        if "login" in page.url and args.email:
            page.fill("input[name=email]", args.email)
            page.fill("input[name=password]", args.password or "")
            page.click("button[type=submit]")
            page.wait_for_load_state("networkidle")
            if "login" in page.url:
                print("sign-in failed — check --email/--password", file=sys.stderr)
                browser.close()
                return 1

        for name, path, _ in shots:
            page.goto(f"{args.url}{path}")
            page.wait_for_load_state("networkidle")
            target = args.out / f"{name}.png"
            page.screenshot(path=str(target))
            print(f"  {target}")

        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

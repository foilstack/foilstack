"""Check every TCGplayer category id against the name upstream gives it.

A wrong id is invisible without this. It is a real category, it returns a real
catalogue, and every card in it is real — just from a different game. Three of
the eleven ids in this file were wrong for months: `gundam` fetched hololive,
`dragonball` fetched Neopets Battledome, `finalfantasy` fetched Godzilla. The
comment above them said they had been read from the categories endpoint rather
than guessed, which is how they went unexamined.

    uv run python scripts/check_categories.py

Exits non-zero if any name and id disagree. Not part of the test suite: it
needs the network, and a run that fails because upstream is down is a test that
cries wolf. Run it when adding a game.
"""

from __future__ import annotations

import sys

import httpx

sys.path.insert(0, "src")

from foilstack.plugins.sources.tcgcsv import CATEGORIES, HEADERS, PRODUCT_LINES

# The word that must appear in the upstream name for the mapping to be
# credible. Only needed where our short name is not a substring of theirs.
EXPECTED: dict[str, str] = {
    "yugioh": "yugioh",
    "onepiece": "one piece",
    "fleshandblood": "flesh & blood",
    "starwars": "star wars",
    "finalfantasy": "final fantasy",
    "dragonballz": "dragon ball z",
    "dragonballsuper": "dragon ball super ccg",
    "dragonballfusion": "fusion world",
}


def main() -> int:
    # Their endpoint answers 401 to a client that does not say who it is,
    # so this borrows the plugin's own headers rather than inventing a
    # second identity for the same project.
    response = httpx.get("https://tcgcsv.com/tcgplayer/categories", headers=HEADERS, timeout=30.0)
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("results", payload) if isinstance(payload, dict) else payload
    by_id = {row["categoryId"]: row["name"] for row in rows}

    bad = 0
    for name, category_id in sorted(CATEGORIES.items()):
        upstream = by_id.get(category_id)
        expected = EXPECTED.get(name, name)
        if upstream is None:
            print(f"  {name:<18} {category_id:>4}  NO SUCH CATEGORY")
            bad += 1
        elif expected.lower() not in upstream.lower():
            print(f"  {name:<18} {category_id:>4}  {upstream}   <-- expected {expected!r}")
            bad += 1
        else:
            print(f"  {name:<18} {category_id:>4}  {upstream}")

    # The same id, read a second way. `PRODUCT_LINES` is what goes in the
    # `Product Line` column of a TCGplayer upload, and it has to be upstream's
    # category name *exactly* — the substring test above would pass on
    # "Magic: The Gathering" while the uploader rejected it.
    for name, category_id in sorted(CATEGORIES.items()):
        upstream = by_id.get(category_id)
        line = PRODUCT_LINES.get(name)
        if line is None:
            print(f"  {name:<18} no Product Line")
            bad += 1
        elif upstream is not None and line != upstream:
            print(f"  {name:<18} Product Line {line!r} != {upstream!r}")
            bad += 1

    print(f"\n{2 * len(CATEGORIES) - bad}/{2 * len(CATEGORIES)} correct")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""The round trip through a TCGplayer pricing export.

TCGplayer identifies a listing by SKU id — one per product *and* condition
*and* printing, so 10th Edition Abundance is product 15023 but SKU 4519 near
mint and 4521 near mint foil. This catalogue comes from TCGCSV, which states
outright that it does not publish SKUs, so foilstack has no id to write and an
upload built from our own columns identifies nothing.

The seller does have them. `Export Filtered CSV` on the pricing screen returns
every SKU in a product line with its id in the first column, and TCGplayer's
own documented workflow is to edit that file and send it back. So this module
takes their export as the starting point: it finds our stock in their rows,
writes the two columns that are ours to write, and returns those rows and
nothing else.

Starting from their file rather than composing our own has a second benefit
worth stating, because it is not obvious: every informative column comes back
right. Rarity, Photo URL and the three price columns are theirs, carried
through untouched, and the header row is theirs by construction — which is the
one thing the uploader checks before it reads anything else.

Nothing here reads the quantities in the uploaded file. They are the seller's
positions on another marketplace and none of foilstack's business; the only
columns this writes are `Add to Quantity` and `TCG Marketplace Price`.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

# The header row TCGplayer exports and the only one its uploader accepts. Kept
# here as well as in `plugins/exports/tcgplayer.toml` on purpose: this one is
# what an *uploaded* file is checked against, and the check has to fail loudly
# on the wrong file — a seller who uploads an order export instead of a pricing
# export should be told that, not handed an empty CSV.
HEADER = [
    "TCGplayer Id",
    "Product Line",
    "Set Name",
    "Product Name",
    "Title",
    "Number",
    "Rarity",
    "Condition",
    "TCG Market Price",
    "TCG Direct Low",
    "TCG Low Price With Shipping",
    "TCG Low Price",
    "Total Quantity",
    "Add to Quantity",
    "TCG Marketplace Price",
    "Photo URL",
]

# A whole Magic product line exports at about 100 MB, so anything below a few
# hundred is a ceiling that rejects the ordinary case. It exists because the
# size of an uploaded file is chosen by whoever is signed in, not because any
# real export approaches it.
MAX_UPLOAD_BYTES = 512 * 1024 * 1024

# `Total Quantity` *sets* the count and `Add to Quantity` raises it. foilstack
# knows what came out of one import, never what the seller already has listed,
# so it may only ever add — a total written from a partial picture deletes the
# rest of their stock. Their own value is therefore carried straight through.
#
# But it may not be left *blank* on a row being imported, which their export
# leaves it on every row they hold none of. So a blank one becomes an explicit
# zero, which says the same thing in the only spelling the uploader accepts.
# Only a blank one: a row where they already hold five must keep saying five,
# or the add is applied to a total we just invented.
TOTAL_COLUMN = "Total Quantity"
QUANTITY_COLUMN = "Add to Quantity"
PRICE_COLUMN = "TCG Marketplace Price"

Key = tuple[str, str, str, str, str]


class NotAPricingExport(ValueError):
    """The uploaded file is not the export this expects."""


@dataclass
class MatchReport:
    """What became of each stock line, so the seller is told rather than shown
    a shorter file than they expected."""

    matched: int = 0
    unpriced: list[str] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    ambiguous: list[str] = field(default_factory=list)

    @property
    def skipped(self) -> int:
        return len(self.unpriced) + len(self.unmatched) + len(self.ambiguous)

    def summary(self) -> str:
        parts = [f"{self.matched} matched"]
        if self.unmatched:
            parts.append(f"{len(self.unmatched)} not in the export")
        if self.unpriced:
            parts.append(f"{len(self.unpriced)} with no price")
        if self.ambiguous:
            parts.append(f"{len(self.ambiguous)} ambiguous")
        return " · ".join(parts)


def key_for(row: dict[str, Any]) -> Key:
    """The five columns that identify a listing, from one of our export rows.

    There is no id in common with TCGplayer — theirs is a SKU id, ours is a
    product id — so the join is on what both sides describe. Measured against
    a real 800,344-row export, those five columns are unique for all but 24
    keys, and `tcg_name` is the raw upstream spelling rather than the cleaned
    one precisely so that this matches: on `name` the same test loses one card
    in ten to punctuation alone.
    """
    return (
        str(row.get("tcg_product_line") or ""),
        str(row.get("set_name") or ""),
        str(row.get("tcg_name") or ""),
        str(row.get("number") or ""),
        str(row.get("tcg_condition") or ""),
    )


def _label(row: dict[str, Any]) -> str:
    bits = [str(row.get("tcg_name") or "?"), str(row.get("set_name") or "")]
    return f"{' · '.join(b for b in bits if b)} ({row.get('tcg_condition') or '?'})"


def fill(upload: Iterable[bytes], rows: list[dict[str, Any]]) -> tuple[str, MatchReport]:
    """Their export in, their rows back out with our quantities and prices on.

    The uploaded file is streamed and never held: a whole product line is
    around 800,000 rows and 100 MB, and the only rows worth keeping are the few
    hundred that match. So the lookup is built from *our* inventory and their
    file is scanned once against it, which makes peak memory a function of what
    the seller owns rather than of how much Magic exists.
    """
    report = MatchReport()

    wanted: dict[Key, dict[str, Any]] = {}
    for row in rows:
        if row.get("list_price") is None:
            # TCGplayer requires a marketplace price of at least a cent on
            # every row it imports, so a card we hold no price for cannot go in
            # the file at all. Saying so beats sending a row that fails.
            report.unpriced.append(_label(row))
            continue
        wanted[key_for(row)] = row

    found: dict[Key, list[str] | None] = {}
    for their in _read(upload):
        key = (
            their["Product Line"],
            their["Set Name"],
            their["Product Name"],
            their["Number"],
            their["Condition"],
        )
        if key not in wanted:
            continue
        if key in found:
            # The same five columns twice in their own export. 24 keys of
            # 800,318 in a real file, all of them proxy printings. Two ids
            # answer to one description and there is no evidence here for
            # which, so neither is written: an unlisted card is a card the
            # seller can list by hand, and a wrong SKU id edits a listing they
            # did not mean to touch.
            found[key] = None
            continue
        ours = wanted[key]
        line = [their[column] for column in HEADER]
        line[HEADER.index(QUANTITY_COLUMN)] = str(int(ours["quantity"]))
        line[HEADER.index(PRICE_COLUMN)] = f"{float(ours['list_price']):.2f}"
        if not line[HEADER.index(TOTAL_COLUMN)].strip():
            line[HEADER.index(TOTAL_COLUMN)] = "0"
        found[key] = line

    # Written the way their own export writes it: CRLF, every data field
    # quoted, the header row bare. None of that is required by CSV and all of
    # it is free, and the uploader on the other end is one we cannot test
    # against — so the file it receives may as well be shaped exactly like the
    # file it produced.
    buf = io.StringIO()
    buf.write(",".join(HEADER) + "\r\n")
    writer = csv.writer(buf, lineterminator="\r\n", quoting=csv.QUOTE_ALL)
    for key, matched in found.items():
        if matched is None:
            report.ambiguous.append(_label(wanted[key]))
            continue
        writer.writerow(matched)
        report.matched += 1
    for key, row in wanted.items():
        if key not in found:
            report.unmatched.append(_label(row))

    return buf.getvalue(), report


def _read(upload: Iterable[bytes]) -> Iterator[dict[str, str]]:
    """Their rows, with the header checked before any of them are trusted.

    A file with the wrong header is the common mistake — the pricing screen
    offers several exports and only one of them is this — and it has to be
    named as such. Reading on regardless would produce a valid, empty CSV and
    a seller with no idea why their inventory vanished.
    """
    reader = csv.reader(line.decode("utf-8-sig", "replace") for line in _lines(upload))
    try:
        header = next(reader)
    except StopIteration:
        raise NotAPricingExport("that file is empty") from None
    if [h.strip() for h in header] != HEADER:
        raise NotAPricingExport(
            "that is not a TCGplayer pricing export. Use Export Filtered CSV on "
            "the Pricing screen — the file whose first column is TCGplayer Id."
        )
    for values in reader:
        if len(values) != len(HEADER):
            continue
        yield dict(zip(HEADER, values, strict=True))


def _lines(chunks: Iterable[bytes]) -> Iterator[bytes]:
    """Byte chunks re-cut on newlines, so `csv` sees whole rows.

    An upload arrives in fixed-size blocks that fall wherever they fall, and a
    block boundary lands mid-row roughly always. Splitting here rather than
    reading the file into memory is the whole reason a 100 MB upload costs
    nothing to process.
    """
    tail = b""
    for chunk in chunks:
        tail += chunk
        *complete, tail = tail.split(b"\n")
        yield from (line + b"\n" for line in complete)
    if tail:
        yield tail

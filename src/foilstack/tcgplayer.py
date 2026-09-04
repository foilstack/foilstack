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

The quantities in their file are read, and for one reason: `Add to Quantity` is
a delta. The number that brings a listing to our stock level is our stock minus
what they already hold, and writing our stock there instead adds it a second
time on every run after the first — three copies with one already listed goes
out as a "3", lands on their "1", and offers four cards that cannot all be
shipped. An oversell is a cancellation and a mark against the seller, so this
is the one column of theirs worth reading.

The column takes a negative, which makes the file a sync rather than an append:
a copy sold or discarded here comes down there on the next run, and uploading
the same file twice is a no-op the second time.
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

# `Total Quantity` *sets* the count and `Add to Quantity` moves it. We write the
# second, computed from the first, and hand the first back unchanged — it is the
# number the delta was measured against, and any other value there applies our
# delta to a count nobody took.
#
# It may not be left *blank* on a row being imported, which their export leaves
# it on every row they hold none of. Blank is their spelling of zero and it goes
# out as an explicit `0`, which says the same thing in the spelling the uploader
# accepts.
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
    unreadable: list[str] = field(default_factory=list)
    # Not a skip — these are in the file. They are called out because a
    # negative delta takes a live listing down, which is the one thing the
    # round trip does that the seller did not ask for card by card.
    reduced: list[str] = field(default_factory=list)

    @property
    def skipped(self) -> int:
        return len(self.unpriced) + len(self.unmatched) + len(self.ambiguous) + len(self.unreadable)

    def summary(self) -> str:
        parts = [f"{self.matched} matched"]
        if self.reduced:
            parts.append(f"{len(self.reduced)} reduced")
        if self.unmatched:
            parts.append(f"{len(self.unmatched)} not in the export")
        if self.unpriced:
            parts.append(f"{len(self.unpriced)} with no price")
        if self.ambiguous:
            parts.append(f"{len(self.ambiguous)} ambiguous")
        if self.unreadable:
            parts.append(f"{len(self.unreadable)} with an unreadable quantity")
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

    # A row to write, or the word for why there is not one.
    found: dict[Key, list[str] | str] = {}
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
            found[key] = "ambiguous"
            continue
        ours = wanted[key]
        total = _their_total(their[TOTAL_COLUMN])
        if total is None:
            found[key] = "unreadable"
            continue
        line = [their[column] for column in HEADER]
        # The delta, not the stock line. A row already at our level gets a 0
        # and stays in the file, because the price beside it is still ours to
        # write and a listing at the right quantity and the wrong price is
        # what the seller came here to fix.
        line[HEADER.index(QUANTITY_COLUMN)] = str(int(ours["quantity"]) - total)
        line[HEADER.index(PRICE_COLUMN)] = f"{float(ours['list_price']):.2f}"
        line[HEADER.index(TOTAL_COLUMN)] = str(total)
        found[key] = line

    # Written the way their own export writes it: CRLF, every data field
    # quoted, the header row bare. None of that is required by CSV and all of
    # it is free, and the uploader on the other end is one we cannot test
    # against — so the file it receives may as well be shaped exactly like the
    # file it produced.
    buf = io.StringIO()
    buf.write(",".join(HEADER) + "\r\n")
    writer = csv.writer(buf, lineterminator="\r\n", quoting=csv.QUOTE_ALL)
    refused = {"ambiguous": report.ambiguous, "unreadable": report.unreadable}
    for key, outcome in found.items():
        if isinstance(outcome, str):
            refused[outcome].append(_label(wanted[key]))
            continue
        writer.writerow(outcome)
        report.matched += 1
        if outcome[HEADER.index(QUANTITY_COLUMN)].startswith("-"):
            report.reduced.append(_label(wanted[key]))
    for key, row in wanted.items():
        if key not in found:
            report.unmatched.append(_label(row))

    return buf.getvalue(), report


def _their_total(raw: str) -> int | None:
    """How many they already hold, from their own column. None if it will not read.

    Tolerant, because the file has usually been through a spreadsheet by the
    time it comes back and `2`, ` 2 `, `2.0` and `1,024` all mean a number the
    uploader would take. Anything else is not a count — and reading it as zero
    is exactly the oversell the delta exists to prevent, so the row is dropped
    and named instead of guessed at.
    """
    text = raw.strip().replace(",", "")
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return None


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

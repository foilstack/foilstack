"""The two plugin contracts.

**Source plugins** supply a catalogue: which cards exist, what they look like
and what they are worth. **Export plugins** turn inventory into a file some
marketplace will accept.

The split is not symmetric, and the asymmetry is the point.

A source plugin must run code — it talks to a remote API, paginates, and
normalises whatever shape that API happens to have. It is therefore the only
place a plugin can do something dangerous, and installing one is an explicit,
deliberate act.

An export plugin is a column mapping. Expressing it as data rather than code
means the common case — "TCGplayer wants these nine columns in this order" —
is a file a non-programmer can write and, more importantly, that a reviewer can
read in ten seconds and be certain does nothing else. See `exports.py`.

Every field on `CardRecord` is required except where noted, and `image_url` is
required *on purpose*: matching is nearest-neighbour search over reference
images, so a source that yields rows without images produces cards this
application structurally cannot find. Better to fail when the plugin is written
than after someone ingests four hundred thousand of them.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class PriceRecord:
    """One printing's prices, as a source plugin sees them.

    Keyed by sub-type because that is what a printing *is* to a marketplace:
    Normal, Foil and Reverse Holofoil are the same artwork at wildly different
    money, and averaging them into one number prices a foil like a bulk common.

    Every field is optional except the key. An unpriced printing is a fact
    worth recording — it means nobody is selling one — and dropping the row
    would lose the printing along with the price.
    """

    source_id: str  # matches CardRecord.source_id, before namespacing
    sub_type: str  # "Normal", "Foil", "Reverse Holofoil", …
    market: float | None = None
    low: float | None = None
    mid: float | None = None
    high: float | None = None

    def __post_init__(self) -> None:
        if not self.source_id or not self.sub_type:
            raise ValueError("source_id and sub_type are required")


@dataclass(frozen=True)
class CardRecord:
    """One printing, as a source plugin sees it."""

    source_id: str  # unique within the plugin; namespaced on the way in
    name: str
    game: str
    image_url: str  # required: no image, no matching
    set_name: str | None = None
    number: str | None = None
    variant: str | None = None
    market: float | None = None
    currency: str = "USD"

    def __post_init__(self) -> None:
        if not self.source_id or not self.name or not self.game:
            raise ValueError("source_id, name and game are required")
        if not self.image_url:
            raise ValueError(
                f"{self.source_id}: image_url is required — a card with no "
                "reference image cannot be matched against a scan"
            )


@runtime_checkable
class SourcePlugin(Protocol):
    """Supplies catalogue rows.

    Runs on the user's machine and fetches from upstream there. This project
    redistributes no card data: we ship the code that knows how to ask.
    """

    name: str
    games: list[str]

    def fetch(self, limit: int | None = None) -> AsyncIterator[CardRecord]: ...

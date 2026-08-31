"""The three plugin contracts.

**Source plugins** supply a catalogue: which cards exist, what they look like
and what they are worth. **Enrichment plugins** add to a catalogue somebody
else ingested. **Export plugins** turn inventory into a file some marketplace
will accept.

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

import datetime as dt
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
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
    # The raw spelling, where the source cleans one and keeps both. Optional
    # because most sources have only one name to give; supply it when yours
    # does, because a marketplace file joins on the raw form. See `Card`.
    source_name: str | None = None
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
                f"{self.source_id}: image_url is required, because a card with no "
                "reference image cannot be matched against a scan"
            )


@runtime_checkable
class SourcePlugin(Protocol):
    """Supplies catalogue rows.

    Runs on the user's machine and fetches from upstream there. This project
    redistributes no card data: we ship the code that knows how to ask.
    """

    name: str
    #: Every game this plugin can fetch.
    games: list[str]
    #: Human names for those slugs, where the slug is not one. `dragonballz`
    #: and `fleshandblood` are cache keys and CLI arguments, not something to
    #: show a reader, and the landing page names the games this can do — so
    #: somewhere has to hold "Flesh and Blood". It lives beside the slugs it
    #: translates rather than in the template, because the plugin that adds a
    #: game is the thing that knows what the game is called. Optional: a slug
    #: with no entry is title-cased, which is right for "pokemon" and "magic".
    labels: dict[str, str]
    #: The one it was constructed for. Read by the CLI when re-constructing a
    #: plugin for a different set, so it is part of the contract rather than an
    #: implementation detail.
    game: str

    # How the application constructs one. Declared because it does: the CLI
    # builds `type(plugin)(game=..., set_code=...)` for every command, and a
    # protocol that leaves the constructor out describes an interface nobody
    # can actually implement against.
    def __init__(self, game: str = ..., set_code: str | None = ...) -> None: ...

    def fetch(self, limit: int | None = None) -> AsyncIterator[CardRecord]: ...


@dataclass(frozen=True)
class PriceHistoryRecord:
    """One printing's price on one past day.

    Deliberately not a `PriceRecord` with a date bolted on. A `PriceRecord` is
    today's four figures and it overwrites `card_prices`; this is one number on
    one day that has already happened, and it is only ever appended to history.
    Giving them the same type would make it possible to write a backfill into
    the current-price table by passing the wrong object, and the current-price
    table is the one a listing is priced from.

    `market` alone, and nullable, because the sources that can supply a past
    day supply one number for it rather than the low/mid/high spread a live
    endpoint returns. A backfill that invented the other three would put made-up
    figures in the one table this project cannot rebuild.
    """

    source_id: str  # the upstream catalogue's product id, before namespacing
    sub_type: str  # "Normal", "Foil", … — named as the *catalogue* names it
    recorded_on: dt.date
    market: float | None = None

    def __post_init__(self) -> None:
        if not self.source_id or not self.sub_type:
            raise ValueError("source_id and sub_type are required")


@runtime_checkable
class EnrichmentPlugin(Protocol):
    """Adds to a catalogue somebody else ingested.

    The third contract, and the asymmetry with `SourcePlugin` is the point. A
    source *is* a catalogue: it decides which cards exist and it must supply an
    image for every one of them, because a row without an image is a card this
    application structurally cannot match. An enricher owns no rows. It joins
    onto cards already present, by the upstream id the source recorded, and it
    is allowed to know nothing about images at all.

    That distinction is what lets MTGJSON in. It publishes no card imagery —
    its own documentation sends you to Scryfall for that — so it can never
    satisfy `CardRecord.image_url` and would be rejected by the source contract
    on principle. What it has is three months of daily prices for a catalogue
    that otherwise remembers only the days since you installed this.

    `matches_source` is how the join is made honest. MTGJSON keys its prices by
    TCGplayer product id, which is meaningless against a catalogue ingested
    from anywhere else, and a product id that happens to collide with another
    upstream's integers would write one game's prices onto another's cards. So
    an enricher names the source it can speak to, and the writer refuses any
    other.
    """

    name: str
    #: Every game this plugin can enrich. Narrower than a source's, usually:
    #: MTGJSON is a Magic project and claims nothing else.
    games: list[str]
    #: Human names for those slugs, as `SourcePlugin.labels`.
    labels: dict[str, str]
    #: The source plugin whose `source_id` values this one's ids line up with.
    matches_source: str
    #: The one it was constructed for.
    game: str

    def __init__(self, game: str = ..., cache_dir: Path | None = ...) -> None: ...

    #: Yields every day upstream holds. A single printing's series must
    #: arrive in ascending date order — the writer collapses it to the days the
    #: number moved, and "same as the one before" only means anything forwards.
    def price_history(self) -> AsyncIterator[PriceHistoryRecord]: ...

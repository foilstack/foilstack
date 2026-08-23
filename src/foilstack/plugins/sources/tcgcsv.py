"""TCGCSV — the reference source plugin.

Deliberately the first thing built against the plugin contract rather than a
special case inside the application. If the primary data source cannot be
expressed through the same interface a community plugin has to use, the
interface is wrong, and it is much cheaper to learn that now.

Constants come from https://tcgcsv.com/docs. Prices are TCGplayer *market*
prices for a printing, which is a real observed number and not an appraisal.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import ClassVar

import httpx

from foilstack.plugins.base import CardRecord, PriceRecord


class RateLimited(RuntimeError):
    """Upstream asked us to slow down."""


BASE = "https://tcgcsv.com/tcgplayer"
LAST_UPDATED = "https://tcgcsv.com/last-updated.txt"

# Their documented pacing: "Include a time.sleep(100ms) in your update loop. If
# you exceed a reasonable request-per-second threshold, your IP will be
# temporarily throttled for 10 minutes." Applications over 10,000 requests in
# 24 hours "may be banned".
#
# This is somebody's free service mirroring data we would otherwise have to buy
# access to. Being a good neighbour is cheap and the alternative is losing it.
REQUEST_DELAY = 0.1

# TCGCSV answers 401 to requests that do not identify themselves — the default
# httpx user-agent is rejected outright. Send a real name and a version.
USER_AGENT = "foilstack/0.1.0 (+https://github.com/foilstack/foilstack)"
HEADERS = {"User-Agent": USER_AGENT, "Accept": "*/*"}

# TCGplayer category ids, read from the categories endpoint rather than guessed.
CATEGORIES: dict[str, int] = {
    "pokemon": 3,
    "magic": 1,
    "yugioh": 2,
    "lorcana": 71,
    "onepiece": 68,
    "digimon": 63,
    "starwars": 79,
    "fleshandblood": 62,
    "gundam": 87,
    "dragonball": 84,
    "finalfantasy": 88,
}


def _is_card(product: dict) -> bool:
    """Cards have a Number or a Rarity; sealed product has neither.

    Straight from the TCGCSV docs. Without this the catalogue fills with
    booster boxes and tins, which have images and therefore get encoded — so a
    scan of a real card can be "matched" against a photograph of a sealed box.
    """
    return _extended(product, "Number") is not None or _extended(product, "Rarity") is not None


def _extended(product: dict, field: str) -> str | None:
    for item in product.get("extendedData") or []:
        if item.get("name") == field:
            value = item.get("value")
            return str(value) if value not in (None, "") else None
    return None


class TCGCSVSource:
    name = "tcgcsv"
    games: ClassVar[list[str]] = list(CATEGORIES)

    def __init__(self, game: str = "pokemon", set_code: str | None = None) -> None:
        if game not in CATEGORIES:
            raise ValueError(f"unknown game {game!r}; known: {sorted(CATEGORIES)}")
        self.game = game
        # Restricts the fetch to one set. Not a nicety: TCGplayer's Magic
        # category is well over a hundred thousand printings, and encoding all
        # of them is a day of GPU time. A seller listing one set wants that set.
        self.set_code = (set_code or "").strip().lower() or None

    async def sets(self) -> list[dict]:
        """Every set in this game, newest first.

        Exposed separately from `fetch` so a caller can show the list and let
        someone choose, rather than guessing an abbreviation and waiting to
        find out it matched nothing.
        """
        category = CATEGORIES[self.game]
        async with httpx.AsyncClient(timeout=60.0, headers=HEADERS) as client:
            groups = await self._get(client, f"{BASE}/{category}/groups")
        groups.sort(key=lambda g: g.get("publishedOn") or "", reverse=True)
        return [
            {
                "group_id": g.get("groupId"),
                "name": g.get("name"),
                "abbreviation": g.get("abbreviation"),
                "published_on": (g.get("publishedOn") or "")[:10],
            }
            for g in groups
            if g.get("groupId") is not None
        ]

    def _matches_set(self, group: dict) -> bool:
        if self.set_code is None:
            return True
        wanted = self.set_code
        abbr = (group.get("abbreviation") or "").strip().lower()
        name = (group.get("name") or "").strip().lower()
        return wanted in (abbr, name, str(group.get("groupId")))

    async def fetch(self, limit: int | None = None) -> AsyncIterator[CardRecord]:
        category = CATEGORIES[self.game]
        yielded = 0
        async with httpx.AsyncClient(timeout=60.0, headers=HEADERS) as client:
            groups = await self._get(client, f"{BASE}/{category}/groups")
            # Oldest first. Brand-new and preorder sets carry no market price
            # yet, so starting at the newest — which is the order upstream
            # returns — makes a `--limit` run produce a catalogue of cards with
            # no prices in it.
            groups.sort(key=lambda g: (g.get("publishedOn") or "", g.get("groupId") or 0))
            if self.set_code is not None:
                groups = [g for g in groups if self._matches_set(g)]
                if not groups:
                    # Louder than an empty run. "Ingested 0 cards" reads as a
                    # network problem; a typo'd set code is the likelier cause
                    # and the operator can only fix the one they are told about.
                    raise ValueError(
                        f"no set matching {self.set_code!r} in {self.game}; "
                        f"try `foilstack sets --game {self.game}`"
                    )
            for group in groups:
                gid = group.get("groupId")
                if gid is None:
                    continue
                products = await self._get(client, f"{BASE}/{category}/{gid}/products")
                prices = await self._get(client, f"{BASE}/{category}/{gid}/prices")

                # Market price is per printing, so key on the pair. A card with
                # several sub-types has several rows and they are not
                # interchangeable: that is the whole reason printings matter.
                by_product: dict[int, dict] = {}
                for row in prices:
                    pid = row.get("productId")
                    if pid is None:
                        continue
                    # Keep the row even when marketPrice is null: it still
                    # carries subTypeName, which is the printing. Dropping it
                    # loses the variant as well as the price, and an unpriced
                    # card is a fact worth recording rather than a reason to
                    # forget which printing it was.
                    existing = by_product.get(pid)
                    if existing is None or (
                        existing.get("marketPrice") is None and row.get("marketPrice") is not None
                    ):
                        by_product[pid] = row

                for product in products:
                    image = product.get("imageUrl")
                    number = _extended(product, "Number")
                    if not image or not _is_card(product):
                        # Sealed product and oddities. Skipping is correct:
                        # they are not cards, and encoding them puts booster
                        # boxes into the candidate list for real scans.
                        continue
                    price_row = by_product.get(product.get("productId"))
                    yield CardRecord(
                        source_id=str(product.get("productId")),
                        name=product.get("cleanName") or product.get("name") or "",
                        game=self.game,
                        set_name=group.get("name"),
                        number=number,
                        variant=(price_row or {}).get("subTypeName"),
                        image_url=image,
                        market=(price_row or {}).get("marketPrice"),
                    )
                    yielded += 1
                    if limit is not None and yielded >= limit:
                        return

    async def last_updated(self) -> str:
        """The timestamp of upstream's most recent build.

        One cheap request that decides whether a full sync is worth making at
        all. TCGCSV rebuilds exactly once a day, so a sync run against an
        unchanged timestamp is thousands of requests for nothing.
        """
        async with httpx.AsyncClient(timeout=30.0, headers=HEADERS) as client:
            response = await client.get(LAST_UPDATED)
        response.raise_for_status()
        return response.text.strip()

    async def fetch_prices(self) -> AsyncIterator[PriceRecord]:
        """Every printing's prices, one group at a time.

        Separate from `fetch` and much cheaper: one request per group instead
        of two, and no product metadata to re-parse. A daily price sync should
        not have to re-read the whole catalogue to find out a card moved a
        nickel.
        """
        category = CATEGORIES[self.game]
        async with httpx.AsyncClient(timeout=60.0, headers=HEADERS) as client:
            groups = await self._get(client, f"{BASE}/{category}/groups")
            if self.set_code is not None:
                groups = [g for g in groups if self._matches_set(g)]
            for group in groups:
                gid = group.get("groupId")
                if gid is None:
                    continue
                for row in await self._get(client, f"{BASE}/{category}/{gid}/prices"):
                    pid, sub = row.get("productId"), row.get("subTypeName")
                    if pid is None or not sub:
                        continue
                    yield PriceRecord(
                        source_id=str(pid),
                        sub_type=str(sub),
                        market=row.get("marketPrice"),
                        low=row.get("lowPrice"),
                        mid=row.get("midPrice"),
                        high=row.get("highPrice"),
                    )

    @staticmethod
    async def _get(client: httpx.AsyncClient, url: str) -> list[dict]:
        # Paced, not hammered — see REQUEST_DELAY. Before the request rather
        # than after, so it applies however the caller loops.
        await asyncio.sleep(REQUEST_DELAY)
        response = await client.get(url)
        if response.status_code == 429:
            # Their throttle lasts about ten minutes. Retrying inside it just
            # deepens the hole, so stop and let the operator come back.
            raise RateLimited(
                f"rate limited by tcgcsv.com — wait ~10 minutes before retrying ({url})"
            )
        response.raise_for_status()
        payload = response.json()
        return payload.get("results") or []


PLUGIN = TCGCSVSource()

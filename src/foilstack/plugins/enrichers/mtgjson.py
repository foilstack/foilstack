"""MTGJSON — three months of Magic prices the catalogue would otherwise forget.

This exists because of a rule stated elsewhere in this repository as an
absolute: price history cannot be backfilled. TCGCSV mirrors the current day
and publishes no historical endpoint, so a day `sync-prices` does not run is a
day gone permanently. For Magic — and only Magic — that is not quite true.
MTGJSON publishes a rolling ninety days of daily prices, and its TCGplayer
series is the same quantity our `market` column holds.

That claim was measured rather than assumed, against a 113k-card catalogue and
the same day's live prices: 85.2% of 150,344 printings matched `market`
exactly, 96.9% within 5%. Against our other columns the exact-match rate was
9.4% for `mid`, 0.9% for `low` and 0.06% for `high`. The gap is a clock, not a
different figure — MTGJSON builds at 1:00 AM EST and publishes at 9:00 AM EST,
and two thirds of the non-exact rows equal a value we ourselves recorded on an
earlier day.

An enricher rather than a source: MTGJSON ships no card images and its own
documentation sends you to Scryfall for them, so it cannot satisfy
`CardRecord.image_url` and has no business owning catalogue rows. It joins onto
cards `tcgcsv` already ingested, by TCGplayer product id.
"""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import logging
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, ClassVar

import httpx

from foilstack import __version__
from foilstack.plugins.base import PriceHistoryRecord

log = logging.getLogger("foilstack")

BASE = "https://mtgjson.com/api/v5"
META = f"{BASE}/Meta.json"

# The two files, and why each is the one chosen.
#
# AllPrices is the only source of a past day; AllPricesToday holds one date and
# cannot backfill anything. The prices are keyed by MTGJSON's own uuid, so a
# second file has to bridge to the TCGplayer product ids `tcgcsv` stores.
# AllIdentifiers is the obvious bridge and is 218 MB; TcgplayerSkus carries the
# same mapping as a side effect of listing SKUs and is 34 MB, so it is the one
# to learn — and it is also what a future SKU-level export would need.
PRICES_FILE = "AllPrices.json.gz"
SKUS_FILE = "TcgplayerSkus.json.gz"

USER_AGENT = f"foilstack/{__version__} (+https://github.com/foilstack/foilstack)"
HEADERS = {"User-Agent": USER_AGENT, "Accept": "*/*"}

# MTGJSON's price finish keys, and the TCGplayer sub-type each is sold under.
#
# `etched` collapsing onto "Foil" is not a loss. An etched printing is a
# separate TCGplayer product with its own id, so it lands on a different
# `cards` row that TCGplayer itself calls Foil — the finish distinguishes the
# product, not the sub-type within one.
SUB_TYPES = {"normal": "Normal", "foil": "Foil", "etched": "Foil"}

# How a SKU record names the same three things. `finish` is a separate field
# from `printing` and it is the whole reason etched works: an etched SKU reads
# `printing: FOIL, finish: ETCHED`, so keying on `printing` alone both loses
# every etched printing and makes plain foil look ambiguous by folding two
# products into one bucket. Reading both recovered 1,226 etched series and cut
# genuinely ambiguous pairs from 432 to 141 out of 151,008.
_AMBIGUOUS = -1


def _sku_finish(sku: dict) -> str | None:
    if (sku.get("finish") or "").upper() == "ETCHED":
        return "etched"
    printing = (sku.get("printing") or "").upper()
    if printing == "FOIL":
        return "foil"
    if printing == "NON FOIL":
        return "normal"
    return None


def _ijson() -> Any:
    """The streaming JSON parser, imported late and explained when absent.

    `AllPrices.json` is 1.1 GB open and `json.load` on it is a machine with no
    memory left. ijson is an extra rather than a dependency because nothing
    else here needs it and a Magic-only backfill should not weigh on an install
    that only does Pokémon — but the failure has to name the fix, because
    `ModuleNotFoundError: ijson` at the top of a plugin nobody chose to install
    is a puzzle rather than an error.
    """
    try:
        import ijson
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on the install
        raise RuntimeError(
            "the mtgjson enricher needs `ijson` to stream a 1.1 GB file without "
            "loading it into memory. install it with: pip install 'foilstack[mtgjson]'"
        ) from exc
    return ijson


class MTGJSONEnricher:
    name = "mtgjson"
    games: ClassVar[list[str]] = ["magic"]
    labels: ClassVar[dict[str, str]] = {"magic": "Magic: The Gathering"}
    matches_source = "tcgcsv"

    def __init__(self, game: str = "magic", cache_dir: Path | None = None) -> None:
        if game not in self.games:
            raise ValueError(
                f"mtgjson has data for {self.games} only; {game!r} is a different game. "
                "It is a Magic project and does not claim otherwise."
            )
        self.game = game
        self.cache_dir = Path(cache_dir) if cache_dir else Path(".cache/mtgjson")
        #: Filled in as `price_history` runs, and read for the run summary. What
        #: was skipped and why is the interesting half of a backfill: a silent
        #: run that wrote less than expected is indistinguishable from one that
        #: worked, and this table is the one nobody can rebuild.
        self.stats: dict[str, int] = {}

    async def last_updated(self) -> str:
        """Upstream's build version, as `Meta.json` reports it.

        The same cheap first request `sync-prices` makes against TCGCSV, for
        the same reason: MTGJSON rebuilds once a day, and a run against a build
        already backfilled is 180 MB of download to insert nothing. Their FAQ
        warns you may be served a cached copy of any file, so this is a hint
        rather than a guarantee — which is survivable only because the writer
        can never damage a row it has already got.
        """
        async with httpx.AsyncClient(timeout=30.0, headers=HEADERS) as client:
            response = await client.get(META)
        response.raise_for_status()
        meta = response.json().get("meta") or {}
        return str(meta.get("version") or meta.get("date") or "")

    async def price_history(self) -> AsyncIterator[PriceHistoryRecord]:
        """Every daily TCGplayer price MTGJSON holds, as our catalogue names it.

        Yields the raw daily series. Deciding which of those days is worth
        keeping is the writer's job and it does it in one pass in SQL, because
        the rule — a row only where the number moved — has to be applied across
        the seam with days already recorded, and only the database knows those.
        """
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(timeout=120.0, headers=HEADERS, follow_redirects=True) as c:
            skus_path = await self._fetch(c, SKUS_FILE)
            prices_path = await self._fetch(c, PRICES_FILE)

        products = self._sku_map(skus_path)
        log.info("mtgjson: %s printings mapped to TCGplayer products", len(products))

        for record in self._read_prices(prices_path, products):
            yield record

    async def _fetch(self, client: httpx.AsyncClient, name: str) -> Path:
        """Download `name` into the cache, verified against its published hash.

        Every MTGJSON file has a `.sha256` beside it. Checking it is not
        ceremony here: this is 180 MB of download that becomes rows in the one
        table this project cannot rebuild, and a truncated gzip that happens to
        decode is a quieter failure than one that does not.

        The hash doubles as the cache key. A file already on disk that matches
        today's digest is today's file, so a re-run after a failure costs one
        small request instead of the whole download again.
        """
        expected = (await client.get(f"{BASE}/{name}.sha256")).text.strip().split()[0]
        dest = self.cache_dir / name

        if dest.exists() and _sha256(dest) == expected:
            log.info("mtgjson: %s already downloaded and verified", name)
            return dest

        log.info("mtgjson: downloading %s", name)
        digest = hashlib.sha256()
        tmp = dest.with_suffix(dest.suffix + ".part")
        with tmp.open("wb") as fh:
            async with client.stream("GET", f"{BASE}/{name}") as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes(1 << 20):
                    digest.update(chunk)
                    fh.write(chunk)
        if digest.hexdigest() != expected:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(
                f"{name} does not match its published sha256 — download was "
                "truncated or tampered with. nothing was written."
            )
        # Only now is the cached name allowed to exist, so an interrupted run
        # cannot leave a half file that the next one trusts on sight.
        tmp.replace(dest)
        return dest

    def _sku_map(self, path: Path) -> dict[str, dict[str, int]]:
        """uuid → finish → TCGplayer product id.

        A printing's foil and non-foil are usually one TCGplayer product with
        two sub-types, but for 1,711 of them they are two separate products, so
        the map is keyed per finish rather than per uuid. Getting that wrong
        files foil prices against the non-foil product.

        Where one finish resolves to several products — the same printing
        listed twice upstream — the entry is marked ambiguous and dropped
        rather than guessed. That is 141 of 151,008 priced pairs, and a wrong
        attribution here writes a real price onto the wrong card, which is
        invisible in exactly the way a wrong category id is.
        """
        ijson = _ijson()
        found: dict[str, dict[str, int]] = {}
        with gzip.open(path, "rb") as fh:
            for uuid, skus in ijson.kvitems(fh, "data"):
                per_finish: dict[str, int] = {}
                for sku in skus:
                    finish = _sku_finish(sku)
                    if finish is None:
                        continue
                    # Documented as a string, delivered as a number. Coerce
                    # rather than trust either.
                    product = int(sku["productId"])
                    seen = per_finish.get(finish)
                    if seen is None:
                        per_finish[finish] = product
                    elif seen != product:
                        per_finish[finish] = _AMBIGUOUS
                if per_finish:
                    found[uuid] = per_finish
        return found

    def _read_prices(
        self, path: Path, products: dict[str, dict[str, int]]
    ) -> Iterator[PriceHistoryRecord]:
        ijson = _ijson()
        stats = dict.fromkeys(("series", "ambiguous", "unmapped", "days"), 0)
        with gzip.open(path, "rb") as fh:
            for uuid, formats in ijson.kvitems(fh, "data"):
                # Read the one provider by name. The documented provider list
                # is already behind the data — `manapool` ships in the file and
                # not in the model — so enumerating what is there would either
                # break on a new one or quietly average a euro price into a
                # dollar column.
                retail = ((formats.get("paper") or {}).get("tcgplayer") or {}).get("retail") or {}
                for finish, series in retail.items():
                    sub_type = SUB_TYPES.get(finish)
                    if sub_type is None:
                        continue
                    product = (products.get(uuid) or {}).get(finish)
                    if product is None:
                        stats["unmapped"] += 1
                        continue
                    if product == _AMBIGUOUS:
                        stats["ambiguous"] += 1
                        continue
                    stats["series"] += 1
                    # Ascending, because the writer collapses a series to the
                    # days it moved and that rule only reads correctly forwards.
                    for day, price in sorted(series.items()):
                        stats["days"] += 1
                        yield PriceHistoryRecord(
                            source_id=str(product),
                            sub_type=sub_type,
                            recorded_on=dt.date.fromisoformat(day),
                            market=float(price) if price is not None else None,
                        )
        self.stats = stats


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


PLUGIN = MTGJSONEnricher()

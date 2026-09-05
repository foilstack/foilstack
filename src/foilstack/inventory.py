"""Inventory rows and the export they feed.

`export_rows` is the single place inventory becomes the flat dictionary an
exporter maps columns against. Exporters are data, so this function is where
any real computation has to live — and keeping it here means every marketplace
gets the same list price for the same card, rather than each TOML file
inventing its own arithmetic.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from itertools import takewhile
from typing import Any

from sqlalchemy import func, select, true

from foilstack import db
from foilstack.plugins.sources.tcgcsv import PRODUCT_LINES

# Multipliers applied to market price when suggesting a list price. Condition
# is the seller's own call: we never infer it from an image, because a
# photograph cannot show edge wear reliably and a wrong condition is a return.
CONDITIONS = ["NM", "LP", "MP", "HP", "DMG"]

# Foil is the seller's call, never the matcher's. Two printings of the same
# card share artwork exactly and differ under the light, so a scan cannot
# settle it — and the price gap between them is routinely a multiple.
FINISHES = ["nonfoil", "foil"]
FINISH_LABEL = {"nonfoil": "Non-foil", "foil": "Foil"}
STATUSES = ["stock", "sold"]
CONDITION_MULTIPLIER = {"NM": 1.00, "LP": 0.85, "MP": 0.70, "HP": 0.55, "DMG": 0.35}

FLOOR = 0.35

# Pricing rules, applied on top of the condition discount. Two separate
# adjustments because they answer different questions: the condition
# multiplier is what the card is worth, the rule is where you want to sit
# against everyone else selling it.
#
# `lowplus` undercuts the actual lowest listing when the catalogue has one.
# It used to approximate with a discount off market, because only a market
# price was stored; `card_prices.low` is the real figure, and the multiplier
# below survives solely as the fallback for a printing nobody is selling.
RULES = [
    {"id": "market", "name": "Match market", "formula": "market x 1.00", "mult": 1.00},
    {"id": "under", "name": "Undercut 5%", "formula": "market x 0.95", "mult": 0.95},
    {"id": "lowplus", "name": "Low + $0.01", "formula": "lowest listing + $0.01", "mult": 0.82},
    {"id": "premium", "name": "Premium NM", "formula": "market x 1.08 (NM only)", "mult": 1.08},
]
RULE_IDS = {r["id"] for r in RULES}
DEFAULT_RULE = "market"


def rule_by_id(rule: str | None) -> dict:
    for spec in RULES:
        if spec["id"] == rule:
            return spec
    return RULES[0]


def list_price(
    market: float | None,
    condition: str,
    rule: str = DEFAULT_RULE,
    low: float | None = None,
) -> float | None:
    """What to ask for one card, in this condition, under this rule.

    `low` is the current lowest listing where the catalogue has one. Only the
    `lowplus` rule uses it, and only that rule ever should: undercutting the
    cheapest copy on the market is a different question from what the card is
    worth, and the other rules answer the second one.
    """
    if market is None:
        return None
    spec = rule_by_id(rule)

    if spec["id"] == "lowplus" and low is not None:
        # The real rule, at last. Condition still discounts it — undercutting
        # a near-mint listing by a cent with a played card is not undercutting,
        # it is overcharging.
        price = low * CONDITION_MULTIPLIER.get(condition, 1.0) + 0.01
        return round(max(price, FLOOR), 2)

    price = market * CONDITION_MULTIPLIER.get(condition, 1.0)
    if spec["id"] == "premium":
        # A premium only makes sense on a card that looks the part.
        if condition == "NM":
            price *= spec["mult"]
    else:
        price *= spec["mult"]
    if spec["id"] == "lowplus":
        price += 0.01
    return round(max(price, FLOOR), 2)


# TCGplayer names printings, not finishes. "Foil" covers Foil, Holofoil and
# Reverse Holofoil; everything else is the plain printing. Matching on the word
# rather than an exhaustive list means a game we have not seen yet still lands
# on the right side of the only distinction a seller can make in bulk.
def finish_of(sub_type: str) -> str:
    """Which side of the foil line a catalogue printing sits on.

    The one place the word is tested, so "does this printing match" and "does
    this card have this finish at all" cannot drift apart.
    """
    return "foil" if "foil" in sub_type.lower() else "nonfoil"


def priced_finishes(names: list[str]) -> set[str]:
    """The finishes the catalogue actually prices this card in.

    Frequently one of the two. Better than a third of the cards here have no
    foil printing and a fifth have nothing but foil printings, so a finish
    control offering both without saying which is real is offering a choice
    the catalogue cannot answer.
    """
    return {finish_of(n) for n in names}


def resolve_finish(default: str, names: list[str]) -> str:
    """The finish to start a card at, given the printings it is priced in.

    The seller answers "foil or not" once for a whole batch, and a batch is
    not all one card: better than a third of the cards here have no foil
    printing and a fifth have nothing else. A default of "non-foil" on a card
    that exists only as foil is not a decision anyone made about that card, so
    the match wins and the default is dropped.

    This used to be flagged rather than resolved — the finish chip in a warning
    colour, the price in red — which put a warning on the one thing the
    catalogue is unambiguous about and left the seller to click away something
    they had no other answer to. Deviating silently is right *because* there is
    no choice being made: a card with printings on one side of the foil line
    only has exactly one honest finish.

    So only where the catalogue is unambiguous. With no printings at all, or
    with both finishes priced, the default stands and the seller decides.
    """
    available = priced_finishes(names)
    if not available or default in available:
        return default
    return available.pop()


def matching_printings(finish: str, names: list[str]) -> list[str]:
    """The printings that count as this finish, in the order given.

    Falls back to the other side rather than returning nothing: a card with a
    single printing serves both answers, and pricing it at zero because the
    seller said "foil" would be worse than pricing it at the only price there
    is.

    That fallback has to be *visible* where it happens. It is the one path
    that prices a card off the wrong side of the only distinction the seller
    was asked to make, and silently it reads as a confirmed price. Callers get
    `finish_unpriced` on the row for exactly that.
    """
    foils = [n for n in names if finish_of(n) == "foil"]
    plain = [n for n in names if finish_of(n) == "nonfoil"]
    if finish == "foil":
        return foils or plain
    return plain or foils


def pick_printing(finish: str, by_sub: dict[str, Any]) -> str | None:
    """Which stored printing to price a card at, when nobody has said.

    A finish is binary and a printing list is not: Base Set Charizard is "1st
    Edition Holofoil" at $10,000, "Unlimited Holofoil" at $2,146 and "Holofoil"
    at $855, and a seller who ticked "foil" has chosen between none of them.

    So it guesses **high**. Overpricing leaves a card unsold and noticed;
    underpricing sells it immediately at a loss discovered from the payout.
    Only one of those is recoverable.

    A guess is still a guess, which is why `InventoryItem.sub_type` exists —
    once the seller names the printing this function is not consulted, and the
    card page marks any row where it still is.
    """
    candidates = matching_printings(finish, sorted(by_sub))
    if not candidates:
        return None
    return max(candidates, key=lambda n: (by_sub[n].market or 0.0, n))


def resolve_printing(
    declared: str | None, finish: str, by_sub: dict[str, Any]
) -> tuple[str | None, bool]:
    """The printing to price at, and whether it was chosen by a person.

    A declared printing that is no longer in the catalogue falls back to the
    guess rather than pricing at nothing — upstream renames sub-types
    occasionally, and a seller should not lose a price to that.
    """
    if declared and declared in by_sub:
        return declared, True
    return pick_printing(finish, by_sub), False


# TCGplayer spells conditions out. Our codes are for a person typing at a pile
# of cards; the upload wants the words its own export writes, and a file that
# says "NM" is a file it rejects.
TCG_CONDITION = {
    "NM": "Near Mint",
    "LP": "Lightly Played",
    "MP": "Moderately Played",
    "HP": "Heavily Played",
    "DMG": "Damaged",
}


def tcg_condition(condition: str, sub_type: str | None) -> str:
    """The `Condition` column of a TCGplayer upload, which is not just condition.

    It is condition *and printing*: "Near Mint" and "Near Mint Foil" are two
    values of one column, because on TCGplayer they are two different things to
    sell. "Normal" is the absent case rather than a word that appears — a
    non-foil card is "Near Mint", never "Near Mint Normal".

    Whatever the catalogue calls the printing is what goes on the end, which is
    right for every game because both strings come from the same upstream:
    "Foil" for Magic, "Holofoil" for Dragon Ball, "1st Edition Holofoil" for
    Pokemon. Inventing a shorter list here would flatten exactly the
    distinction `sub_type` exists to keep.
    """
    name = TCG_CONDITION.get(condition, condition)
    if not sub_type or sub_type == "Normal":
        return name
    return f"{name} {sub_type}"


def sku(item_id: int) -> str:
    """A stable internal SKU.

    Deliberately not the upstream catalogue id: two rows of the same card in
    different conditions are different things to sell, and a marketplace
    custom-label that collides between them makes the sold-out one impossible
    to reconcile.
    """
    return f"FS-{10000 + item_id}"


# How many card ids one price fetch may name in a single statement.
#
# Postgres carries at most 65535 bind parameters in one message and `IN (...)`
# renders one per element, so an account holding more distinct cards than that
# did not get a slow page — it got a 500, and it got one on every screen in the
# application, because the topbar priced inventory through this same function.
# A seller buying collections passes 65k distinct cards; that is a cliff rather
# than a curve, and nothing on the way to it gets slower to warn you.
#
# Chunked well below the limit rather than exactly at it: the cap is on the
# whole message, and this list is not always the only thing in one.
PRICE_CHUNK = 10_000


def _prices_for(session, card_ids: set[int]) -> dict[int, dict[str, Any]]:
    """Every stored printing price for these cards, keyed by card then sub-type.

    Fetched in chunks rather than per row: an inventory of a few hundred cards
    would otherwise issue a few hundred round trips to render a table, and one
    of a hundred thousand exceeds what a single statement can bind.
    """
    if not card_ids:
        return {}
    out: dict[int, dict[str, Any]] = {}
    ids = list(card_ids)
    for start in range(0, len(ids), PRICE_CHUNK):
        rows = session.scalars(
            select(db.CardPrice).where(db.CardPrice.card_id.in_(ids[start : start + PRICE_CHUNK]))
        ).all()
        for row in rows:
            out.setdefault(row.card_id, {})[row.sub_type] = row
    return out


def priced_printing(holder: Any = None) -> Any:
    """The `card_prices` row that prices one inventory row, as SQL.

    The same decision `resolve_printing` makes in Python — the printing the
    seller named if the catalogue still has it, otherwise `pick_printing`'s
    guess — written once as a correlated lateral, so a query can ask what an
    inventory is worth without materialising every row of it in Python first.

    That there are now two expressions of one rule is the cost of this, and it
    is paid deliberately: the alternative was a topbar that summed
    `cards.market` and quoted a different total from the table underneath it,
    which is the bug the comment in `chrome._chrome` records. `pick_printing`
    stays the definition for callers that already hold the price map, and
    `tests/test_inventory_scale.py` drives the two against the same rows,
    printing by printing, so they cannot drift apart in silence.

    The window functions ride along for free: they are evaluated over every
    printing of the card before `LIMIT 1` takes one, which is how a single
    lateral answers "and how many printings were there" and "which sides of the
    foil line are priced at all" without a second visit to the table.
    """
    cp = db.CardPrice
    # Whatever is being priced, which is not always the `inventory` table.
    # `position` prices one row per *distinct* card, finish and declared
    # printing rather than one per card owned, and a lateral hard-wired to
    # `db.InventoryItem` does not correlate to that — it becomes a cartesian
    # product that quietly answers for the whole table. SQLAlchemy warns about
    # it; the numbers it returns look plausible, which is worse.
    item = db.InventoryItem if holder is None else holder
    is_foil_printing = func.lower(cp.sub_type).like("%foil%")
    return (
        select(
            cp.sub_type.label("sub_type"),
            cp.market.label("market"),
            cp.low.label("low"),
            func.count().over().label("n_printings"),
            func.bool_or(is_foil_printing).over().label("has_foil"),
            func.bool_or(~is_foil_printing).over().label("has_plain"),
        )
        .where(cp.card_id == item.card_id)
        .order_by(
            # A printing the seller named wins outright, and only if the
            # catalogue still carries it — upstream renames sub-types, and a
            # declared printing that has gone should fall back to the guess
            # rather than price the card at nothing. `IS NOT DISTINCT FROM`
            # rather than `=` because `item.sub_type` is usually NULL, and a
            # NULL sort key under DESC sorts first, which would hand every
            # undeclared row to whichever printing happened to be there.
            cp.sub_type.is_not_distinct_from(item.sub_type).desc(),
            # Then the seller's side of the foil line, falling back to the
            # other side rather than to nothing: a card with a single printing
            # serves both answers, and pricing it at zero because the seller
            # said "foil" is worse than pricing it off the only price there is.
            (is_foil_printing == (item.finish == "foil")).desc(),
            # And within that side, dearest. It guesses high on purpose:
            # overpricing leaves a card unsold and noticed, underpricing sells
            # it at a loss discovered from the payout. Only one is recoverable.
            func.coalesce(cp.market, 0.0).desc(),
            cp.sub_type.desc(),
        )
        .limit(1)
        .lateral("priced")
    )


def position(session, user_id: int) -> dict[str, Any]:
    """What this account holds, in money, without materialising it.

    The topbar's two figures and the count beside them, as one query. It used
    to be `items()` — the whole inventory built into Python dictionaries so
    that two numbers could be summed off it — and `_chrome` runs on *every*
    screen, so the review queue, the import page and a single card page all
    paid for the whole of inventory before drawing anything. At a hundred and
    fifty thousand rows that was around four seconds and eight hundred
    megabytes per request, on pages that never mention inventory.

    Priced through `priced_printing`, so this agrees with the table on
    `/inventory` rather than with `cards.market`, which is the plain
    printing's price and quoted every foil at the wrong number.

    Grouped before it is priced. Rows sharing a card, a finish and a declared
    printing resolve to the same catalogue row by construction, so pricing
    them once and multiplying by the count asks the catalogue a question per
    *distinct card* rather than per card owned — which is the difference
    between one lookup and forty for a seller with forty copies of a staple.
    """
    item = db.InventoryItem
    held = (
        select(
            item.card_id.label("card_id"),
            item.finish.label("finish"),
            item.sub_type.label("sub_type"),
            func.count().label("n"),
        )
        .where(item.user_id == user_id, item.status == "stock")
        .group_by(item.card_id, item.finish, item.sub_type)
        .subquery()
    )
    priced = priced_printing(held.c)
    declared = priced.c.sub_type.is_not_distinct_from(held.c.sub_type)
    row = session.execute(
        select(
            func.coalesce(func.sum(held.c.n), 0),
            func.coalesce(func.sum(func.coalesce(priced.c.market, db.Card.market) * held.c.n), 0.0),
            func.coalesce(
                func.sum(held.c.n).filter(
                    priced.c.sub_type.is_not(None),
                    priced.c.n_printings > 1,
                    ~declared,
                ),
                0,
            ),
        )
        .select_from(held)
        .join(db.Card, db.Card.id == held.c.card_id)
        .outerjoin(priced, true())
    ).one()
    return {"count": int(row[0]), "market": float(row[1]), "needs_printing": int(row[2])}


def index(
    session, user_id: int, rule: str = DEFAULT_RULE, status: str | None = None
) -> list[dict[str, Any]]:
    """One thin dict per copy: enough to count, filter, sort, total and group.

    `items()` answers the same question in about twice as many keys, and every
    one of the extra ones costs something — the printing list is a second query
    and a list of dicts per row, the TCGplayer spellings are strings nothing on
    the inventory screen reads. That is the right shape for a card page, an
    edit panel or a marketplace export, all of which are bounded by a card, a
    row or a selection. It is the wrong shape for the whole of a seller's
    inventory, which is what the facet counts and the totals bar genuinely
    need to see.

    So this is the whole-set read and `items()` is the detailed one. Measured
    over 150,000 rows: `items()` takes about 3.8 seconds and peaks at 840 MB,
    this takes about 1.1 seconds and 350 MB, and the inventory screen called
    the first of them three times per render.

    The keys it does carry are named identically, so `fold` and `totals` and
    the facets work over either. What it deliberately omits is `printings` —
    absent rather than empty, because an empty list is a claim that the
    catalogue prices this card in nothing at all, and the card page draws its
    printing chips off exactly that.
    """
    item, card = db.InventoryItem, db.Card
    priced = priced_printing()
    query = (
        select(
            item.id,
            item.card_id,
            item.condition,
            item.finish,
            item.sub_type,
            item.status,
            item.cost,
            item.sold_price,
            item.listed,
            item.listed_channels,
            item.scan_id,
            card.name,
            card.game,
            card.set_name,
            card.number,
            card.variant,
            card.image_url,
            card.market,
            priced.c.sub_type,
            priced.c.market,
            priced.c.low,
            priced.c.n_printings,
            priced.c.has_foil,
            priced.c.has_plain,
        )
        .select_from(item)
        .join(card, card.id == item.card_id)
        .outerjoin(priced, true())
        .where(item.user_id == user_id)
    )
    if status is not None:
        query = query.where(item.status == status)

    out: list[dict[str, Any]] = []
    for row in session.execute(query.order_by(item.id.desc())):
        (
            item_id,
            card_id,
            condition,
            finish,
            declared_sub,
            item_status,
            cost,
            sold_price,
            listed,
            listed_channels,
            scan_id,
            name,
            game,
            set_name,
            number,
            variant,
            image_url,
            card_market,
            sub,
            sub_market,
            low,
            n_printings,
            has_foil,
            has_plain,
        ) = row
        # `resolve_printing` returns "did a person choose this", and the pick
        # is ordered so that a declared printing the catalogue still carries
        # wins outright. So the two agree exactly here, and one that the
        # catalogue has dropped reads as the guess it fell back to.
        declared = bool(declared_sub) and sub == declared_sub
        market = sub_market if sub_market is not None else card_market
        available = {side for side, on in (("foil", has_foil), ("nonfoil", has_plain)) if on}
        out.append(
            {
                "id": item_id,
                "card_id": card_id,
                "name": name,
                "game": game,
                "set_name": set_name,
                "number": number,
                "variant": variant,
                "image_url": image_url,
                "condition": condition,
                "finish": finish,
                "finish_label": FINISH_LABEL.get(finish, finish),
                "is_foil": finish == "foil",
                "quantity": 1,
                "status": item_status,
                "sold": item_status == "sold",
                "sold_price": sold_price,
                "cost": cost,
                "market": market,
                "low": low,
                "sub_type": sub,
                "printing_declared": declared,
                "printing_guessed": bool(sub) and not declared and (n_printings or 0) > 1,
                "finishes_priced": sorted(available),
                "finish_unpriced": bool(n_printings) and finish not in available,
                "list_price": list_price(market, condition, rule, low),
                "scan_id": scan_id,
                "listed": bool(listed),
                "listed_channels": listed_channels or "",
                "listed_label": listed_channels or "\u2014",
            }
        )
    return out


def items(
    session, user_id: int, rule: str = DEFAULT_RULE, status: str | None = None
) -> list[dict[str, Any]]:
    """Every row this account owns, optionally only those in one state.

    `user_id` is required rather than defaulted. A scoping argument with a
    default is one a caller can forget, and forgetting it here shows one
    seller another seller's cards.

    `status` defaults to everything so the inventory screen can show sold rows
    alongside stock. Callers that must not see sold cards — a listing run, most
    obviously, since listing something you have already sold is the one
    inventory error that reaches a customer — pass "stock" explicitly.
    """
    query = (
        select(db.InventoryItem, db.Card)
        .join(db.Card, db.Card.id == db.InventoryItem.card_id)
        .where(db.InventoryItem.user_id == user_id)
    )
    if status is not None:
        query = query.where(db.InventoryItem.status == status)
    rows = session.execute(query.order_by(db.InventoryItem.id.desc())).all()
    prices = _prices_for(session, {card.id for _, card in rows})

    out: list[dict[str, Any]] = []
    for item, card in rows:
        # The price of the printing the seller says they hold, not an average
        # over printings. A foil priced at its non-foil market value is wrong
        # by a multiple, and always wrong in the direction that loses money.
        by_sub = prices.get(card.id, {})
        sub, declared = resolve_printing(item.sub_type, item.finish, by_sub)
        available = priced_finishes(list(by_sub))
        row = by_sub.get(sub) if sub else None
        market = row.market if row and row.market is not None else card.market
        low = row.low if row else None
        price = list_price(market, item.condition, rule, low)
        cost = item.cost
        margin = None
        if price is not None and cost is not None:
            margin = round(price - cost, 2)
        market_for_margin = market
        out.append(
            {
                "id": item.id,
                "card_id": card.id,
                "sku": sku(item.id),
                "source": card.source,
                "source_ref": card.source_id.split(":", 1)[-1],
                "name": card.name,
                "game": card.game,
                "set_name": card.set_name,
                "number": card.number,
                "variant": card.variant,
                "condition": item.condition,
                # The same two facts as `condition` and `sub_type`, spelled the
                # way a TCGplayer upload spells them. Here rather than in the
                # exporter TOML because exporters are data: a marketplace whose
                # vocabulary differs from ours needs a translation, and this is
                # the only place one can live.
                "tcg_condition": tcg_condition(item.condition, sub),
                "tcg_product_line": PRODUCT_LINES.get(card.game, card.game),
                # The raw spelling, and `name` where the catalogue predates the
                # column. Falling back is a real answer for most cards and a
                # wrong one for any card with punctuation in it, which is why
                # the matcher reports what it could not find rather than
                # quietly returning a shorter file.
                "tcg_name": card.source_name or card.name,
                "finish": item.finish,
                "finish_label": FINISH_LABEL.get(item.finish, item.finish),
                "is_foil": item.finish == "foil",
                # A copy is one card. Kept on the dict so an exporter mapping
                # `quantity` still works when given a single copy rather than
                # a grouped stock line.
                "quantity": 1,
                "notes": item.notes or "",
                "status": item.status,
                "sold": item.status == "sold",
                "sold_price": item.sold_price,
                "sold_at": item.sold_at,
                "created_at": item.created_at,
                "cost": cost,
                "market": market,
                "low": low,
                "sub_type": sub,
                "printing_declared": declared,
                # A guess only matters when there was a choice. One printing is
                # not an ambiguity, and flagging it would cry wolf on almost
                # every card in the catalogue.
                "printing_guessed": bool(sub) and not declared and len(by_sub) > 1,
                # Which finishes the catalogue prices this card in, so the
                # screens can mark the one it does not rather than offering
                # both as if they were equally real.
                "finishes_priced": sorted(available),
                # The seller's finish has no printing behind it, so the price
                # beside it came off the other side of the foil line. Not
                # conditional on `printing_guessed`: that one stays quiet on a
                # single-printing card, which is precisely the shape this is —
                # one "Normal" row under a chip reading "Foil", with nothing
                # on screen to say the number is the non-foil one.
                "finish_unpriced": bool(by_sub) and item.finish not in available,
                # Every printing we hold a price for, so the card page can offer
                # them and say what it did not pick.
                "printings": [
                    {
                        "sub_type": name,
                        "market": by_sub[name].market,
                        "low": by_sub[name].low,
                        "on": name == sub,
                    }
                    for name in sorted(by_sub)
                ],
                "list_price": price,
                "margin": margin,
                "margin_pct": (
                    round(100 * (market_for_margin - cost) / market_for_margin)
                    if cost is not None and market_for_margin
                    else None
                ),
                "scan_id": item.scan_id,
                "image_url": card.image_url,
                "listed": bool(item.listed),
                "listed_channels": item.listed_channels or "",
                "listed_label": item.listed_channels or "—",
                "ebay_title": " ".join(p for p in [card.name, card.set_name, card.number] if p)[
                    :80
                ],
            }
        )
    return out


def merge_channels(labels: Iterable[str]) -> list[str]:
    """The distinct channels named across a set of copies.

    `listed_channels` holds the comma-joined label of one marking run, not a
    single channel, so two copies reading `tcgplayer` and `ebay, tcgplayer`
    name three strings and two channels. Splitting rather than uniquing the
    labels is the difference between "listed on eBay and TCGplayer" and a
    third marketplace called "ebay, tcgplayer".
    """
    seen = {c.strip() for label in labels if label for c in label.split(",") if c.strip()}
    return sorted(seen)


def _summarise(values: list[str], labels: dict[str, str] | None = None) -> str:
    """`NM` when they all agree, `2 NM, 1 LP` when they do not."""
    counts: dict[str, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    if len(counts) == 1:
        only = next(iter(counts))
        return (labels or {}).get(only, only)
    return ", ".join(
        f"{n} {(labels or {}).get(v, v)}" for v, n in sorted(counts.items(), key=lambda kv: -kv[1])
    )


def groups(
    session, user_id: int, rule: str = DEFAULT_RULE, status: str | None = None
) -> list[dict[str, Any]]:
    """Inventory as one line per card, holding every copy of it.

    Grouped by the card alone — not by card and condition — because this screen
    answers "what do I own and how many". A seller with three Lightning Bolts
    has three Lightning Bolts, and splitting them across rows because one is
    played buries the number they came here for.

    Condition and finish are not lost, they are summarised (`2 NM, 1 LP`) and
    itemised on the card page. Where the distinction genuinely changes the
    answer — a marketplace upload, where condition sets the price — the export
    regroups by it. See `export_rows`.

    Quantity is derived and never stored. One database row is one physical
    card, which is what a scan is evidence of and what carries its own cost,
    notes and sale.
    """
    return fold(items(session, user_id, rule, status))


def fold(copies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The same grouping, over copies already in hand.

    Split out of `groups` so the inventory screen can read its copies once,
    through `index`, and fold them without a second trip — it used to call
    `groups` twice and `items` a third time to draw one page, which is three
    reads of the whole inventory for one screen.

    Works over either shape of copy. `printings` is the one key `index` does
    not carry, and it is read with `.get` for that reason: a line folded from
    thin copies reports `None` there, meaning "not fetched", which is a
    different claim from `[]` — the empty list says the catalogue prices this
    card in nothing, and the card page draws its printing chips off it.
    """
    lines: dict[int, dict[str, Any]] = {}
    for row in copies:
        line = lines.get(row["card_id"])
        if line is None:
            line = lines[row["card_id"]] = {
                **{
                    k: row[k]
                    for k in (
                        "card_id",
                        "name",
                        "game",
                        "set_name",
                        "number",
                        "variant",
                        "market",
                        "image_url",
                    )
                },
                "copies": [],
            }
        line["copies"].append(row)

    out = []
    for line in lines.values():
        copies = line["copies"]
        costs = [c["cost"] for c in copies if c["cost"] is not None]
        in_stock = [c for c in copies if not c["sold"]]

        line["quantity"] = len(copies)
        line["stock_quantity"] = len(in_stock)
        line["sold_quantity"] = len(copies) - len(in_stock)
        line["ids"] = [c["id"] for c in copies]
        line["conditions"] = _summarise([c["condition"] for c in copies])
        line["finishes"] = _summarise([c["finish"] for c in copies], FINISH_LABEL)
        line["any_foil"] = any(c["is_foil"] for c in copies)
        line["mixed"] = len({(c["condition"], c["finish"]) for c in copies}) > 1
        # Averaged, so it sits beside market and list price on the same footing.
        # `costed` says how much of the line that average actually covers.
        line["cost"] = round(sum(costs) / len(costs), 2) if costs else None
        line["costed"] = len(costs)
        line["list_price"] = copies[0]["list_price"]
        line["margin_pct"] = (
            round(100 * (line["market"] - line["cost"]) / line["market"])
            if line["cost"] is not None and line["market"]
            else None
        )
        line["printings"] = copies[0].get("printings")
        line["guessed"] = sum(1 for c in copies if c["printing_guessed"])
        line["finish_unpriced"] = sum(1 for c in copies if c["finish_unpriced"])
        line["printing_label"] = _summarise([c["sub_type"] or c["finish_label"] for c in copies])
        channels = merge_channels(c["listed_channels"] for c in copies)
        line["listed_label"] = ", ".join(channels) if channels else "—"
        line["listed"] = any(c["listed"] for c in copies)
        line["sold"] = not in_stock
        line["scan_count"] = sum(1 for c in copies if c["scan_id"])
        out.append(line)

    out.sort(key=lambda line: line["name"].lower())
    return out


# ---------------------------------------------------------------------------
# The inventory screen's filters.
#
# All of this runs over the grouped lines in memory rather than in SQL, because
# `groups` is already a Python fold over `items` and the values three of these
# facets read — condition, printing, whether anything is listed — only exist
# once the copies are folded together. Pushing the filter down to the query
# would mean writing that fold twice, in two languages, and having the screen
# disagree with the card page about the same card.

# How to read one facet's values off a single copy — one row of `items`, one
# physical card. Defined at the copy rather than at the grouped line because
# that is the level every one of these values actually exists at, and because
# the two callers need different levels: the chips are counted over lines, and
# the vocabulary a pick is validated against is read straight off the copies
# the account owns. One definition, unioned upwards, is what keeps those two
# from drifting into disagreeing about what a filter means.
FACET_VALUES: dict[str, Any] = {
    "game": lambda copy: {copy["game"]},
    "set": lambda copy: {copy["set_name"]} if copy["set_name"] else set(),
    "condition": lambda copy: {copy["condition"]},
    "printing": lambda copy: {copy["finish"]},
    "listed": lambda copy: {"listed" if copy["listed"] else "unlisted"},
}

# `order` is the vocabulary's own order where it has one — NM before DMG reads
# as a scale, alphabetical does not. Game and set have no such order and are
# sorted by name; they are also the two whose values come from the catalogue
# rather than from this file, so they can only ever be listed from the rows.
FACETS: list[dict[str, Any]] = [
    {"key": "game", "label": "Game"},
    {"key": "set", "label": "Set"},
    {"key": "condition", "label": "Condition", "order": CONDITIONS},
    {"key": "printing", "label": "Printing", "order": FINISHES, "labels": FINISH_LABEL},
    {
        "key": "listed",
        "label": "Listed",
        "order": ["unlisted", "listed"],
        "labels": {"unlisted": "Not listed", "listed": "Listed"},
    },
]
FACET_KEYS = [spec["key"] for spec in FACETS]

# How many values one facet may paint. Only game and set can exceed it — the
# other three have vocabularies this file declares — and only set realistically
# does: a seller who has been buying collections owns cards from hundreds of
# sets, and a chip for every one of them is several hundred pixels of chrome
# above the table it is supposed to be filtering. The ones kept are those held
# by the most lines, because a set you own four cards from is one to reach the
# search box for, not one to browse to.
FACET_LIMIT = 10


def line_values(line: dict[str, Any], key: str) -> set[str]:
    """One facet's values across every copy on a grouped line.

    A union, so a line holding an NM copy and an LP copy sits under both chips
    and narrowing to LP keeps the whole line. Dropping the copies that do not
    match instead would make the quantity, the averaged cost and the totals all
    disagree with the card page for the same card.
    """
    read = FACET_VALUES[key]
    return {value for copy in line["copies"] for value in read(copy)}


def present_values(copies: list[dict[str, Any]], key: str) -> set[str]:
    """Every value of one facet that some copy this account owns actually has.

    Read off the seller's whole inventory, deliberately, and not off whatever
    the screen is currently showing. Narrowing a pick to the visible rows looks
    like the same guard and is a different and much worse one: filter to LP,
    then search for a card that has no LP copy, and the LP pick is no longer
    "present" — so it gets dropped, its chip disappears, and the screen answers
    with the near-mint copies. A filter that silently stops applying is worse
    than one that matches nothing, because nothing on screen says it happened.

    Against the whole inventory it only ever drops a value the account has no
    card under at all, which is a stale bookmark or a hand-edited URL. Honouring
    one of those empties the screen and paints a chip for a set the seller does
    not own, which reads as lost inventory rather than as an empty filter.
    """
    read = FACET_VALUES[key]
    return {value for copy in copies for value in read(copy)}


def filter_groups(rows: list[dict[str, Any]], picks: dict[str, set[str]]) -> list[dict[str, Any]]:
    """The lines matching every facet that has something picked.

    Values within one facet are ORed and the facets are ANDed: Magic *and*
    Pokemon means either of them, Magic *and* NM means both at once. That is
    the only reading under which adding a chip to a facet you have already
    picked from widens the result, which is what a seller ticking a second
    game is asking for.
    """
    for key, chosen in picks.items():
        if chosen:
            rows = [r for r in rows if line_values(r, key) & chosen]
    return rows


def facet_options(rows: list[dict[str, Any]], picks: dict[str, set[str]]) -> list[dict[str, Any]]:
    """Each facet's values with how many lines hold them, ready to paint.

    A facet is counted against the lines the *other* facets allow, not against
    the final result. Counted against the result, every unpicked chip in a
    facet you have already picked from reads zero — which says clicking it
    would empty the screen when it would in fact add rows.

    A picked value is always painted, at zero if that is what it counts. It is
    the control for taking the filter off again, and a filter you cannot see is
    one you cannot undo.

    A facet with fewer than two values is dropped, unless something in it is
    picked. One chip reading "Magic 340" on a Magic-only inventory is a control
    that cannot do anything, and a row of them pushes the ones that can off the
    screen. A facet with more than `FACET_LIMIT` is capped and says how many it
    is not showing — a filter that quietly omits values is one a seller
    concludes their cards are missing from.
    """
    out: list[dict[str, Any]] = []
    for spec in FACETS:
        key = spec["key"]
        chosen = picks.get(key, set())
        scope = filter_groups(rows, {k: v for k, v in picks.items() if k != key})
        counts: dict[str, int] = dict.fromkeys(chosen, 0)
        for line in scope:
            for value in line_values(line, key):
                counts[value] = counts.get(value, 0) + 1

        order = spec.get("order")
        values = sorted(counts, key=order.index if order else lambda v: str(v).lower())
        if len(values) < 2 and not chosen:
            continue
        hidden = 0
        if not order and len(values) > FACET_LIMIT:
            # Chosen by count, painted in the facet's own order. Chosen *and*
            # painted by count would reorder the row under the cursor every
            # time a chip elsewhere changed the counts, so a second click lands
            # on whatever slid into the place the first one was.
            keep = set(sorted(values, key=lambda v: (-counts[v], str(v).lower()))[:FACET_LIMIT])
            keep |= chosen
            hidden = len(values) - len(keep)
            values = [v for v in values if v in keep]
        labels = spec.get("labels") or {}
        out.append(
            {
                "key": key,
                "label": spec["label"],
                "hidden": hidden,
                "options": [
                    {
                        "value": value,
                        "label": labels.get(value, value),
                        "count": counts[value],
                        "on": value in chosen,
                    }
                    for value in values
                ],
            }
        )
    return out


# What each sortable column sorts on. `set` sorts by game, then set, then
# collector number rather than by the displayed string, so a set reads in the
# order it was printed instead of 1, 10, 100, 11.
SORT_VALUES: dict[str, Any] = {
    "name": lambda line: line["name"].lower(),
    "set": lambda line: (
        line["game"].lower(),
        (line["set_name"] or "").lower(),
        _number_key(line["number"]),
    ),
    "quantity": lambda line: line["quantity"],
    "cost": lambda line: line["cost"],
    "market": lambda line: line["market"],
    "margin": lambda line: line["margin_pct"],
    "listed": lambda line: line["listed_label"].lower(),
}
DEFAULT_SORT = "name"
DEFAULT_DIR = "asc"


def _number_key(number: str | None) -> tuple[int, int, str]:
    """A collector number ordered as a number where it is one.

    They are strings in the catalogue and routinely are not numbers at all —
    "H12", "SV49", "233a" — so this is a sort key and not a parse. "233a" sorts
    with 233 because the digits lead; "H12" does not, and goes in a second
    block after the numbered run rather than among it. That is the order a set
    is actually printed in, and it keeps "H12" from landing above "12", which
    is where a plain digit-prefix key puts it.
    """
    text = (number or "").strip()
    digits = "".join(takewhile(str.isdigit, text))
    return (0, int(digits), text.lower()) if digits else (1, 0, text.lower())


def sort_groups(
    rows: list[dict[str, Any]], key: str = DEFAULT_SORT, direction: str = DEFAULT_DIR
) -> list[dict[str, Any]]:
    """Ordered by one column, with the lines that have no value for it last.

    Missing stays at the bottom in both directions rather than flipping to the
    top when the sort reverses. A card with no cost recorded is not the
    cheapest card, and forty of them above the expensive ones is the fastest
    way to make a working sort look broken.
    """
    value = SORT_VALUES.get(key, SORT_VALUES[DEFAULT_SORT])
    present = [r for r in rows if value(r) is not None]
    missing = [r for r in rows if value(r) is None]
    present.sort(key=value, reverse=direction == "desc")
    return present + missing


# The status views the screen offers, and what each one asks of a copy.
# `printing` is not a status at all — it is the pass to make after an import,
# every line still priced on a guess between printings — so it is answered
# after the lines are folded rather than by filtering copies.
SHOW_VALUES = ("stock", "sold", "all", "printing")
DEFAULT_SHOW = "stock"


@dataclass(frozen=True)
class Narrowed:
    """One narrowing of an inventory, and the intermediates a screen needs.

    `rows` is what to draw. `total_lines` is what there was before the search
    box and the facets, so a screen can say "138 of 152" and mean it. `facets`
    is counted against the *other* facets, which is only computable part-way
    through. `picks` is what survived validation, which is what the links have
    to be rebuilt from.
    """

    rows: list[dict[str, Any]]
    total_lines: int
    facets: list[dict[str, Any]]
    picks: dict[str, set[str]]


def narrow(
    copies: list[dict[str, Any]],
    *,
    show: str = DEFAULT_SHOW,
    q: str = "",
    wire: dict[str, list[str] | None] | None = None,
    sort: str = DEFAULT_SORT,
    dir: str = DEFAULT_DIR,
) -> Narrowed:
    """The lines a filter selects, in the order the screen puts them.

    Every step the inventory screen used to run inline, in one place, because
    it is no longer the only caller. A listing run started by "select all
    matching these filters" has to resolve to *exactly* the lines the seller
    was looking at — and the way to guarantee that is not to write the
    narrowing twice and test that the two agree, it is to have one narrowing.

    Everything here arrives from a querystring, so nothing here trusts its
    arguments: an unknown sort falls back, a direction that is not "desc" is
    ascending, and picks are intersected with what the account actually owns.
    """
    show = show if show in SHOW_VALUES else DEFAULT_SHOW
    sort = sort if sort in SORT_VALUES else DEFAULT_SORT
    dir = "desc" if dir == "desc" else "asc"

    wanted = show if show in ("stock", "sold") else None
    rows = fold([r for r in copies if wanted is None or r["status"] == wanted])
    if show == "printing":
        rows = [r for r in fold([r for r in copies if not r["sold"]]) if r["guessed"]]
    total_lines = len(rows)

    needle = q.strip().lower()
    if needle:
        rows = [
            r
            for r in rows
            if needle
            in " ".join(
                str(x)
                for x in (
                    r["name"],
                    r["game"],
                    r["set_name"] or "",
                    r["conditions"],
                    r["finishes"],
                    r["number"] or "",
                )
            ).lower()
        ]

    # Narrowed to what the account can actually offer before anything is
    # filtered. A value from a stale bookmark or a hand-edited URL is dropped
    # rather than honoured: honoured it empties the screen, which reads as lost
    # inventory rather than as a filter that matched nothing.
    #
    # Against the whole inventory, deliberately, and not against the rows the
    # search has already narrowed — see `present_values`, which is where that
    # distinction cost a live filter once.
    given = wire or {}
    picks = {key: set(given.get(key) or []) & present_values(copies, key) for key in FACET_KEYS}

    # Built before the rows are narrowed, so each chip counts against what the
    # *other* facets allow rather than against the final result.
    facets = facet_options(rows, picks)
    return Narrowed(
        rows=sort_groups(filter_groups(rows, picks), sort, dir),
        total_lines=total_lines,
        facets=facets,
        picks=picks,
    )


def totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Position from the rows still in stock, plus what the sold ones realised.

    Sold rows are excluded from every "what do I have" number and counted only
    in the realised ones. Leaving them in inflates inventory value with cards
    that are gone, which is the exact number a seller would act on.
    """
    stock = [r for r in rows if not r["sold"]]
    sold = [r for r in rows if r["sold"]]

    market = sum(r["market"] or 0 for r in stock)
    listed = sum(r["list_price"] or 0 for r in stock)
    cost = sum(r["cost"] or 0 for r in stock)
    realised = sum(r["sold_price"] or 0 for r in sold)
    sold_cost = sum(r["cost"] or 0 for r in sold)
    return {
        # One row is one card, so these are counts rather than sums of a
        # quantity column. There is no longer a quantity column.
        "count": len(stock),
        # Distinct cards held, not distinct sellable lines: this sits beside
        # "count" on the analytics screen, where the question is how much
        # variety is on the shelf.
        "distinct": len({r["card_id"] for r in stock}),
        "market": round(market, 2),
        "listed": round(listed, 2),
        "cost": round(cost, 2),
        "margin": round(listed - cost, 2) if cost else None,
        "sold_count": len(sold),
        "sold_rows": len(sold),
        "realised": round(realised, 2),
        "realised_cost": round(sold_cost, 2),
        # Only meaningful where a cost was actually recorded; without one this
        # is just the sale price wearing a more confident name.
        "realised_profit": round(realised - sold_cost, 2) if sold_cost else None,
    }


def export_rows(
    session, user_id: int, rule: str = DEFAULT_RULE, ids: set[int] | None = None
) -> list[dict[str, Any]]:
    """Rows for a marketplace upload — stock only, one line per sellable thing.

    Regrouped by card *and* condition *and* finish, which is a finer split than
    the inventory screen uses. Not an inconsistency: a marketplace listing is
    priced by condition, so an NM copy and an LP copy are two listings even
    though they are one line in "what do I own". Collapsing them here would
    sell a played card at a near-mint price.

    Still grouped rather than one row per card, because a marketplace reads one
    row as one listing: two copies emitted as two rows of quantity 1 becomes
    two separate listings of the same card, which nobody meant and which is
    tedious to unpick once live.

    A sold card is never here — an export containing one is a listing for
    something you cannot ship.
    """
    lines: dict[tuple, dict[str, Any]] = {}
    for row in items(session, user_id, rule, status="stock"):
        if ids is not None and row["id"] not in ids:
            continue
        key = (row["card_id"], row["condition"], row["finish"])
        line = lines.get(key)
        if line is None:
            line = lines[key] = {
                **row,
                "quantity": 0,
                "ids": [],
                "listed_ids": [],
                # Per copy, because marking is per copy: one line can be
                # listed on TCGplayer in one copy and nowhere in the other,
                # and a caller asking "what is left to mark" needs the ids,
                # not the summary.
                "copy_channels": {},
            }
        line["quantity"] += 1
        line["ids"].append(row["id"])
        line["copy_channels"][row["id"]] = merge_channels([row["listed_channels"]])
        if row["listed"]:
            line["listed_ids"].append(row["id"])

    for line in lines.values():
        # Aggregated over the copies rather than inherited from whichever one
        # was seen first. Two copies of a card are one row here and two rows in
        # the database, so a line can be half listed — and the first copy's
        # answer is then a coin toss presented as a fact.
        line["channels"] = sorted({c for cs in line["copy_channels"].values() for c in cs})
        line["listed_channels"] = ", ".join(line["channels"])
        line["listed_count"] = len(line["listed_ids"])
        line["listed"] = line["listed_count"] == line["quantity"]
        line["listed_partly"] = 0 < line["listed_count"] < line["quantity"]

    return sorted(lines.values(), key=lambda r: (r["name"].lower(), r["condition"], r["finish"]))

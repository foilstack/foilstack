"""Inventory rows and the export they feed.

`export_rows` is the single place inventory becomes the flat dictionary an
exporter maps columns against. Exporters are data, so this function is where
any real computation has to live — and keeping it here means every marketplace
gets the same list price for the same card, rather than each TOML file
inventing its own arithmetic.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

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


def _prices_for(session, card_ids: set[int]) -> dict[int, dict[str, Any]]:
    """Every stored printing price for these cards, keyed by card then sub-type.

    Fetched in one query rather than per row: an inventory of a few hundred
    cards would otherwise issue a few hundred round trips to render a table.
    """
    if not card_ids:
        return {}
    out: dict[int, dict[str, Any]] = {}
    rows = session.scalars(select(db.CardPrice).where(db.CardPrice.card_id.in_(card_ids))).all()
    for row in rows:
        out.setdefault(row.card_id, {})[row.sub_type] = row
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
    lines: dict[int, dict[str, Any]] = {}
    for row in items(session, user_id, rule, status):
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
        line["printings"] = copies[0]["printings"]
        line["guessed"] = sum(1 for c in copies if c["printing_guessed"])
        line["finish_unpriced"] = sum(1 for c in copies if c["finish_unpriced"])
        line["printing_label"] = _summarise([c["sub_type"] or c["finish_label"] for c in copies])
        channels = sorted({c["listed_channels"] for c in copies if c["listed_channels"]})
        line["listed_label"] = ", ".join(channels) if channels else "—"
        line["listed"] = any(c["listed"] for c in copies)
        line["sold"] = not in_stock
        line["scan_count"] = sum(1 for c in copies if c["scan_id"])
        out.append(line)

    out.sort(key=lambda line: line["name"].lower())
    return out


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
            line = lines[key] = {**row, "quantity": 0, "ids": []}
        line["quantity"] += 1
        line["ids"].append(row["id"])
    return sorted(lines.values(), key=lambda r: (r["name"].lower(), r["condition"], r["finish"]))

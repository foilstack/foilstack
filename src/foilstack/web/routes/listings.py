"""Getting cards to market, and seeing how that went.

The listing screen, the CSV exports it produces, and the analytics built on
what came back. Together because they are one loop: you export a file, you mark
what you listed, and the numbers on the analytics screen are the answer to
whether that was worth doing.

Every channel here is a CSV you upload yourself. There is no API client behind
any of these names and no credential held for any of them, which the screen
says plainly rather than implying a connection that does not exist.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Iterator
from typing import Any
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select

from foilstack import db, inventory, tcgplayer
from foilstack.config import Settings
from foilstack.plugins import export_plugins
from foilstack.web import joblog
from foilstack.web.chrome import CHANNELS, _aware, _chrome, templates
from foilstack.web.deps import (
    Selection,
    api_owner,
    db_session,
    owner,
    pricing_dep,
    selection_dep,
    settings_dep,
)
from foilstack.web.routes import inventory as inv_routes

router = APIRouter()


# How many lines one "select all matching" run may cover.
#
# Not a policy about how much a seller may list — it is a guard on a request
# that resolves an unbounded filter, and the ceiling is well above any real
# inventory. Without one, a hand-edited `sel=all` on an enormous account turns
# one GET into a fold over everything they own, repeatedly, from a URL short
# enough to be shared around.
MAX_SELECTED_LINES = 50_000

# How many lines of a run the table paints before it stops and says so.
#
# A display budget and nothing else. The run is whatever was selected — the
# totals, the CSV, the `Mark N` buttons and the TCGplayer round trip all cover
# every line of it, and none of them consult this number. What it bounds is the
# HTML: `sel=all` may resolve fifty thousand lines, and fifty thousand rows of
# this table is tens of megabytes the browser has to parse before it paints the
# figure the seller actually came to read.
#
# Deliberately not a pager. On `/inventory` a page is what the seller acts on,
# which is why it earns page numbers in the URL and why `sel=page` means
# something; here the *run* is the unit and a row is not clickable, so paging
# controls would suggest the run is divided when only the view is. It is also
# not lazily scrolled: the header folds every line before the page can print
# `$2,203.43 at list`, so loading rows on scroll would buy the same smaller DOM
# for a JSON endpoint and an observer, on a screen nobody browses.
#
# Set well above any selection made by ticking rows. One inventory page is 100
# grouped lines, and `export_rows` splits those by condition and finish, so a
# hand-picked run or a `sel=page` one cannot exceed this: a seller who chose
# rows one at a time always sees every one of them.
#
# What does get windowed is the run nobody picked — `sel=all`, and the bare
# `/listings` off the nav bar, which is no selection at all and which
# `_resolve` answers with the whole of stock. That second one is the common
# case rather than the exotic one, and it is the page `--shots` was timing out
# on: reaching this screen without touching inventory first prices everything
# the seller owns, and the screen says `whole inventory` because it does.
RUN_ROWS_SHOWN = 1_000

# How many set names the match form spells out before it stops naming them.
# The list exists so the seller can filter their TCGplayer export down to it;
# past a couple of dozen sets a filtered export is not meaningfully smaller
# than the whole product line, and the list has stopped being advice.
MATCH_SETS_SHOWN = 12


def _run_sets(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The product lines and sets this run covers, for the seller to filter
    their TCGplayer export down to.

    `Export Filtered CSV` exports whatever the pricing screen is filtered to,
    and a seller with no reason to filter exports the whole product line. For
    Magic that is around 800,000 rows and 100 MB, uploaded so that a few dozen
    of those rows can be kept — and the transfer is the entire cost of the
    round trip, since the matching itself is under two seconds. A 100 MB
    upload is also over the request body ceiling a proxy in front of this will
    commonly impose, so it is not only slow but a size at which the run starts
    failing outright.

    Named in `tcg_product_line` and `set_name` because those are the export's
    own spellings, which is the vocabulary the pricing screen filters in. A
    set named here that the seller cannot find in their filter would be worse
    than naming none.

    Listing the product lines is the other half, and it is why they are
    grouped rather than flattened: one upload is one product line, so a run
    spanning two of them cannot be matched from a single file however it is
    filtered, and the screen should say so by showing both.
    """
    lines: dict[str, set[str]] = {}
    for row in rows:
        line = str(row.get("tcg_product_line") or "")
        name = str(row.get("set_name") or "")
        if line and name:
            lines.setdefault(line, set()).add(name)
    return [
        {
            "line": line,
            "sets": sorted(lines[line])[:MATCH_SETS_SHOWN],
            "more": max(0, len(lines[line]) - MATCH_SETS_SHOWN),
        }
        for line in sorted(lines)
    ]


def _policy_label(pricing: inventory.Pricing) -> str:
    """What a run was priced under, for the job log to record.

    The floor is named as well as the rule because it is now a number the
    seller can change per run. A log line saying only `market` cannot answer
    the question the seller actually comes back with — why the bulk in this
    file went out at a different price from the bulk in the last one.
    """
    return f"{pricing.rule} · floor ${pricing.floor:.2f}"


def _resolve(
    session, user_id: int, pricing: inventory.Pricing, sel: Selection
) -> tuple[set[int], str]:
    """The inventory ids this run covers, and a phrase describing where from.

    Hand-picked ids pass straight through. A filter is resolved through
    `inventory.narrow` — the same call the inventory screen makes, with the
    same arguments — so "all matching these filters" means the lines that were
    on screen and not a second opinion about them.

    The description is returned rather than derived by the caller because it is
    the only thing standing between a filter run and the bug this replaced: a
    listing run that could not say what it was over, priced against everything
    the account owned while the screen behind it read `Not listed 75`.
    """
    if not sel.by_filter:
        # Empty stays empty. `export_rows` reads `None` as "everything this
        # account owns", and passing it here is what made a bare `/listings`
        # off the nav bar price the seller's whole inventory and offer to mark
        # all of it — a run nobody asked for, one click from the screen's most
        # consequential button. Reaching this screen without a selection is now
        # a screen that says so and offers a way to make one.
        return set(sel.ids), ""

    copies = inventory.index(session, user_id, pricing)
    found = inventory.narrow(
        copies, show=sel.show, q=sel.q, wire=sel.wire(), sort=sel.sort, dir=sel.dir
    )
    rows = found.rows
    if sel.sel == "page":
        # The same window, arrived at the same way. `page` is clamped here as
        # it is there, so a run started from the last page of a result that has
        # since shrunk lists that page rather than nothing.
        last = max(1, math.ceil(len(rows) / inv_routes.PAGE_SIZE))
        page = min(max(sel.page, 1), last)
        start = (page - 1) * inv_routes.PAGE_SIZE
        rows = rows[start : start + inv_routes.PAGE_SIZE]
    elif len(rows) > MAX_SELECTED_LINES:
        raise HTTPException(
            400,
            f"that filter matches {len(rows):,} lines, which is more than one "
            f"listing run may cover. Narrow it further and list in batches.",
        )

    return {i for line in rows for i in line["ids"]}, _describe(sel, len(rows))


def _describe(sel: Selection, lines: int) -> str:
    """What the seller asked for, in words, for the screen to repeat back.

    A count alone is not enough. The whole hazard of selecting by filter rather
    than by ticking rows is that the seller cannot see what was selected, so
    the run has to state its own terms — and state them from the parameters it
    actually resolved, not from what the previous screen displayed.
    """
    where = []
    if sel.show and sel.show != inventory.DEFAULT_SHOW:
        where.append(
            {"sold": "sold", "all": "stock and sold", "printing": "needs printing"}.get(
                sel.show, sel.show
            )
        )
    if sel.q:
        where.append(f"matching \u201c{sel.q}\u201d")
    for key in inventory.FACET_KEYS:
        values = sel.picks.get(key) or ()
        if values:
            where.append(", ".join(values))
    scope = "this page of" if sel.sel == "page" else "all"
    plural = "" if lines == 1 else "s"
    return f"{scope} {lines:,} line{plural}" + (" · " + " · ".join(where) if where else "")


@router.get("/listings", response_class=HTMLResponse)
def page_listings(
    request: Request,
    channel: list[str] | None = Query(None),
    pricing: inventory.Pricing = Depends(pricing_dep),
    sel: Selection = Depends(selection_dep),
    session=Depends(db_session),
    user: db.User = Depends(owner),
    settings: Settings = Depends(settings_dep),
):
    """A listing run: the selected rows, priced by one rule, for one or more
    marketplaces. It ends in a CSV — nothing here posts to a marketplace."""
    chosen, described = _resolve(session, user.id, pricing, sel)
    picked = set(channel or ["tcgplayer"])
    rows = inventory.export_rows(session, user.id, pricing, ids=chosen)
    run_value = sum((r["list_price"] or 0) * r["quantity"] for r in rows)
    market_value = sum((r["market"] or 0) * r["quantity"] for r in rows)

    # Two different counts, and the screen needs both. A row here is one
    # marketplace listing; a card is one thing in a box. Four duplicate copies
    # make 41 cards into 37 listings, and a button offering to mark "37" while
    # inventory and the topbar both say 41 reads as a bug in whichever number
    # the seller trusts less.
    card_count = sum(r["quantity"] for r in rows)
    # What pressing the button would actually change: the copies not yet
    # recorded as listed on every channel selected for this run. A card marked
    # on TCGplayer is still unlisted on eBay, so this grows again the moment a
    # second channel is ticked.
    mark_ids = sorted(
        item_id
        for r in rows
        for item_id, on in r["copy_channels"].items()
        if not picked.issubset(on)
    )
    # And what the other button would change: the copies recorded on at least
    # one channel in this run. The two overlap on purpose — a copy on
    # TCGplayer with eBay also ticked has something to add and something to
    # take away, and offering only one of those would make the pair of buttons
    # disagree about the same row.
    unmark_ids = sorted(
        item_id for r in rows for item_id, on in r["copy_channels"].items() if picked & set(on)
    )
    picked_label = ", ".join(c["name"] for c in CHANNELS if c["key"] in picked)

    # Windowed last, after every figure above has been folded over the whole
    # run. `line_count` is the run; `rows` is what gets painted. The template
    # counts listings off `line_count` for that reason — a status bar reporting
    # the window would be the truncated-count bug the review queue already had,
    # where a heading said `20 cards` over a batch of fifty.
    line_count = len(rows)
    shown = rows[:RUN_ROWS_SHOWN]

    # Whether to offer the way in, for a seller who arrived with nothing
    # selected. Counted in SQL rather than off `rows`, which is empty in
    # exactly the case this is for.
    #
    # A test for "is there anything to list", not a figure to print. The run
    # behind the link matches inventory lines and takes them whole, so it is
    # reliably larger than this; showing it on the button promised 19,746 and
    # delivered 28,160.
    #
    # It is a link and not a default: `?sel=all&listed=unlisted` is a stated
    # selection that arrives in the URL, can be reloaded onto and reads back
    # in the run's own terms. That is the whole distinction — the widest
    # useful run is one deliberate click away, rather than the thing that
    # happens to you for clicking `Listings` in the nav.
    unlisted = 0
    if not rows:
        unlisted = (
            session.scalar(
                select(func.count(db.InventoryItem.id)).where(
                    db.InventoryItem.user_id == user.id,
                    db.InventoryItem.status == "stock",
                    db.InventoryItem.listed == 0,
                )
            )
            or 0
        )

    # The selection travels onward as whatever it arrived as. Re-encoding a
    # filter run as its resolved ids would put the length ceiling back on the
    # one URL the browser follows after the run is priced — and it is the
    # longest one, because an export link carries the whole selection too.
    ids = "".join(f"&{k}={quote_plus(v)}" for k, v in sel.query_items())
    chans = "".join(f"&channel={c}" for c in sorted(picked))
    # And so does the floor, but only when it is not the seller's own. Carried
    # unconditionally it would pin every link on the page to a number that
    # happened to be current, so a seller who saved a new floor in another tab
    # would keep exporting under the old one from links drawn before the save.
    override = pricing.floor != user.price_floor
    fq = f"&floor={pricing.floor:.2f}" if override else ""
    exporters = export_plugins()
    return templates.TemplateResponse(
        request,
        "listings.html",
        {
            "nav": "listings",
            "rows": shown,
            # The run, and how much of it the table is showing. Both, because
            # a seller looking at a thousand rows under a heading that says
            # 3,884 has to be told plainly that the missing ones are in the
            # file — a window that looks like a truncation is worse than no
            # window, since the fix for a truncated export is not obvious and
            # the seller would have no reason to trust the CSV.
            "line_count": line_count,
            # Only meaningful on the empty screen, and zero everywhere else so
            # the template has one thing to test.
            "unlisted_count": unlisted,
            "unlisted_href": f"/listings?rule={pricing.rule}{chans}{fq}&sel=all&listed=unlisted",
            "rows_hidden": line_count - len(shown),
            # Preformatted, as `matched_label` is on the inventory screen:
            # Jinja has no thousands filter here and a run is exactly the size
            # at which the separators start mattering.
            "shown_label": f"{len(shown):,}",
            "line_label": f"{line_count:,}",
            "rule": pricing.rule,
            "rules": [
                {
                    **r,
                    "on": r["id"] == pricing.rule,
                    "href": f"/listings?rule={r['id']}{chans}{fq}{ids}",
                }
                for r in inventory.RULES
            ],
            "rule_obj": inventory.rule_by_id(pricing.rule),
            "channels": [
                {
                    **c,
                    "on": c["key"] in picked,
                    "exporter": exporters.get(c["key"]),
                    "csv_href": f"/export/{c['key']}?rule={pricing.rule}{fq}{ids}",
                    # Only TCGplayer needs a file to start from, and only
                    # because its ids are SKU ids nobody outside their own
                    # export can know. eBay's sheet is composed from nothing.
                    "match_href": (
                        f"/export/tcgplayer/match?rule={pricing.rule}{fq}{ids}"
                        if c["key"] == "tcgplayer"
                        else None
                    ),
                    "href": "/listings?rule="
                    + pricing.rule
                    + "".join(
                        f"&channel={k}"
                        for k in sorted(
                            picked - {c["key"]} if c["key"] in picked else picked | {c["key"]}
                        )
                    )
                    + fq
                    + ids,
                }
                for c in CHANNELS
            ],
            "picked": sorted(picked),
            # What to filter a TCGplayer export down to. Only the match
            # form uses it, but it is a fact about the run rather than
            # about one channel.
            "run_sets": _run_sets(rows),
            "picked_label": picked_label,
            "card_count": card_count,
            "mark_ids": mark_ids,
            "unmark_ids": unmark_ids,
            "selected_ids": sorted(chosen),
            # What this run is over, in words, when it was chosen by filter
            # rather than by ticking rows. Empty for a hand-picked run, where
            # the seller has already seen every line they chose.
            "described": described,
            "run_value": run_value,
            "market_value": market_value,
            "delta": run_value - market_value,
            # Three separate facts, and the screen says all three. The floor
            # in force, the seller's own, and whether those are the same —
            # a run priced under a number the seller never saved is exactly
            # the thing that must not be able to pass for their settings.
            "floor": pricing.floor,
            "account_floor": user.price_floor,
            "floor_override": override,
            "max_floor": inventory.MAX_FLOOR,
            # The rest of the run, as fields, so the floor control can
            # re-ask for this same run with one number changed. Built from
            # what was resolved rather than from the URL, for the reason
            # `_describe` exists.
            "floor_form": (
                [("rule", pricing.rule)]
                + [("channel", c) for c in sorted(picked)]
                + list(sel.query_items())
            ),
            "log": joblog.entries(user.id),
            **_chrome(session, request, user, settings),
        },
    )


@router.get("/analytics", response_class=HTMLResponse)
def page_analytics(
    request: Request,
    # The value threshold for this look at the screen. `/analytics?min=1.00`
    # reports one position; `POST /api/account/value-threshold` is what makes
    # it the seller's own. Two presses, for the reason the floor has two: a
    # number typed to see what the shelf looks like without the bulk must not
    # become the account's settings because they hit Enter in a field.
    # Aliased rather than named `min`, which shadows the builtin inside a
    # function that does arithmetic.
    min_value: str | None = Query(None, alias="min"),
    pricing: inventory.Pricing = Depends(pricing_dep),
    session=Depends(db_session),
    user: db.User = Depends(owner),
    settings: Settings = Depends(settings_dep),
):
    """Position, not performance.

    Everything real on this screen is computed from inventory this account
    holds. Sell-through, realised profit and days-to-sell need sales, and
    foilstack never sees a sale: the CSV leaves here and what happens to it
    happens on a marketplace. Those panels are marked as the demo figures they
    are rather than dressed up as measurements.

    The value threshold divides this screen in two, and which side a figure
    falls on is a question about what the figure *is*. What a position is
    worth is a forecast — it assumes the cards sell — so a shelf of ten-cent
    commons that will mostly never move is exactly the thing a seller may want
    out of it, and inventory value, listed, not-listed and the by-game bars
    all follow the threshold. Cost basis is not a forecast: that money left a
    bank account for every card held, cheap ones included, so it is always the
    whole of stock. Realised figures are history and are never filtered at
    all — a common that sold for a dime earned a real dime.
    """
    threshold = inventory.threshold_for(user, min_value)
    rows = inventory.items(session, user.id, pricing)
    totals = inventory.totals(rows)

    # Stock only, on both sides. `by_game` used to fold every row including
    # sold ones, so a panel headed "Inventory value by game" disagreed with
    # the "Inventory value" tile directly above it by whatever had been sold
    # — the same fault the `listed_value` comment below describes, one panel
    # over.
    stock = [r for r in rows if not r["sold"]]
    counted, excluded = inventory.split_by_value(stock, threshold)

    by_game: dict[str, float] = {}
    for r in counted:
        by_game[r["game"]] = by_game.get(r["game"], 0.0) + (r["market"] or 0) * r["quantity"]
    top = sorted(by_game.items(), key=lambda kv: -kv[1])[:5]
    peak = max((v for _, v in top), default=0.0) or 1.0
    # Stock only, to match `totals["market"]` — a sold row counted here made
    # "listed value" include cards that are no longer on the shelf, and the
    # "not yet listed" figure beside it is that total minus this one, so one
    # sold-and-listed card overstated the first and understated the second.
    listed_value = sum(r["market"] or 0 for r in counted if r["listed"])

    # What the screen reports as the position, at this threshold. Held apart
    # from `totals` rather than replacing its keys: `totals` is what the
    # account owns and several figures still need that — the cost basis, and
    # sell-through, whose denominator is every card on the shelf and not just
    # the ones worth counting.
    counted_market = round(sum(r["market"] or 0 for r in counted), 2)
    position = {
        "count": len(counted),
        "distinct": len({r["card_id"] for r in counted}),
        "market": counted_market,
        "listed": round(sum(r["list_price"] or 0 for r in counted), 2),
        # Counted value against the cost of everything held. Deliberately
        # mixed, and said so on screen: the cards left out cost real money, so
        # netting them out of both sides would report a gain on a position the
        # seller did not pay for.
        "gain": round(counted_market - totals["cost"], 2) if totals["cost"] else None,
    }
    left_out = {
        "count": len(excluded),
        "distinct": len({r["card_id"] for r in excluded}),
        "market": round(sum(r["market"] or 0 for r in excluded), 2),
    }

    # Real sales, now that they are recorded. Sell-through and days-to-sell are
    # computable from what we hold; fees and shipping are not, and are not
    # invented here — they are named as missing on the screen instead.
    sold = [r for r in rows if r["sold"]]
    horizon = dt.datetime.now(dt.UTC) - dt.timedelta(days=30)
    recent = [r for r in sold if r["sold_at"] and _aware(r["sold_at"]) >= horizon]
    held_days = [
        (_aware(r["sold_at"]) - _aware(r["created_at"])).days
        for r in sold
        if r["sold_at"] and r.get("created_at")
    ]
    sale_stats = {
        "sold_30d": len(recent),
        "gross_30d": sum(r["sold_price"] or 0 for r in recent),
        "sold_all": len(sold),
        "gross_all": totals["realised"],
        "profit_all": totals["realised_profit"],
        "sell_through": (
            round(100 * len(sold) / (len(sold) + totals["count"]))
            if (len(sold) + totals["count"])
            else None
        ),
        "avg_days": round(sum(held_days) / len(held_days), 1) if held_days else None,
        "costed": sum(1 for r in sold if r["cost"] is not None),
    }

    return templates.TemplateResponse(
        request,
        "analytics.html",
        {
            "nav": "analytics",
            "rows": rows,
            "totals": totals,
            "position": position,
            "left_out": left_out,
            "sales": sale_stats,
            "by_game": [{"label": g, "value": v, "pct": f"{100 * v / peak:.0f}%"} for g, v in top],
            "listed_value": listed_value,
            "unlisted_value": round(counted_market - listed_value, 2),
            # Three separate facts, the same three the floor control states:
            # what is in force, what the account saved, and whether those are
            # the same. A screen reporting a position under a threshold the
            # seller never saved must not be able to pass for their settings.
            "threshold": threshold,
            "account_threshold": user.value_threshold,
            "threshold_override": threshold != user.value_threshold,
            "max_threshold": inventory.MAX_THRESHOLD,
            **_chrome(session, request, user, settings),
        },
    )


def _marking_targets(payload: dict, session, user_id: int) -> tuple[list, list[str]]:
    """The rows and channels named by a mark/unmark request.

    Shared so the two directions cannot drift, and so neither can be written
    without the `user_id` filter — the whole difference between marking your
    own cards and marking somebody else's is one `where` clause.
    """
    ids = [int(i) for i in (payload.get("ids") or [])]
    channels = [c for c in (payload.get("channels") or []) if c in {c2["key"] for c2 in CHANNELS}]
    if not ids:
        raise HTTPException(400, "no rows selected")
    if not channels:
        raise HTTPException(400, "no channels selected")
    items = session.scalars(
        select(db.InventoryItem).where(
            db.InventoryItem.id.in_(ids),
            db.InventoryItem.user_id == user_id,
        )
    ).all()
    return list(items), channels


@router.post("/api/listings/mark")
async def api_mark_listed(
    request: Request,
    session=Depends(db_session),
    user: db.User = Depends(api_owner),
):
    """Record that these rows have been listed, on these channels.

    This marks our own database. It does not talk to a marketplace, and the
    button that calls it does not claim to: you export the CSV, you upload it,
    and this is how you tell foilstack you did.
    """
    items, channels = _marking_targets(await request.json(), session, user.id)
    now = dt.datetime.now(dt.UTC)
    for item in items:
        # Added to, not replaced. A card listed on TCGplayer and then also on
        # eBay is on both, and overwriting the label said the seller had taken
        # the first listing down — which they never told us. Taking one down is
        # `unmark`, which names the channel it is removing.
        on = set(inventory.merge_channels([item.listed_channels or ""])) | set(channels)
        item.listed = 1
        item.listed_channels = ", ".join(sorted(on))
        item.listed_at = now
    session.commit()
    joblog.add(user.id, f"marked {len(items)} cards listed on {', '.join(channels)}")
    return {"ok": True, "marked": len(items)}


@router.post("/api/listings/unmark")
async def api_unmark_listed(
    request: Request,
    session=Depends(db_session),
    user: db.User = Depends(api_owner),
):
    """Take these rows back off these channels — in our record of it.

    The counterpart to `mark`, and the same disclaimer twice over: this does
    not delete a live listing any more than marking created one. A seller who
    ends an auction tells foilstack here, and the card returns to the run so
    the next export contains it again.
    """
    items, channels = _marking_targets(await request.json(), session, user.id)
    dropped = set(channels)
    changed = 0
    for item in items:
        on = set(inventory.merge_channels([item.listed_channels or ""]))
        left = on - dropped
        if left == on:
            continue
        changed += 1
        item.listed_channels = ", ".join(sorted(left)) or None
        # `listed` is "on a marketplace somewhere", so it survives losing one
        # of two channels and falls only when the last one goes. `listed_at`
        # goes with it: on a row still listed it dates the remaining listing,
        # which this did not touch, and on an empty one it would date a
        # listing that no longer exists.
        item.listed = 1 if left else 0
        if not left:
            item.listed_at = None
    session.commit()
    joblog.add(user.id, f"unmarked {changed} cards on {', '.join(channels)}")
    return {"ok": True, "unmarked": changed}


def _require_selection(sel: Selection, chosen: set[int]) -> None:
    """Refuse to build a file for a run nobody chose.

    The screen can answer "nothing selected" with a screen. A download cannot:
    a header-only CSV is a file that looks like a file, uploads without
    complaint and does nothing, and nothing about it says the selection was
    the problem. That is the shape of the dead-file trap the TCGplayer export
    button was removed from the inventory bar for.

    Only a *missing* selection is refused. A filter that legitimately matched
    nothing is an explicit question with an empty answer, and an empty file is
    the honest reply to it.
    """
    if not sel.by_filter and not chosen:
        raise HTTPException(
            400,
            "nothing selected. Pick rows on the inventory screen and press "
            "List selected, or open /listings to list everything unlisted.",
        )


# Declared ahead of `/export/{name}`. FastAPI matches in declaration order and
# these do not overlap — one is a POST two segments deep — but the pair is the
# same shape as the one that has already broken here once, and keeping the
# specific route first costs nothing.
@router.post("/export/tcgplayer/match")
async def export_tcgplayer_match(
    file: UploadFile = File(...),
    pricing: inventory.Pricing = Depends(pricing_dep),
    sel: Selection = Depends(selection_dep),
    session=Depends(db_session),
    user: db.User = Depends(owner),
):
    """A TCGplayer pricing export in, the seller's own rows back out.

    The upload is theirs and stays theirs: it is read once, never written to
    disk by us, and nothing from it is stored. One column of it is read — the
    quantity they already hold, because `Add to Quantity` is a delta and our
    stock is a total. See `foilstack.tcgplayer` for why the round trip is
    necessary at all: TCGplayer identifies a listing by a SKU id this
    catalogue has no way to know.
    """
    chosen, _ = _resolve(session, user.id, pricing, sel)
    _require_selection(sel, chosen)
    rows = inventory.export_rows(session, user.id, pricing, ids=chosen)
    if not rows:
        raise HTTPException(400, "nothing in stock to list")

    try:
        body, report = await run_in_threadpool(tcgplayer.fill, _chunks(file), rows)
    except tcgplayer.NotAPricingExport as exc:
        joblog.add(user.id, f"tcgplayer match rejected · {exc}")
        raise HTTPException(400, str(exc)) from exc

    joblog.add(user.id, f"tcgplayer match · {report.summary()} · {_policy_label(pricing)}")
    # Named individually rather than counted, because "12 not in the export"
    # is a number a seller can do nothing with and a list of twelve cards is
    # twelve things they can go and check.
    for label in (report.unmatched + report.ambiguous + report.unpriced + report.unreadable)[:6]:
        joblog.add(user.id, f"  skipped {label}")
    # Named too, and not as a skip: these are rows the file takes *down*,
    # which is the one thing in it the seller did not choose card by card.
    for label in report.reduced[:6]:
        joblog.add(user.id, f"  reduced {label}")

    return Response(
        content=body,
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="tcgplayer-listings.csv"',
            # The same summary the job log gets, for the page that posted this
            # to state without reloading. A CSV response is a download rather
            # than a navigation, so nothing on the screen changes when this
            # succeeds — the seller got a file and no report, and the counts
            # naming what was skipped sat in a log they had no reason to
            # re-read. Counts and ASCII words only; the card names that would
            # not survive a header stay in the log.
            "X-Match-Report": report.ascii_summary(),
        },
    )


def _chunks(file: UploadFile) -> Iterator[bytes]:
    """The upload in blocks, with a ceiling.

    A whole product line is around 100 MB, so the ceiling has to be generous —
    but it has to exist, because this route accepts a file and the size of that
    file is decided by whoever is signed in.
    """
    total = 0
    while chunk := file.file.read(1 << 20):
        total += len(chunk)
        if total > tcgplayer.MAX_UPLOAD_BYTES:
            raise tcgplayer.NotAPricingExport(
                f"that file is larger than {tcgplayer.MAX_UPLOAD_BYTES // (1 << 20)} MB"
            )
        yield chunk


@router.get("/export/{name}")
def export_csv(
    name: str,
    pricing: inventory.Pricing = Depends(pricing_dep),
    sel: Selection = Depends(selection_dep),
    session=Depends(db_session),
    user: db.User = Depends(owner),
):
    spec = export_plugins().get(name)
    if spec is None:
        raise HTTPException(404, "no such exporter")
    chosen, _ = _resolve(session, user.id, pricing, sel)
    _require_selection(sel, chosen)
    rows = inventory.export_rows(session, user.id, pricing, ids=chosen)
    body = spec.render(rows)
    joblog.add(user.id, f"wrote {spec.filename} · {len(rows)} rows · {_policy_label(pricing)}")
    return Response(
        content=body,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{spec.filename}"'},
    )

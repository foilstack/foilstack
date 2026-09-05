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
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from sqlalchemy import select

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


def _resolve(session, user_id: int, rule: str, sel: Selection) -> tuple[set[int], str]:
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
        return set(sel.ids), ""

    copies = inventory.index(session, user_id, rule)
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
    rule: str = inventory.DEFAULT_RULE,
    channel: list[str] | None = Query(None),
    sel: Selection = Depends(selection_dep),
    session=Depends(db_session),
    user: db.User = Depends(owner),
    settings: Settings = Depends(settings_dep),
):
    """A listing run: the selected rows, priced by one rule, for one or more
    marketplaces. It ends in a CSV — nothing here posts to a marketplace."""
    rule = rule if rule in inventory.RULE_IDS else inventory.DEFAULT_RULE
    chosen, described = _resolve(session, user.id, rule, sel)
    picked = set(channel or ["tcgplayer"])
    rows = inventory.export_rows(session, user.id, rule, ids=chosen or None)
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

    # The selection travels onward as whatever it arrived as. Re-encoding a
    # filter run as its resolved ids would put the length ceiling back on the
    # one URL the browser follows after the run is priced — and it is the
    # longest one, because an export link carries the whole selection too.
    ids = "".join(f"&{k}={quote_plus(v)}" for k, v in sel.query_items())
    chans = "".join(f"&channel={c}" for c in sorted(picked))
    exporters = export_plugins()
    return templates.TemplateResponse(
        request,
        "listings.html",
        {
            "nav": "listings",
            "rows": rows,
            "rule": rule,
            "rules": [
                {**r, "on": r["id"] == rule, "href": f"/listings?rule={r['id']}{chans}{ids}"}
                for r in inventory.RULES
            ],
            "rule_obj": inventory.rule_by_id(rule),
            "channels": [
                {
                    **c,
                    "on": c["key"] in picked,
                    "exporter": exporters.get(c["key"]),
                    "csv_href": f"/export/{c['key']}?rule={rule}{ids}",
                    # Only TCGplayer needs a file to start from, and only
                    # because its ids are SKU ids nobody outside their own
                    # export can know. eBay's sheet is composed from nothing.
                    "match_href": (
                        f"/export/tcgplayer/match?rule={rule}{ids}"
                        if c["key"] == "tcgplayer"
                        else None
                    ),
                    "href": "/listings?rule="
                    + rule
                    + "".join(
                        f"&channel={k}"
                        for k in sorted(
                            picked - {c["key"]} if c["key"] in picked else picked | {c["key"]}
                        )
                    )
                    + ids,
                }
                for c in CHANNELS
            ],
            "picked": sorted(picked),
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
            "floor": inventory.FLOOR,
            "log": joblog.entries(user.id),
            **_chrome(session, request, user, settings),
        },
    )


@router.get("/analytics", response_class=HTMLResponse)
def page_analytics(
    request: Request,
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
    """
    rows = inventory.items(session, user.id)
    totals = inventory.totals(rows)
    by_game: dict[str, float] = {}
    for r in rows:
        by_game[r["game"]] = by_game.get(r["game"], 0.0) + (r["market"] or 0) * r["quantity"]
    top = sorted(by_game.items(), key=lambda kv: -kv[1])[:5]
    peak = max((v for _, v in top), default=0.0) or 1.0
    # Stock only, to match `totals["market"]` — a sold row counted here made
    # "listed value" include cards that are no longer on the shelf, and the
    # "not yet listed" figure beside it is that total minus this one, so one
    # sold-and-listed card overstated the first and understated the second.
    listed_value = sum(r["market"] or 0 for r in rows if r["listed"] and not r["sold"])

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
            "sales": sale_stats,
            "by_game": [{"label": g, "value": v, "pct": f"{100 * v / peak:.0f}%"} for g, v in top],
            "listed_value": listed_value,
            "unlisted_value": totals["market"] - listed_value,
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


# Declared ahead of `/export/{name}`. FastAPI matches in declaration order and
# these do not overlap — one is a POST two segments deep — but the pair is the
# same shape as the one that has already broken here once, and keeping the
# specific route first costs nothing.
@router.post("/export/tcgplayer/match")
async def export_tcgplayer_match(
    file: UploadFile = File(...),
    rule: str = inventory.DEFAULT_RULE,
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
    rule = rule if rule in inventory.RULE_IDS else inventory.DEFAULT_RULE
    chosen, _ = _resolve(session, user.id, rule, sel)
    rows = inventory.export_rows(session, user.id, rule, ids=chosen or None)
    if not rows:
        raise HTTPException(400, "nothing in stock to list")

    try:
        body, report = await run_in_threadpool(tcgplayer.fill, _chunks(file), rows)
    except tcgplayer.NotAPricingExport as exc:
        joblog.add(user.id, f"tcgplayer match rejected · {exc}")
        raise HTTPException(400, str(exc)) from exc

    joblog.add(user.id, f"tcgplayer match · {report.summary()} · {rule}")
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
        headers={"Content-Disposition": 'attachment; filename="tcgplayer-listings.csv"'},
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
    rule: str = inventory.DEFAULT_RULE,
    sel: Selection = Depends(selection_dep),
    session=Depends(db_session),
    user: db.User = Depends(owner),
):
    spec = export_plugins().get(name)
    if spec is None:
        raise HTTPException(404, "no such exporter")
    rule = rule if rule in inventory.RULE_IDS else inventory.DEFAULT_RULE
    chosen, _ = _resolve(session, user.id, rule, sel)
    rows = inventory.export_rows(session, user.id, rule, ids=chosen or None)
    body = spec.render(rows)
    joblog.add(user.id, f"wrote {spec.filename} · {len(rows)} rows · {rule}")
    return Response(
        content=body,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{spec.filename}"'},
    )

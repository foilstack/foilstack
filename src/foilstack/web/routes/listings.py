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
from collections.abc import Iterator

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from foilstack import db, inventory, tcgplayer
from foilstack.config import Settings
from foilstack.plugins import export_plugins
from foilstack.web import joblog
from foilstack.web.chrome import CHANNELS, _aware, _chrome, templates
from foilstack.web.deps import api_owner, db_session, owner, settings_dep

router = APIRouter()


@router.get("/listings", response_class=HTMLResponse)
def page_listings(
    request: Request,
    id: list[int] | None = Query(None),
    rule: str = inventory.DEFAULT_RULE,
    channel: list[str] | None = Query(None),
    session=Depends(db_session),
    user: db.User = Depends(owner),
    settings: Settings = Depends(settings_dep),
):
    """A listing run: the selected rows, priced by one rule, for one or more
    marketplaces. It ends in a CSV — nothing here posts to a marketplace."""
    rule = rule if rule in inventory.RULE_IDS else inventory.DEFAULT_RULE
    chosen = set(id or [])
    picked = set(channel or ["tcgplayer"])
    rows = inventory.export_rows(session, user.id, rule, ids=chosen or None)
    run_value = sum((r["list_price"] or 0) * r["quantity"] for r in rows)
    market_value = sum((r["market"] or 0) * r["quantity"] for r in rows)
    ids = "".join(f"&id={i}" for i in sorted(chosen))
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
            "selected_ids": sorted(chosen),
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
    listed_value = sum(r["market"] or 0 for r in rows if r["listed"])

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
    payload = await request.json()
    ids = [int(i) for i in (payload.get("ids") or [])]
    channels = [c for c in (payload.get("channels") or []) if c in {c2["key"] for c2 in CHANNELS}]
    if not ids:
        raise HTTPException(400, "no rows selected")
    if not channels:
        raise HTTPException(400, "no channels selected")

    label = ", ".join(channels)
    items = session.scalars(
        select(db.InventoryItem).where(
            db.InventoryItem.id.in_(ids),
            db.InventoryItem.user_id == user.id,
        )
    ).all()
    for item in items:
        item.listed = 1
        item.listed_channels = label
        item.listed_at = dt.datetime.now(dt.UTC)
    session.commit()
    joblog.add(user.id, f"marked {len(items)} rows listed on {label}")
    return {"ok": True, "marked": len(items)}


# Declared ahead of `/export/{name}`. FastAPI matches in declaration order and
# these do not overlap — one is a POST two segments deep — but the pair is the
# same shape as the one that has already broken here once, and keeping the
# specific route first costs nothing.
@router.post("/export/tcgplayer/match")
async def export_tcgplayer_match(
    file: UploadFile = File(...),
    rule: str = inventory.DEFAULT_RULE,
    id: list[int] | None = Query(None),
    session=Depends(db_session),
    user: db.User = Depends(owner),
):
    """A TCGplayer pricing export in, the seller's own rows back out.

    The upload is theirs and stays theirs: it is read once, never written to
    disk by us, and nothing from it is stored. The quantities it contains are
    their positions on another marketplace and are not read at all. See
    `foilstack.tcgplayer` for why the round trip is necessary — TCGplayer
    identifies a listing by a SKU id this catalogue has no way to know.
    """
    rule = rule if rule in inventory.RULE_IDS else inventory.DEFAULT_RULE
    chosen = set(id or [])
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
    for label in (report.unmatched + report.ambiguous + report.unpriced)[:6]:
        joblog.add(user.id, f"  skipped {label}")

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
    id: list[int] | None = Query(None),
    session=Depends(db_session),
    user: db.User = Depends(owner),
):
    spec = export_plugins().get(name)
    if spec is None:
        raise HTTPException(404, "no such exporter")
    rule = rule if rule in inventory.RULE_IDS else inventory.DEFAULT_RULE
    chosen = set(id or [])
    rows = inventory.export_rows(session, user.id, rule, ids=chosen or None)
    body = spec.render(rows)
    joblog.add(user.id, f"wrote {spec.filename} · {len(rows)} rows · {rule}")
    return Response(
        content=body,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{spec.filename}"'},
    )

"""The inventory: what a seller owns, and editing it.

**Declaration order is load-bearing here.** `/api/inventory/delete` and
`/api/inventory/{item_id}` have the same shape, and FastAPI matches in the
order routes are declared, so with these the other way round a POST to
`delete` is captured by the `{item_id}` route, `"delete"` fails to parse as an
integer, and bulk delete answers 422. The extraction that produced this file
preserved the original order for exactly that reason; keep it.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select

from foilstack import db, inventory, prices, search
from foilstack.config import Settings
from foilstack.plugins import export_plugins
from foilstack.web import joblog
from foilstack.web.chrome import _chrome, templates
from foilstack.web.deps import api_owner, db_session, owner, settings_dep

router = APIRouter()


@router.get("/inventory", response_class=HTMLResponse)
def page_inventory(
    request: Request,
    q: str = "",
    rule: str = inventory.DEFAULT_RULE,
    show: str = "stock",
    session=Depends(db_session),
    user: db.User = Depends(owner),
    settings: Settings = Depends(settings_dep),
):
    rule = rule if rule in inventory.RULE_IDS else inventory.DEFAULT_RULE
    copies = inventory.items(session, user.id, rule)
    counts = {
        "all": len(copies),
        "stock": sum(1 for r in copies if not r["sold"]),
    }
    counts["sold"] = counts["all"] - counts["stock"]
    counts["printing"] = sum(1 for r in copies if r["printing_guessed"] and not r["sold"])

    wanted = show if show in ("stock", "sold") else None
    rows = inventory.groups(session, user.id, rule, status=wanted)
    if show == "printing":
        # The pass to make after an import: every line still priced on a guess
        # between printings, in one place, instead of hunting for the `?`.
        rows = [r for r in inventory.groups(session, user.id, rule, status="stock") if r["guessed"]]
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
    return templates.TemplateResponse(
        request,
        "inventory.html",
        {
            "nav": "inventory",
            "rows": rows,
            "q": q,
            "rule": rule,
            "show": show,
            "counts": counts,
            "totals": inventory.totals(copies),
            "exporters": export_plugins().values(),
            **_chrome(session, request, user, settings),
        },
    )


@router.get("/inventory/{card_id}", response_class=HTMLResponse)
def page_card(
    card_id: int,
    request: Request,
    rule: str = inventory.DEFAULT_RULE,
    session=Depends(db_session),
    user: db.User = Depends(owner),
    settings: Settings = Depends(settings_dep),
):
    """One card, and every copy of it you own, batched by the import it came in.

    This is where the quantity on the inventory screen becomes legible: which
    scans make it up, which archive each arrived in, what each cost and whether
    it has sold. Consolidated on the list, itemised here.
    """
    rule = rule if rule in inventory.RULE_IDS else inventory.DEFAULT_RULE
    line = next(
        (g for g in inventory.groups(session, user.id, rule) if g["card_id"] == card_id),
        None,
    )
    if line is None:
        raise HTTPException(404, "nothing in your inventory for that card")

    jobs = {
        j.id: j
        for j in session.scalars(select(db.ImportJob).where(db.ImportJob.user_id == user.id))
    }
    scans = {sc.id: sc for sc in session.scalars(select(db.Scan).where(db.Scan.user_id == user.id))}

    # Grouped by the archive they arrived in, because "where did these come
    # from" is the question the quantity raises.
    batches: dict[int | None, dict] = {}
    for copy in line["copies"]:
        scan = scans.get(copy["scan_id"]) if copy["scan_id"] else None
        job = jobs.get(scan.job_id) if scan else None
        copy["scan_filename"] = scan.filename if scan else None
        copy["auto_accepted"] = bool(scan.auto_accepted) if scan else False
        job_id = job.id if job else None
        batch = batches.get(job_id)
        if batch is None:
            batch = batches[job_id] = {
                "job_id": job_id,
                "filename": job.filename if job else "added without an import",
                "created_at": job.created_at if job else None,
                "copies": [],
            }
        batch["copies"].append(copy)

    ordered = sorted(
        batches.values(),
        key=lambda b: (b["created_at"] is None, b["created_at"] or 0),
        reverse=True,
    )

    # One series per printing this seller actually holds — usually one. A card
    # they own in both foil and plain is two lines at genuinely different money,
    # and averaging them would draw a price nobody can buy or sell at.
    held = sorted({c["sub_type"] for c in line["copies"] if c.get("sub_type")})
    series = prices.history(session, card_id, held)
    charts = [
        {
            "sub_type": sub,
            "summary": prices.summarise(points),
            "spark": prices.spark(points),
        }
        for sub, points in sorted(series.items())
    ]

    return templates.TemplateResponse(
        request,
        "card.html",
        {
            "nav": "inventory",
            "line": line,
            "batches": ordered,
            "rule": rule,
            "charts": charts,
            # Other printings of this card we hold a price for but the seller has
            # not claimed. Named so the page can say what it did not pick.
            "other_printings": [
                pr for pr in (line["copies"][0].get("printings") or []) if pr not in held
            ],
            **_chrome(session, request, user, settings),
        },
    )


@router.post("/api/inventory/delete")
async def api_inventory_bulk_delete(
    request: Request,
    session=Depends(db_session),
    user: db.User = Depends(api_owner),
):
    """Delete several rows at once.

    **Sold rows are refused**, and that is the whole safety model here rather
    than a nag dialog. The two kinds of row are not equally recoverable: a card
    still in stock can be re-imported, because its scan is on disk and the
    matcher is deterministic. A sold row is the only record that the sale
    happened, and it carries the cost basis that makes realised profit true —
    delete it and no later import brings it back.

    So bulk delete does the recoverable thing in bulk and leaves the
    irreversible one as a deliberate, single-row act.
    """
    payload = await request.json()
    ids = [int(i) for i in (payload.get("ids") or [])]
    if not ids:
        raise HTTPException(400, "nothing selected")

    items = session.scalars(
        select(db.InventoryItem).where(
            db.InventoryItem.id.in_(ids),
            db.InventoryItem.user_id == user.id,
        )
    ).all()
    if not items:
        raise HTTPException(404, "nothing to delete")

    sold = [i for i in items if i.status == "sold"]
    if sold:
        raise HTTPException(
            400,
            f"{len(sold)} of these {'has' if len(sold) == 1 else 'have'} sold. "
            "A sold row is the only record of "
            "that sale and carries the cost basis behind your realised profit. "
            "Delete those one at a time, from the card page, if you really mean to.",
        )

    count = len(items)
    _delete_items(session, items)
    session.commit()
    joblog.add(user.id, f"deleted {count} rows from inventory")
    return {"ok": True, "deleted": count}


@router.get("/api/inventory/{item_id}", response_class=HTMLResponse)
def api_inventory_panel(
    item_id: int,
    request: Request,
    session=Depends(db_session),
    user: db.User = Depends(owner),
):
    """The edit panel for one row, as an HTML fragment.

    Rendered server-side rather than assembled in JavaScript: it is a form over
    fields the templates already know how to draw, and shipping a second
    rendering of the same row in JS is how the two drift apart.
    """
    item = session.get(db.InventoryItem, item_id)
    if item is None or item.user_id != user.id:
        raise HTTPException(404, "no such row")
    rows = inventory.items(session, user.id)
    row = next((r for r in rows if r["id"] == item_id), None)
    if row is None:
        raise HTTPException(404, "no such row")
    return templates.TemplateResponse(
        request,
        "_card_panel.html",
        {
            "r": row,
            "conditions": inventory.CONDITIONS,
            "finishes": inventory.FINISHES,
            "finish_label": inventory.FINISH_LABEL,
            "games": search.games(session),
        },
    )


@router.post("/api/inventory/{item_id}")
async def api_inventory_update(
    item_id: int,
    request: Request,
    session=Depends(db_session),
    user: db.User = Depends(api_owner),
):
    """Edit one row: condition, finish, quantity, cost, notes, sold state."""
    item = session.get(db.InventoryItem, item_id)
    if item is None or item.user_id != user.id:
        raise HTTPException(404, "no such row")
    payload = await request.json()

    if "card_id" in payload:
        # The row is pointing at the wrong card. Until this existed the only
        # remedy was to delete the row — which discards the scan with it, so
        # correcting a mistake cost you the evidence of what you actually
        # photographed.
        card = session.get(db.Card, int(payload["card_id"]))
        if card is None:
            raise HTTPException(400, "no such card")
        if card.id != item.card_id:
            item.card_id = card.id
            # A declared printing belongs to the card it was declared on. Kept
            # across a correction it would price the new card by a name that
            # may not exist in its price rows, which is worse than the guess it
            # replaced because it looks deliberate.
            item.sub_type = None

    if "condition" in payload:
        if payload["condition"] not in inventory.CONDITIONS:
            raise HTTPException(400, "unknown condition")
        item.condition = payload["condition"]
    if "finish" in payload:
        if payload["finish"] not in inventory.FINISHES:
            raise HTTPException(400, "unknown finish")
        item.finish = payload["finish"]
    if "sub_type" in payload:
        declared = (payload["sub_type"] or "").strip()
        if declared:
            # Only a printing this card actually has. Free text here would let
            # a typo silently price the card off the guess forever, looking for
            # all the world like a deliberate choice.
            known = {
                row.sub_type
                for row in session.scalars(
                    select(db.CardPrice).where(db.CardPrice.card_id == item.card_id)
                )
            }
            if declared not in known:
                raise HTTPException(400, "no such printing for this card")
            item.sub_type = declared
            # Keep the coarse field consistent, so bulk views and CSV columns
            # that speak foil/non-foil do not contradict the precise one.
            item.finish = "foil" if "foil" in declared.lower() else "nonfoil"
        else:
            item.sub_type = None
    for field in ("cost", "sold_price"):
        if field in payload:
            raw = payload[field]
            if raw in (None, ""):
                setattr(item, field, None)
            else:
                try:
                    setattr(item, field, max(0.0, float(raw)))
                except (TypeError, ValueError):
                    raise HTTPException(400, f"{field} must be a number") from None
    if "notes" in payload:
        item.notes = (payload["notes"] or "").strip()[:2000] or None

    if "status" in payload:
        status = payload["status"]
        if status not in inventory.STATUSES:
            raise HTTPException(400, "unknown status")
        if status == "sold" and item.status != "sold":
            item.status = "sold"
            item.sold_at = dt.datetime.now(dt.UTC)
        elif status == "stock" and item.status != "stock":
            # Unsold: clear the sale so realised profit does not keep counting
            # a card that is back on the shelf.
            item.status = "stock"
            item.sold_at = None
            item.sold_price = None

    session.commit()
    joblog.add(user.id, f"updated {inventory.sku(item.id)} · {item.condition} {item.finish}")
    return {"ok": True}


@router.delete("/api/inventory/{item_id}")
def api_inventory_delete(
    item_id: int,
    session=Depends(db_session),
    user: db.User = Depends(api_owner),
):
    """Remove a row outright.

    For a card that should never have been here — a misidentified scan
    committed by mistake. A card that *sold* should be marked sold instead:
    deleting it takes its cost basis with it and makes realised profit wrong
    forever, which is why the panel puts the two actions far apart.
    """
    item = session.get(db.InventoryItem, item_id)
    if item is None or item.user_id != user.id:
        raise HTTPException(404, "no such row")
    label = inventory.sku(item.id)
    _delete_items(session, [item])
    session.commit()
    joblog.add(user.id, f"deleted {label} from inventory")
    return {"ok": True}


def _delete_items(session, items: list[db.InventoryItem]) -> None:
    """Remove rows, and stop their scans claiming to be in inventory.

    A scan whose inventory row is gone is not `confirmed` any more — leaving it
    that way puts the card in neither the queue nor the inventory, visible
    nowhere, while the database says it was accepted. Marking it discarded is
    simply the truth, and it keeps the review counts honest.
    """
    scan_ids = [i.scan_id for i in items if i.scan_id]
    for item in items:
        session.delete(item)
    session.flush()
    if scan_ids:
        for scan in session.scalars(select(db.Scan).where(db.Scan.id.in_(scan_ids))):
            scan.status = "discarded"

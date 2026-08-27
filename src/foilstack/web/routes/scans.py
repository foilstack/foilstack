"""Getting scans in, and deciding what they are.

The import screen and everything the review queue does to a scan: uploading an
archive, watching the job, and confirming, correcting or discarding what comes
back. Grouped by the thing they act on rather than by whether they render HTML
or return JSON — a route that confirms a scan and the screen that offers the
button belong together, and splitting them by response type would put the two
halves of one decision in different files.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import warnings
import zipfile
from pathlib import Path

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from foilstack import db, importing, inventory, search
from foilstack.config import Settings
from foilstack.importing import run_import
from foilstack.web import joblog
from foilstack.web.chrome import _chrome, _money, templates
from foilstack.web.deps import api_owner, db_session, owner, settings_dep

logger = logging.getLogger(__name__)
router = APIRouter()


def _queue_rows(session, user_id: int) -> list[dict]:
    """The match queue: scans still waiting on a decision.

    Confirmed scans are deliberately absent. The import screen is where cards
    arrive and where you decide about them; once decided they are inventory,
    and a queue that keeps showing them has no end state — you commit and the
    page looks exactly as it did.

    Auto-accepted scans therefore never appear here either, which costs the
    audit trail this list used to carry. That moved rather than vanished: an
    inventory row created without a human looking at it wears an `auto` badge,
    which is a better place for it anyway — it is visible for the life of the
    card rather than only until the next import.
    """
    scans = session.scalars(
        select(db.Scan)
        # Every row reads its job's import defaults, and four hundred rows
        # asking for them one at a time is four hundred queries.
        .options(selectinload(db.Scan.job))
        .where(
            db.Scan.user_id == user_id,
            db.Scan.status.in_(("pending", "unmatched", "error")),
        )
        .order_by(db.Scan.id.desc())
        .limit(400)
    ).all()

    priced = inventory._prices_for(
        session,
        {c.card_id for s in scans for c in s.candidates}
        | {s.chosen_card_id for s in scans if s.chosen_card_id},
    )
    rows = []
    for scan in scans:
        top = scan.candidates[0] if scan.candidates else None
        needs_review = True  # nothing else is in this list any more
        alt = ""
        if scan.status == "error":
            alt = scan.error or "could not read this image"
        elif top is None:
            alt = "no match in the catalogue. is this game ingested?"
        elif len(scan.candidates) > 1:
            runner = scan.candidates[1]
            alt = (
                f"also matched: {runner.card.name}"
                f"{' (' + runner.card.variant + ')' if runner.card.variant else ''}"
                f" {runner.score * 100:.0f}%"
            )
        elif top.score < 0.90:
            # Said only when it is true. Every row in this queue needs a
            # decision, so hanging "low confidence" on all of them — including
            # a 99% match — trains the reader to ignore the one row where it
            # means something.
            alt = "low confidence. check the printing before committing"

        # A person's choice outranks the encoder's ranking, and survives a
        # reload because it is a column rather than a state the browser is
        # holding. `chosen_card` may be None despite an id if the catalogue
        # was re-ingested underneath it, which falls back to the guesses.
        chosen = scan.chosen_card if scan.chosen_card_id else None
        card = chosen or (top.card if top else None)
        if chosen is not None:
            # What was overruled, kept in view. Useful when a seller returns to
            # a row later and wants to know whether they changed it on purpose.
            alt = (
                f"encoder said: {top.card.name} {top.score * 100:.0f}%"
                if top is not None and top.card_id != chosen.id
                else ""
            )
        rows.append(
            {
                "scan_id": scan.id,
                "filename": scan.filename,
                "needs_review": needs_review,
                "chosen": chosen is not None,
                "card_id": card.id if card else None,
                "name": card.name if card else "Unidentified",
                "image_url": card.image_url if card else None,
                "market": (card.market or 0.0) if card else 0.0,
                # What this card costs in each printing, so the queue can show the
                # price for the finish that is actually selected. Before this the
                # row showed the plain printing's price whatever the toggle said.
                "prices": {
                    name: row.market
                    for name, row in (priced.get(card.id, {}) if card else {}).items()
                    if row.market is not None
                },
                "meta": " · ".join(
                    p
                    for p in [
                        card.game if card else None,
                        card.set_name if card else None,
                        f"#{card.number}" if card and card.number else None,
                        _money(card.market) if card and card.market is not None else None,
                    ]
                    if p
                )
                if card
                else scan.filename,
                "alt": alt,
                "score": top.score if top else 0.0,
                "pct": f"{(top.score if top else 0) * 100:.0f}%",
                "ambiguous": card is not None
                and len([n for n in priced.get(card.id, {}) if "foil" in n.lower()]) > 1,
                # Every runner-up, not just the first. The encoder stores five
                # and the queue used to offer one, so a scan whose right answer
                # sat at rank three was indistinguishable from one with no
                # right answer at all.
                "alternates": [
                    {
                        "card_id": c.card.id,
                        "name": c.card.name,
                        "variant": c.card.variant or "",
                        "set_name": c.card.set_name or "",
                        "game": c.card.game,
                        "pct": f"{c.score * 100:.0f}%",
                    }
                    # Once a person has chosen, the encoder's top guess is a
                    # runner-up like any other — it has to stay reachable, or
                    # correcting a good match by mistake is a one-way door.
                    for c in (scan.candidates if chosen else scan.candidates[1:])
                ],
                # What the top match calls itself, as the opening query for the
                # search box. A wrong match is usually wrong about the printing
                # rather than the name, so the search starts one edit away from
                # the answer instead of empty.
                "query": card.name if card else "",
                "game": card.game if card else "",
                # The defaults this scan was imported under. Read off the job
                # rather than off the settings panel, because the panel is
                # aimed at the next import and this queue outlives the one
                # that filled it — two batches waiting together, one graded
                # NM and one DMG, each want their own answer.
                #
                # Only the starting position. The per-row chips still decide
                # what gets committed, so a default that is right for most of
                # a batch costs nothing on the cards it is wrong for.
                "condition": scan.job.default_condition or "NM",
                "finish": scan.job.default_finish or "nonfoil",
            }
        )
    return rows


@router.get("/app", response_class=HTMLResponse)
def page_import(
    request: Request,
    filter: str = "all",
    session=Depends(db_session),
    user: db.User = Depends(owner),
    settings: Settings = Depends(settings_dep),
):
    jobs = session.scalars(
        select(db.ImportJob)
        .where(db.ImportJob.user_id == user.id)
        .order_by(db.ImportJob.id.desc())
        .limit(6)
    ).all()
    active = next((j for j in jobs if j.status in ("pending", "matching")), None)

    everything = _queue_rows(session, user.id)
    counts = {
        "all": len(everything),
        "matched": sum(1 for r in everything if r["card_id"]),
    }
    counts["nomatch"] = counts["all"] - counts["matched"]
    if filter == "matched":
        rows = [r for r in everything if r["card_id"]]
    elif filter == "nomatch":
        rows = [r for r in everything if not r["card_id"]]
    else:
        rows = everything

    return templates.TemplateResponse(
        request,
        "import.html",
        {
            "nav": "import",
            "jobs": jobs,
            "active": active,
            "rows": rows,
            "counts": counts,
            "filter": filter,
            "queue_value": sum(r["market"] for r in rows),
            "auto_accept": settings.auto_accept,
            "thresholds": [0.88, 0.92, 0.96],
            "max_images": importing.MAX_IMAGES,
            "max_mb": settings.max_archive_mb,
            "conditions": inventory.CONDITIONS,
            **_chrome(session, request, user, settings),
        },
    )


@router.get("/matches")
def page_matches():
    """The review queue lives on the import screen now, beside what produced
    it. Kept as a redirect because it was a bookmarkable URL."""
    return RedirectResponse("/app?filter=review", status_code=307)


@router.post("/api/import")
async def api_import(
    background: BackgroundTasks,
    archive: list[UploadFile] = File(...),
    auto_accept: float = Form(None),
    default_condition: str = Form("NM"),
    default_finish: str = Form("nonfoil"),
    session=Depends(db_session),
    user: db.User = Depends(api_owner),
    settings: Settings = Depends(settings_dep),
):
    uploads = [f for f in archive if (f.filename or "").strip()]
    if not uploads:
        raise HTTPException(400, "no file uploaded")
    if default_condition not in inventory.CONDITIONS:
        raise HTTPException(400, "unknown condition")
    if default_finish not in inventory.FINISHES:
        raise HTTPException(400, "unknown finish")

    # Either one archive, or loose images that get packed into one below.
    # Anything else — two archives, or an archive alongside images — is a
    # muddle worth refusing rather than guessing at.
    suffixes = {Path(f.filename or "").suffix.lower() for f in uploads}
    single_archive = len(uploads) == 1 and suffixes == {".zip"}
    if not single_archive and not suffixes <= importing.IMAGE_SUFFIXES:
        kinds = ", ".join(sorted(s.lstrip(".") for s in importing.IMAGE_SUFFIXES))
        raise HTTPException(400, f"expected one .zip archive, or images ({kinds})")
    if len(uploads) > importing.MAX_IMAGES:
        raise HTTPException(400, f"at most {importing.MAX_IMAGES:,} images per upload")

    # Checked before a byte is written, and again against what the archive
    # actually contains once its size is known. A quota tested only after the
    # upload has landed is a quota that still lets the disk fill.
    if settings.max_account_mb:
        used = importing.usage_bytes(session, user.id)
        ceiling = settings.max_account_mb * 1024 * 1024
        if used >= ceiling:
            raise HTTPException(
                413,
                f"this account is using {used / 1048576:.0f} MB of its "
                f"{settings.max_account_mb} MB limit. discard some scans first",
            )

    tmp = Path(tempfile.mkdtemp(prefix="foilstack-")) / "upload.zip"
    limit = settings.max_archive_mb * 1024 * 1024
    if settings.max_account_mb:
        limit = min(limit, ceiling - used)

    async def drain(upload: UploadFile, write, size: int) -> int:
        """Copy one upload through `write`, counting every byte against `limit`."""
        while chunk := await upload.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                raise HTTPException(
                    413,
                    f"upload exceeds {limit / 1048576:.0f} MB "
                    "(the per-upload cap, or what is left of this account's quota)",
                )
            write(chunk)
        return size

    try:
        if single_archive:
            with open(tmp, "wb") as out:
                size = await drain(uploads[0], out.write, 0)
        else:
            # Loose images are packed into an archive at the door, so that one
            # code path serves both. The traversal checks, the expansion
            # ceiling, the duplicate-name suffixing and the image cap all live
            # in extract_archive, and none of them need a second implementation
            # for an image that arrived on its own. Stored rather than
            # deflated: a JPEG does not compress, so the CPU would buy nothing.
            size = 0
            with warnings.catch_warnings(), zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as zf:
                # Two phone folders both holding IMG_0001.jpg is ordinary, not a
                # mistake worth warning about: extract_archive suffixes the
                # second one on the way back out, so both scans survive.
                warnings.filterwarnings("ignore", "Duplicate name", UserWarning)
                for n, upload in enumerate(uploads, 1):
                    name = Path(upload.filename or f"scan-{n}").name
                    with zf.open(name, "w") as entry:
                        size = await drain(upload, entry.write, size)
    except HTTPException:
        shutil.rmtree(tmp.parent, ignore_errors=True)
        raise

    if single_archive:
        filename = uploads[0].filename or "archive.zip"
    elif len(uploads) == 1:
        filename = uploads[0].filename or "scan"
    else:
        filename = f"{len(uploads)} images"

    job = db.ImportJob(
        user_id=user.id,
        filename=filename,
        status="pending",
        auto_accept=auto_accept,
        default_condition=default_condition,
        default_finish=default_finish,
    )
    session.add(job)
    session.commit()

    joblog.add(user.id, f"POST /imports · {filename} · {size / 1048576:.1f} MB")
    background.add_task(run_import, job.id, tmp, settings)
    return {"job_id": job.id}


@router.get("/api/jobs/{job_id}")
def api_job(
    job_id: int,
    session=Depends(db_session),
    user: db.User = Depends(api_owner),
):
    job = session.get(db.ImportJob, job_id)
    # 404 rather than 403 for someone else's job: a distinguishable "forbidden"
    # confirms the id exists, which is the one bit this endpoint should not leak.
    if job is None or job.user_id != user.id:
        raise HTTPException(404, "no such job")
    return {
        "id": job.id,
        "status": job.status,
        "total": job.total,
        "processed": job.processed,
        "message": job.message,
    }


@router.get("/api/scans/{scan_id}/match-panel", response_class=HTMLResponse)
def api_match_panel(
    scan_id: int,
    request: Request,
    session=Depends(db_session),
    user: db.User = Depends(owner),
):
    """ "This is the wrong card" — the panel that lets a seller say so.

    Nearest-neighbour search answers with the five cards whose artwork is
    closest. When it is wrong about all five there was previously nothing left
    to click: confirm something untrue, or discard the scan and lose the card.
    """
    scan = session.get(db.Scan, scan_id)
    if scan is None or scan.user_id != user.id:
        raise HTTPException(404, "no such scan")
    top = scan.candidates[0].card if scan.candidates else None
    return templates.TemplateResponse(
        request,
        "_match_panel.html",
        {
            "options": [
                {
                    "card_id": c.card.id,
                    "name": c.card.name,
                    "variant": c.card.variant or "",
                    "game": c.card.game,
                    "set_name": c.card.set_name or "",
                    "number": c.card.number or "",
                    "note": f"{c.score * 100:.0f}%",
                }
                for c in scan.candidates[1:]
            ],
            "empty": "",
            "games": search.games(session),
            # The search starts on the top match's name and game rather than
            # empty. A wrong match is usually wrong about the printing, not the
            # name, so this opens one click from the answer.
            "query": top.name if top else "",
            "game": top.game if top else "",
        },
    )


@router.get("/api/cards/search", response_class=HTMLResponse)
def api_card_search(
    request: Request,
    q: str = "",
    game: str = "",
    session=Depends(db_session),
    user: db.User = Depends(owner),
):
    """Search the catalogue by name.

    Returns the same fragment the match panel draws its runners-up with, so a
    searched result and a suggested one are the same object on screen and in
    the code.

    Authenticated, though the catalogue is public reference data shared across
    accounts. Not because the rows are secret but because an unauthenticated
    substring search over every card is a free table scan for anyone who finds
    the URL.
    """
    results = search.by_name(session, q, game=game)
    return templates.TemplateResponse(
        request,
        "_match_options.html",
        {
            "options": [{**r, "note": _money(r["market"]) if r["market"] else ""} for r in results],
            "empty": "no card by that name" if (q or "").strip() else "type a card name",
        },
    )


@router.post("/api/scans/{scan_id}/choose")
def api_choose(
    scan_id: int,
    card_id: int = Form(...),
    session=Depends(db_session),
    user: db.User = Depends(api_owner),
):
    """Record which card a person says this scan is, without committing it.

    Deliberately not the same thing as confirming. Confirming creates an
    inventory row; this only says what the card *is*, so the seller can still
    set condition and finish, look at it again, and change their mind — and
    find their correction still there after a reload, which is the part that
    made this a column rather than a variable in the browser.
    """
    scan = session.get(db.Scan, scan_id)
    if scan is None or scan.user_id != user.id:
        raise HTTPException(404, "no such scan")
    card = session.get(db.Card, card_id)
    if card is None:
        raise HTTPException(400, "no such card")
    scan.chosen_card_id = card.id
    session.commit()
    return {"ok": True, "card_id": card.id}


def _confirm(
    session, user_id: int, scan_id: int, card_id: int, condition: str, finish: str = "nonfoil"
) -> None:
    scan = session.get(db.Scan, scan_id)
    if scan is None or scan.user_id != user_id:
        raise HTTPException(404, "no such scan")
    if session.get(db.Card, card_id) is None:
        raise HTTPException(400, "no such card")
    scan.status = "confirmed"
    session.add(
        db.InventoryItem(
            user_id=user_id,
            card_id=card_id,
            scan_id=scan.id,
            condition=condition,
            finish=finish,
        )
    )


@router.post("/api/scans/{scan_id}/confirm")
def api_confirm(
    scan_id: int,
    card_id: int = Form(...),
    condition: str = Form("NM"),
    finish: str = Form("nonfoil"),
    session=Depends(db_session),
    user: db.User = Depends(api_owner),
):
    if condition not in inventory.CONDITIONS:
        raise HTTPException(400, "unknown condition")
    if finish not in inventory.FINISHES:
        raise HTTPException(400, "unknown finish")
    _confirm(session, user.id, scan_id, card_id, condition, finish)
    session.commit()
    joblog.add(user.id, f"confirmed scan {scan_id} · {condition} {finish}")
    return {"ok": True}


@router.post("/api/scans/commit")
async def api_commit(
    request: Request,
    session=Depends(db_session),
    user: db.User = Depends(api_owner),
):
    """Commit the whole queue in one go.

    The unreviewed count comes back in the response rather than being blocked:
    committing a queue with low-confidence rows in it is a decision the seller
    is allowed to make, but not one they should make without being told.
    """
    payload = await request.json()
    rows = payload.get("rows") or []
    if not rows:
        raise HTTPException(400, "nothing to commit")
    for row in rows:
        condition = row.get("condition", "NM")
        finish = row.get("finish", "nonfoil")
        if condition not in inventory.CONDITIONS:
            raise HTTPException(400, "unknown condition")
        if finish not in inventory.FINISHES:
            raise HTTPException(400, "unknown finish")
        _confirm(
            session,
            user.id,
            int(row["scan_id"]),
            int(row["card_id"]),
            condition,
            finish,
        )
    session.commit()
    guessed = sum(
        1 for r in inventory.items(session, user.id, status="stock") if r["printing_guessed"]
    )
    joblog.add(user.id, f"committed {len(rows)} scans to inventory")
    # Told, not buried. A card priced on a guess between printings looks
    # exactly like a card priced on a decision.
    return {"ok": True, "committed": len(rows), "needs_printing": guessed}


@router.post("/api/scans/{scan_id}/discard")
def api_discard(
    scan_id: int,
    session=Depends(db_session),
    user: db.User = Depends(api_owner),
):
    scan = session.get(db.Scan, scan_id)
    if scan is None or scan.user_id != user.id:
        raise HTTPException(404, "no such scan")
    scan.status = "discarded"
    session.commit()
    return {"ok": True}


@router.post("/api/scans/discard-all")
async def api_discard_all(
    request: Request,
    session=Depends(db_session),
    user: db.User = Depends(api_owner),
):
    payload = await request.json()
    ids = [int(i) for i in (payload.get("scan_ids") or [])]
    if not ids:
        raise HTTPException(400, "nothing to discard")
    scans = session.scalars(
        select(db.Scan).where(db.Scan.id.in_(ids), db.Scan.user_id == user.id)
    ).all()
    for scan in scans:
        if scan.status != "confirmed":
            scan.status = "discarded"
    session.commit()
    joblog.add(user.id, f"discarded {len(scans)} unreviewed matches")
    return {"ok": True}

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
from foilstack.importing import ACTIVE_STATUSES, run_import
from foilstack.web import joblog
from foilstack.web.chrome import _chrome, _money, templates
from foilstack.web.deps import api_owner, db_session, owner, settings_dep

logger = logging.getLogger(__name__)
router = APIRouter()


def _price_map(printings: dict) -> dict[str, float]:
    """Per-printing market prices keyed by sub-type, with the unpriced dropped."""
    return {name: row.market for name, row in printings.items() if row.market is not None}


def _card_meta(card) -> str:
    """The line under a card's name in the queue: game, set, number, price.

    Shared with `api_choose`, which has to reproduce it exactly. Correcting a
    match rewrites this line in the browser, and building the string in two
    places is how the corrected row came to be the one row on the page with no
    price under it.
    """
    return " · ".join(
        part
        for part in [
            card.game,
            card.set_name,
            f"#{card.number}" if card.number else None,
            _money(card.market) if card.market is not None else None,
        ]
        if part
    )


def _cohort_label(job: db.ImportJob) -> str:
    """What a job's batch settled on, for a row that had to be moved to it.

    Reads off the job rather than being recomputed, so the queue and the import
    screen's status tooltip cannot disagree — and so the label survives the
    next import, which will settle on something else.
    """
    parts = [job.cohort_game or "", (job.cohort_set or "") if job.same_set else ""]
    label = " · ".join(part for part in parts if part)
    return f"batch is {label}" if label else "moved to match the batch"


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
        | {s.chosen_card_id for s in scans if s.chosen_card_id}
        | {s.cohort_card_id for s in scans if s.cohort_card_id},
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

        # Three claims about one scan, in order of who made them: a person, then
        # the batch, then the encoder. Each survives a reload because each is a
        # column rather than a state the browser is holding, and either card may
        # be None despite an id if the catalogue was re-ingested underneath it —
        # which falls back to the next claim down.
        chosen = scan.chosen_card if scan.chosen_card_id else None
        cohort = scan.cohort_card if (chosen is None and scan.cohort_card_id) else None
        card = chosen or cohort or (top.card if top else None)
        if chosen is not None:
            # What was overruled, kept in view. Useful when a seller returns to
            # a row later and wants to know whether they changed it on purpose.
            alt = (
                f"encoder said: {top.card.name} {top.score * 100:.0f}%"
                if top is not None and top.card_id != chosen.id
                else ""
            )
        elif cohort is not None:
            # Which batch, not what the batch is: the section heading above
            # this row already names the cohort, and repeating it here spent
            # the first thirty characters of a line that gets ellipsised
            # saying what the reader had just read — pushing the one thing
            # only this row knows, the match it was moved off, off the end.
            alt = "moved to the batch"
            if top is not None and top.card_id != cohort.id:
                alt += f" · encoder said: {top.card.name} {top.score * 100:.0f}%"
        shown = next((c for c in scan.candidates if card and c.card_id == card.id), None)
        prices = _price_map(priced.get(card.id, {})) if card else {}
        default_finish = scan.job.default_finish or "nonfoil"
        rows.append(
            {
                "scan_id": scan.id,
                "filename": scan.filename,
                # The upload this scan arrived in, which is what the queue
                # groups by. Keyed by the job rather than by its name: the same
                # archive sent twice is two uploads, and folding them together
                # would drop cards from an older batch in among the ones that
                # just landed.
                "job_id": scan.job_id,
                "job_filename": scan.job.filename,
                # Two uploads can carry the same name — "3 images" is what any
                # loose batch is called — so the section heading needs the
                # time to tell them apart.
                "job_at": scan.job.created_at,
                "job_cohort": _cohort_label(scan.job) if scan.job.cohort_game else "",
                "needs_review": needs_review,
                "chosen": chosen is not None,
                # Not folded into `chosen`. The two look alike on the row — both
                # overrule the encoder's ranking and both suppress the one-click
                # swap — but only one of them is a person, and the badge that
                # says "you picked this card" must not appear over a decision
                # nobody made.
                "cohort": cohort is not None,
                "card_id": card.id if card else None,
                "name": card.name if card else "Unidentified",
                "image_url": card.image_url if card else None,
                "market": (card.market or 0.0) if card else 0.0,
                # What this card costs in each printing, so the queue can show the
                # price for the finish that is actually selected. Before this the
                # row showed the plain printing's price whatever the toggle said.
                "prices": prices,
                "meta": _card_meta(card) if card else scan.filename,
                "alt": alt,
                # The score of the card this row is *showing*, which is not
                # always the encoder's best: a row the batch moved is pointing
                # at a candidate further down the list, and captioning it with
                # rank zero's score would claim a confidence for a card that
                # never earned it. Falls back to the top score when the shown
                # card has no candidate at all, which is a card somebody found
                # by search rather than one the encoder proposed.
                "score": shown.score if shown else (top.score if top else 0.0),
                "pct": f"{(shown.score if shown else (top.score if top else 0)) * 100:.0f}%",
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
                    # What comes out of the list is whatever the row is
                    # already showing — usually rank zero, but the chosen card
                    # or the one the batch reached down for when there is one.
                    # Everything else stays, including the encoder's top guess
                    # once something has overruled it: it has to remain
                    # reachable, or correcting a good match by mistake is a
                    # one-way door.
                    for c in scan.candidates
                    if c is not shown
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
                # The batch's answer, and this row's. They differ when the
                # card the scan matched is priced on one side of the foil
                # line only: the default is abandoned rather than flagged,
                # because there is no other honest finish for that card. See
                # `inventory.resolve_finish`.
                #
                # Both are on the row because the queue needs them both — the
                # default is what a re-pointed row re-resolves against, and a
                # card corrected from a foil-only match to an ordinary one
                # should go back to what the batch asked for.
                "default_finish": default_finish,
                "finish": inventory.resolve_finish(default_finish, list(prices)),
            }
        )

    # The order the scans arrived in, which is the order they were in the
    # archive. `run_import` writes one row per file and commits as it goes, so
    # within a job the ids are the import order exactly — no column needed to
    # record what the sequence already says.
    #
    # This replaced dearest-first. Ordering by value put the cards worth
    # getting right at the top, which reads well as an argument and badly as a
    # tool: a seller confirming a batch has the physical stack in their hand,
    # in the order they photographed it, and a queue in any other order makes
    # them hunt for each card instead of working down the pile. Value ordering
    # optimised the part of the job that gets abandoned; matching the stack
    # means less of it gets abandoned.
    #
    # Ascending, so the queue reads the way the pile does — first card
    # scanned, first card decided.
    #
    # Note this reorders the 400 rows the query returned, which are the newest
    # 400. A queue longer than that was already only partly visible; this does
    # not change what is on the page, only the order of it.
    #
    # `_group_rows` splits this list without re-sorting it, so this is also the
    # order inside each upload's section.
    rows.sort(key=lambda r: r["scan_id"])
    return rows


def _group_rows(rows: list[dict]) -> list[dict]:
    """The queue split into one section per upload, earliest upload first.

    Grouping is a partition of the list it is given, not a second sort: the
    rows arrive dearest-first from `_queue_rows` and keep that order inside
    each section. Sorting here instead would mean the queue's ordering rule
    lived in two places, and the two would drift.

    Sections run oldest first, so the queue is worked in the order the batches
    arrived and the backlog drains from the front. Newest-first read well on
    the batch you had just dropped and badly on every one behind it: the
    oldest cards sank further with each import and were the ones a part-way
    review never reached.

    That does put the archive you just uploaded at the bottom, which is what
    the collapsible sections are for — fold the batches you are done with and
    the new one comes up to meet you.
    """
    groups: dict[int, dict] = {}
    for row in rows:
        group = groups.get(row["job_id"])
        if group is None:
            group = groups[row["job_id"]] = {
                "job_id": row["job_id"],
                "filename": row["job_filename"],
                "at": row["job_at"],
                # What the batch was held to, on the heading of the batch it
                # was held to. A rule applied to every row in a section is a
                # property of the section, and repeating it on four hundred
                # rows would say it four hundred times and explain it none.
                "cohort": row["job_cohort"],
                "rows": [],
                "value": 0.0,
            }
        group["rows"].append(row)
        # The same figure the bar totals for the whole queue, per upload. Left
        # as market rather than the finish-aware price the rows show, so the
        # two numbers on the screen are the same measure.
        group["value"] += row["market"]
    return sorted(groups.values(), key=lambda g: g["job_id"])


# Named per account below, because a cookie belongs to the origin: one browser
# can sign into several accounts on a multi-user install, and a job id is a row
# number, so an unkeyed cookie would fold account B's queue to match account A's.
FOLDED_COOKIE = "foilstack_folded"

# A seller who folds every batch for a year would otherwise grow this without
# limit, and an oversized cookie is not a slow request but a rejected one. The
# browser trims to the same number from the same end.
FOLDED_MAX = 200


def _folded_jobs(request: Request, user_id: int) -> set[int]:
    """Which upload sections this browser has folded shut.

    A cookie rather than localStorage, which is where this started and could
    not work: localStorage is only readable once the page has parsed, so the
    queue rendered expanded and then snapped shut when the script caught up.
    Measured at 56ms on this machine and 123ms with the CPU throttled six
    times — not a subliminal frame but a visible jolt, on every single load,
    on the one screen where folding is part of the workflow.

    Sent with the request, the answer is known at render time and the markup is
    right the first time, so there is nothing to correct and nothing to see.

    Anything unparseable is ignored rather than raised on. This value is edited
    by a browser and survives in one for a year; a stale or hand-mangled cookie
    has to cost a fold, not the page.
    """
    raw = request.cookies.get(f"{FOLDED_COOKIE}_{user_id}", "")
    return {int(part) for part in raw.split(",")[:FOLDED_MAX] if part.isdigit()}


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
    active = next((j for j in jobs if j.status in ACTIVE_STATUSES), None)

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
            "groups": _group_rows(rows),
            "folded": _folded_jobs(request, user.id),
            # Shared with the browser so the two trim the cookie to the same
            # length from the same end, rather than disagreeing about which
            # folds survived.
            "folded_max": FOLDED_MAX,
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
    same_game: bool = Form(False),
    same_set: bool = Form(False),
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
    # A set belongs to exactly one game, so "same set" already says "same
    # game" and the pair `(false, true)` describes nothing. Widened rather
    # than rejected: the browser ties the two checkboxes together, and a
    # client that did not is asking for the stricter of the two.
    same_game = same_game or same_set

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
        same_game=same_game,
        same_set=same_set,
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

    # What comes out of the list is whatever the row is *showing*, which is not
    # always rank zero: a row a person corrected, or one the batch moved, is
    # pointing somewhere else, and dropping rank zero regardless both offers
    # the current answer back as an alternative to itself and takes away the
    # only route back to the encoder's. The queue's inline runners-up already
    # work this way; these two lists have to agree, or "wrong card?" opens on a
    # different set of options than the row it opened from.
    shown_id = scan.chosen_card_id or scan.cohort_card_id
    if shown_id is None:
        shown_id = scan.candidates[0].card_id if scan.candidates else None
    shown = next(
        (c.card for c in scan.candidates if c.card_id == shown_id),
        scan.chosen_card if scan.chosen_card_id else None,
    )
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
                for c in scan.candidates
                if c.card_id != shown_id
            ],
            "empty": "",
            "games": search.games(session),
            # The search starts on the shown card's name and game rather than
            # empty. A wrong match is usually wrong about the printing, not the
            # name, so this opens one click from the answer.
            "query": shown.name if shown else "",
            "game": shown.game if shown else "",
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
    # The prices and the meta line for the card that was just picked. The row
    # rewrites both in the browser, and it has nowhere else to get them: the
    # panel it was picked from lists names and sets, not per-printing prices.
    # Without these the corrected row lost its price and did not get it back
    # until it was committed and had become inventory — the one row on the
    # screen with no number on it being, of course, the one a person had just
    # taken a deliberate interest in.
    return {
        "ok": True,
        "card_id": card.id,
        "prices": _price_map(inventory._prices_for(session, {card.id}).get(card.id, {})),
        "meta": _card_meta(card),
    }


def _confirm(
    session, user_id: int, scan_id: int, card_id: int, condition: str, finish: str = "nonfoil"
) -> bool:
    """Move one scan into inventory. Returns whether a row was created.

    Confirming twice must not produce two cards. One row in `inventory` is one
    physical card, and there is exactly one card behind a scan however many
    times the request arrives — so a double-click, a browser retry, a proxy
    retry, or the same `scan_id` twice in one bulk payload has to be the same
    single card, not two. It arrived that way once: nothing here looked at the
    scan's state, and every replay minted a second row that counted, priced and
    exported as a card the seller did not have.

    Two guards, because they fail differently. This one answers the ordinary
    replay, where the first confirmation is committed and visible. The unique
    index on `inventory.scan_id` answers the race this cannot see, where two
    requests read before either wrote — `FOR UPDATE` on the scan is what makes
    them queue rather than collide.
    """
    scan = session.get(db.Scan, scan_id, with_for_update=True)
    if scan is None or scan.user_id != user_id:
        raise HTTPException(404, "no such scan")
    if session.get(db.Card, card_id) is None:
        raise HTTPException(400, "no such card")

    existing = session.scalar(
        select(db.InventoryItem.id).where(db.InventoryItem.scan_id == scan.id)
    )
    if existing is not None:
        # Status and inventory disagreeing is what leaves a card visible
        # nowhere, so say `confirmed` even when this call created nothing.
        scan.status = "confirmed"
        return False

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
    return True


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
    created = _confirm(session, user.id, scan_id, card_id, condition, finish)
    session.commit()
    if created:
        joblog.add(user.id, f"confirmed scan {scan_id} · {condition} {finish}")
    return {"ok": True, "created": created}


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
    committed = 0
    # One scan is one card however many times the payload names it. The rows
    # are built from the DOM, so a row rendered twice — or a list assembled
    # across a re-render — sends the same scan twice, and the unique index
    # would fail the whole commit rather than the duplicate.
    seen: set[int] = set()
    for row in rows:
        condition = row.get("condition", "NM")
        finish = row.get("finish", "nonfoil")
        if condition not in inventory.CONDITIONS:
            raise HTTPException(400, "unknown condition")
        if finish not in inventory.FINISHES:
            raise HTTPException(400, "unknown finish")
        scan_id = int(row["scan_id"])
        if scan_id in seen:
            continue
        seen.add(scan_id)
        committed += _confirm(
            session,
            user.id,
            scan_id,
            int(row["card_id"]),
            condition,
            finish,
        )
    session.commit()
    guessed = sum(
        1 for r in inventory.items(session, user.id, status="stock") if r["printing_guessed"]
    )
    joblog.add(user.id, f"committed {committed} scans to inventory")
    # Told, not buried. A card priced on a guess between printings looks
    # exactly like a card priced on a decision.
    return {"ok": True, "committed": committed, "needs_printing": guessed}


@router.post("/api/scans/{scan_id}/discard")
def api_discard(
    scan_id: int,
    session=Depends(db_session),
    user: db.User = Depends(api_owner),
    settings: Settings = Depends(settings_dep),
):
    scan = session.get(db.Scan, scan_id)
    if scan is None or scan.user_id != user.id:
        raise HTTPException(404, "no such scan")
    scan.status = "discarded"
    # The photograph goes with it, and the quota it was holding comes back.
    # Without this the only advice the 413 has for a full account — discard
    # something — moved a status and freed nothing at all.
    importing.purge_scans(session, settings, [scan])
    session.commit()
    return {"ok": True}


@router.post("/api/scans/discard-all")
async def api_discard_all(
    request: Request,
    session=Depends(db_session),
    user: db.User = Depends(api_owner),
    settings: Settings = Depends(settings_dep),
):
    payload = await request.json()
    ids = [int(i) for i in (payload.get("scan_ids") or [])]
    if not ids:
        raise HTTPException(400, "nothing to discard")
    scans = session.scalars(
        select(db.Scan).where(db.Scan.id.in_(ids), db.Scan.user_id == user.id)
    ).all()
    dropped = [scan for scan in scans if scan.status != "confirmed"]
    for scan in dropped:
        scan.status = "discarded"
    released = importing.purge_scans(session, settings, dropped)
    session.commit()
    joblog.add(
        user.id,
        f"discarded {len(dropped)} unreviewed matches · {released / 1048576:.1f} MB freed",
    )
    return {"ok": True}

"""Archive import: a .zip of card images in, reviewable matches out.

Two things here are security rather than housekeeping, and both look like
paranoia until the day they do not:

* **Path traversal.** A zip entry may be named `../../etc/whatever`. Python
  will happily write it. Every destination is resolved and checked to be inside
  the extraction directory before a single byte is written.
* **Decompression ratio.** A few kilobytes of zip can expand to gigabytes.
  Entries are rejected on declared size, and the running total is capped.

The matcher itself is embeddings only. Reprints share artwork, so the vector
gives a name you can trust and a printing you cannot — which is why the top
match is presented next to its rivals rather than as an answer.

There is a second pass over the batch when the seller has said the batch is
one game or one set. Each scan is matched alone, but a stack of cards is not a
set of unrelated images, and what the other four hundred scans agree on is
evidence about this one. See `apply_cohort`.

Everything written here carries the owner of the job that produced it. The
catalogue is shared; scans and inventory are not.
"""

from __future__ import annotations

import asyncio
import logging
import os
import zipfile
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Any, NamedTuple

from sqlalchemy import select

from foilstack import db, images, inventory, search
from foilstack.config import Settings
from foilstack.embedding import EmbedderError, embed_image

logger = logging.getLogger(__name__)

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
MAX_ENTRY_BYTES = 40 * 1024 * 1024
# How many images one archive may contain. Every image costs an encoder pass,
# so an unbounded archive is an unbounded amount of someone else's compute —
# which matters the moment this is reachable from the internet rather than
# from the laptop it is running on.
MAX_IMAGES = int(os.getenv("FOILSTACK_MAX_IMAGES", "5000"))
MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024

# The statuses a job is in while something is still working on it. Shared with
# the import screen rather than spelled out beside it: the screen's own copy
# was missing `grouping`, so a job killed during the cohort pass disappeared
# from it without a word while one killed during matching hung it forever —
# two different wrong answers to the same event. One tuple, and a fourth
# status cannot be taught to only half the readers.
ACTIVE_STATUSES = ("pending", "matching", "grouping")

CANDIDATE_COUNT = 5
# How deep the search goes when the batch has been declared one game or one
# set. Five is the right number to *show* — past that the runners-up are noise
# a person has to read — but it is the wrong number to hold an election on:
# `_cohort_votes` scores a cohort by how widely it appears across the batch,
# and a set that is present in every scan's candidates while topping few of
# them is invisible at five. This is no longer how a stray scan is *rescued* —
# `search.search_within` does that, and does it without a depth limit at all —
# so twenty-five is now purely the width of the ballot.
COHORT_CANDIDATE_COUNT = 25

# How far two printings' prices may diverge before a match between them has to
# be reviewed. 20% of the dearer one, and never mind a gap under a dollar —
# below that the review costs more attention than the error costs money.
PRICE_GAP_TOLERANCE = 0.20
MIN_ABSOLUTE_GAP = 1.00


class ImportError_(RuntimeError):
    pass


class Pooled(NamedTuple):
    """What one scan contributes to the batch pass, held until the batch ends.

    The ranking is what the cohort is elected from. The vector is what a scan
    that lost the election gets re-searched with, and keeping it is what lets
    the second pass ask a question the first one could not: not "how far down
    this ranking is something from the batch's set" but "what is the best card
    *in* that set". Re-encoding to recover it would be an extra encoder pass
    per stray scan, which is the expensive half of an import.

    4 KB a scan, so 20 MB at the 5000-image ceiling, and it goes when the job
    does.
    """

    hits: list[tuple[int, float]]
    vector: Any


def scan_path(stored_path: str, scans_dir: Path) -> Path | None:
    """Where a scan's image actually is, now.

    Scans are stored as a path *relative* to the scans directory, and resolved
    against it on the way back out. An absolute path is a property of whichever
    process happened to run the import, not of the scan: the compose file mounts
    `./data` at `/data`, so a row written by the CLI on the host records
    `/home/you/foilstack/data/scans/1/x.jpg` and the container — holding the very
    same file at `/data/scans/1/x.jpg` — cannot find it. Every thumbnail 404s and
    the queue renders with empty boxes where the scans should be.

    Rows written before that was fixed still hold an absolute path, so those are
    re-rooted under the current scans directory by their trailing
    `<job>/<filename>`. Returns None if the file is missing or resolves outside
    the scans directory, which is the same check `/scan/{id}/image` needs before
    it turns a database value into a filesystem read.
    """
    raw = Path(stored_path)
    if raw.is_absolute():
        parts = raw.parts
        target = (
            raw if raw.exists() else (scans_dir.joinpath(*parts[-2:]) if len(parts) >= 2 else None)
        )
    else:
        target = scans_dir / raw
    if target is None:
        return None

    resolved = target.resolve()
    root = scans_dir.resolve()
    if root not in resolved.parents:
        return None
    return resolved if resolved.exists() else None


def usage_bytes(session, user_id: int) -> int:
    """How much disk this account's scans occupy.

    Summed from the rows rather than measured on the filesystem: the answer has
    to be available before an upload is accepted, and walking a directory tree
    on every import is a cost that grows with the size of the thing it is
    protecting.

    It falls when a scan is discarded because `purge_scans` deletes the files
    and zeroes `size_bytes` in the same breath. That used to be a claim in this
    docstring and nowhere else: discarding only moved a status, the sum never
    moved, and the 413 telling a seller to "discard some scans first" asked
    them to do something that could not work.
    """
    from sqlalchemy import func, select

    total = session.scalar(
        select(func.coalesce(func.sum(db.Scan.size_bytes), 0)).where(db.Scan.user_id == user_id)
    )
    return int(total or 0)


def purge_scans(session, settings: Settings, scans: list[db.Scan]) -> int:
    """Delete the images behind discarded scans. Returns the bytes released.

    The row stays and the files go. Those are separable, and they answer
    different questions: the row carries the candidate list — what the encoder
    saw, and how close the runners-up were — which is the record of *why* a
    card was rejected and stays useful long after the photograph of it is not.
    The photograph is the bulk on disk and the whole of the quota.

    So `size_bytes` becomes 0 rather than the row becoming nothing, and the
    quota falls by exactly what the disk did.

    Skips any scan an inventory row still points at, whatever its status says.
    The inventory table and the card page both render that photograph, and a
    discard endpoint is not where a card someone is selling loses its picture.

    Deliberately not called when an inventory row is *deleted*, even though
    that marks its scan discarded too. Bulk delete refuses sold rows on the
    stated grounds that an in-stock row is recoverable "because its scan is on
    disk" — so deleting a row here and taking its scan with it would quietly
    withdraw the one promise that makes deleting in bulk safe. Those scans are
    reclaimed by `foilstack purge`, where an operator is asking for it.

    Missing files are not an error. A purge that has already run, a data
    directory restored without its scan mirror, and a row from before display
    copies existed all reach here with nothing to unlink, and all three are
    ordinary.
    """
    from sqlalchemy import select

    ids = [scan.id for scan in scans]
    if not ids:
        return 0

    claimed = {
        scan_id
        for scan_id in session.scalars(
            select(db.InventoryItem.scan_id).where(db.InventoryItem.scan_id.in_(ids))
        )
        if scan_id is not None
    }

    released = 0
    for scan in scans:
        if scan.id in claimed:
            continue
        # Both roots, resolved the same way the image routes resolve them:
        # `scan_path` is what refuses a stored value that points outside the
        # directory it is supposed to be relative to, and unlinking is not a
        # place to start trusting that value more than reading did.
        for root in (settings.scans_dir, settings.display_dir):
            path = scan_path(scan.stored_path, root)
            if path is None:
                continue
            try:
                path.unlink()
            except OSError as exc:
                # Worth a line and not worth failing the discard: the seller
                # asked for the scan to go away, and it has, whatever the
                # filesystem thinks about the file behind it.
                logger.warning("could not delete %s: %s", path, exc)
        released += scan.size_bytes or 0
        scan.size_bytes = 0
    return released


def extract_archive(archive_path: Path, dest: Path) -> list[Path]:
    """Unpack image entries, refusing anything that tries to escape `dest`."""
    dest.mkdir(parents=True, exist_ok=True)
    resolved_dest = dest.resolve()
    written: list[Path] = []
    total = 0

    with zipfile.ZipFile(archive_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            raw = info.filename
            # Check the *declared* name before flattening it. Taking the
            # basename would neutralise `../../evil.jpg` on its own, but
            # silently: an archive containing such an entry is hostile, and
            # quietly sanitising it throws away the only evidence of that.
            parts = PurePosixPath(raw.replace("\\", "/")).parts
            if raw.startswith("/") or ".." in parts:
                raise ImportError_(f"refusing unsafe archive entry: {raw}")

            name = Path(raw).name
            if not name or Path(raw).suffix.lower() not in IMAGE_SUFFIXES:
                continue
            if info.file_size > MAX_ENTRY_BYTES:
                logger.warning("skipping oversized entry %s", info.filename)
                continue
            total += info.file_size
            if total > MAX_TOTAL_BYTES:
                raise ImportError_("archive expands beyond the size limit")

            target = (resolved_dest / name).resolve()
            if not str(target).startswith(str(resolved_dest) + "/"):
                # Second belt. The guard above catches declared traversal; this
                # catches anything that still resolves outside the directory,
                # such as a symlinked destination.
                raise ImportError_(f"refusing unsafe archive entry: {info.filename}")

            stem, suffix = target.stem, target.suffix
            n = 1
            while target.exists():
                target = resolved_dest / f"{stem}-{n}{suffix}"
                n += 1

            with zf.open(info) as src, open(target, "wb") as out:
                out.write(src.read())
            written.append(target)
            if len(written) >= MAX_IMAGES:
                logger.warning("archive truncated at %s images", MAX_IMAGES)
                break

    # Archive order, not filename order. `zf.infolist()` is the central
    # directory, which is the order the entries were written — and for the
    # loose images this application packs itself, that is the order the browser
    # sent them, which is the order they were picked or dropped.
    #
    # It was `sorted(written)` from the first commit, with no reason recorded,
    # and sorting throws away the only signal there is about how the seller
    # meant the batch to read. They are working down a physical stack in the
    # order they photographed it, and the queue follows this list: alphabetical
    # is only the same thing when the filenames happen to be sequential.
    return written


async def run_import(job_id: int, archive_path: Path, settings: Settings) -> None:
    """Process one archive. Runs in the background; never raises into the caller."""
    session = db.session()
    job = session.get(db.ImportJob, job_id)
    if job is None:
        return

    try:
        scans_dir = settings.scans_dir / str(job_id)
        files = extract_archive(archive_path, scans_dir)
        job.total = len(files)
        job.status = "matching"
        session.commit()

        if not files:
            job.status = "done"
            job.message = "no images found in archive"
            session.commit()
            return

        if search.count(session, settings.embed_model) == 0:
            job.status = "failed"
            job.message = (
                "no catalogue vectors for this encoder. run `foilstack ingest` "
                "then `foilstack embed`"
            )
            session.commit()
            return

        # A batch declared one game or one set cannot decide anything until it
        # has seen all of itself, so the auto-accept decision waits for the
        # second pass and what every scan is holding stays in memory until
        # then. Held rather than stored: it is the working out, not the answer,
        # and only the one that gets used is worth a row. See `Pooled` for the
        # size of it — the vectors dominate, at 20 MB for a full 5000-image
        # archive, which is the price of not re-encoding the strays.
        pool: dict[int, Pooled] | None = {} if (job.same_game or job.same_set) else None

        for path in files:
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            scan = db.Scan(
                job_id=job.id,
                user_id=job.user_id,
                filename=path.name,
                stored_path=str(path.relative_to(settings.scans_dir)),
                size_bytes=size,
            )
            session.add(scan)
            session.commit()
            try:
                await _match_one(session, scan, settings, job, pool)
            except EmbedderError as exc:
                scan.status = "error"
                scan.error = str(exc)
            except Exception as exc:
                logger.exception("match failed for %s", path.name)
                scan.status = "error"
                scan.error = f"{type(exc).__name__}: {exc}"
            job.processed += 1
            session.commit()
            await asyncio.sleep(0)

        if pool is not None:
            # Its own status, because it is its own wait. On a large batch this
            # is a pass over every scan after the progress bar has already
            # reached the end, and a bar sitting full under the word "matching"
            # reads as a job that has hung.
            job.status = "grouping"
            session.commit()
            apply_cohort(session, job, pool, settings)

        job.status = "done"
        session.commit()
    except Exception as exc:
        logger.exception("import job %s failed", job_id)
        job.status = "failed"
        job.message = f"{type(exc).__name__}: {exc}"
        session.commit()
    finally:
        session.close()
        archive_path.unlink(missing_ok=True)
        # And the directory it was staged in, which nothing else was removing:
        # the route makes a fresh `mkdtemp` per upload and then returns, so
        # this task is the only thing left that could. `rmdir` rather than
        # `rmtree` because it refuses a directory with anything still in it,
        # which is what makes it safe against a caller that passed an archive
        # sitting among other files.
        with suppress(OSError):
            archive_path.parent.rmdir()


def reap_interrupted_jobs(session) -> int:
    """Fail every job left mid-flight by a process that is no longer running.

    `run_import` is a background task inside the web process, so a job only
    advances while that process lives, and nothing owns one across a restart:
    the archive was staged in a temporary directory the reboot took with it,
    and on the cohort pass the candidate pool the batch is decided from was
    only ever in memory. There is no resuming one. The only honest thing left
    is to say so — because the alternative is what shipped, a row still
    claiming `matching` that the import screen polls every 700ms forever,
    under a progress bar that sweeps for work nobody is doing.

    Boot is the proof, which is why this needs no timestamp and no staleness
    window: if this process is starting then no import is running anywhere, so
    every active row is by definition a corpse. That holds while the
    application is one process — uvicorn is started without `--workers` and
    compose runs a single `web` service. A second replica would break it, by
    failing jobs the other one is still working through.

    Deliberately not scoped by `user_id`. That is the one documented exception
    rather than a forgotten rule: this runs at startup on behalf of the
    install, not inside a request on behalf of a seller.
    """
    jobs = session.scalars(
        select(db.ImportJob).where(db.ImportJob.status.in_(ACTIVE_STATUSES))
    ).all()
    for job in jobs:
        # Message first: it is chosen off the status the job died in, and
        # overwriting that before reading it would collapse all three cases
        # onto the last one.
        job.message = _interrupted_message(job)
        job.status = "failed"
    session.commit()
    return len(jobs)


def _interrupted_message(job: db.ImportJob) -> str:
    """What was lost, in the seller's terms, so they know what to do next.

    Three shapes because three things go wrong. The queue cannot answer "how
    much of my upload landed" on its own — matched scans sit in it among
    everything else — so the counts come off the job row, which has them.
    """
    if job.status == "grouping":
        # Every scan matched; what did not finish is the pass that re-points
        # them at what the rest of the batch implies. Telling this seller to
        # re-upload would be telling them to duplicate the whole batch.
        return (
            f"interrupted by a restart after matching — all {job.total} scans "
            "are in the queue, matched one by one, but the batch's shared "
            "game or set was never applied to them"
        )
    if job.total:
        return (
            f"interrupted by a restart — {job.processed} of {job.total} scans "
            "matched and are in the queue; upload the archive again for the rest"
        )
    # Killed before `extract_archive` returned, so `total` is still its default
    # and not one scan row exists. Nothing landed and nothing needs sorting out.
    return "interrupted by a restart before any scan was matched; upload it again"


async def _match_one(
    session,
    scan,
    settings: Settings,
    job: db.ImportJob,
    pool: dict[int, Pooled] | None = None,
) -> None:
    path = scan_path(scan.stored_path, settings.scans_dir)
    if path is None:
        raise ImportError_(f"stored scan is missing: {scan.filename}")
    vector = await embed_image(settings.embedder_url, path.read_bytes())
    # After encoding, never before: the model gets the full-resolution original,
    # and the browser gets something it can actually load.
    images.make_display_copy(path, settings.display_dir, scan.stored_path)
    hits = search.search(
        session,
        vector,
        settings.embed_model,
        k=COHORT_CANDIDATE_COUNT if pool is not None else CANDIDATE_COUNT,
    )
    if not hits:
        scan.status = "unmatched"
        return

    # No floor. A weak nearest neighbour is still the most useful thing we can
    # say, and hiding it behind "unidentified" throws away the one clue that
    # explains the miss: a Magic scan whose best match is a Pokemon card at
    # 0.67 tells you the Magic catalogue is not ingested. The score, the game
    # and the reference image are all on screen, so a wrong match reads as a
    # wrong match rather than as an answer.
    scan.best_score = hits[0][1]
    for rank, (card_id, score) in enumerate(hits[:CANDIDATE_COUNT]):
        session.add(db.Candidate(scan_id=scan.id, card_id=card_id, score=score, rank=rank))

    if pool is not None:
        # Nothing is accepted mid-batch when the batch gets a say. Auto-accepting
        # here and re-pointing afterwards would mean writing inventory and then
        # taking it back, and a card that reached inventory has already been
        # priced, exported and possibly sold against.
        pool[scan.id] = Pooled(hits, vector)
        scan.status = "pending"
        return

    if _job_accepts(session, hits, settings, job):
        _accept(session, scan, hits[0][0], job)
    else:
        scan.status = "pending"


def _job_accepts(session, hits, settings: Settings, job: db.ImportJob) -> bool:
    """Whether this job may skip the queue for this scan.

    A job with no threshold never auto-accepts anything. Auto-accept is off
    unless the seller turned it on, and it is off rather than falling back to
    the configured default because those are two different answers to "nobody
    said": one puts every scan in front of a person, the other quietly writes
    inventory nobody asked it to. The screen has an Off chip and it ships
    selected, so a stock install reviews everything.
    """
    return job.auto_accept is not None and _may_auto_accept(
        session, hits, settings, job.auto_accept
    )


def _accept(session, scan, card_id: int, job: db.ImportJob) -> None:
    """Confirm a scan nobody looked at, and put the card in inventory."""
    scan.status = "confirmed"
    scan.auto_accepted = 1
    session.flush()
    # The batch default, unless this card is only priced on the other side
    # of the foil line — see `inventory.resolve_finish`. Nobody sees this
    # row before it becomes inventory, so committing a finish the
    # catalogue has no printing for would be a warning on the card page
    # about a decision that was never made.
    priced = inventory._prices_for(session, {card_id})
    _add_to_inventory(
        session,
        scan,
        card_id,
        job.default_condition or "NM",
        job.user_id,
        inventory.resolve_finish(job.default_finish or "nonfoil", list(priced.get(card_id, {}))),
    )


def _cohort_key(card: db.Card, same_set: bool) -> tuple[str, ...]:
    """What this card counts as, for a batch that claims to be one thing.

    Always the game, and the set only when the seller asked for it — with the
    game still in the tuple, because set names are not unique across games.
    Half the catalogues here have something called "Promo", and a Magic promo
    and a Pokemon promo are not the same cohort.
    """
    return (card.game, card.set_name or "") if same_set else (card.game,)


def _cohort_votes(
    pool: dict[int, Pooled],
    cards: dict[int, db.Card],
    same_set: bool,
) -> dict[tuple[str, ...], float]:
    """Score every cohort the batch's matches touched.

    Each scan gives each cohort **its best score under that cohort**, once —
    not one vote per candidate. Counting candidates instead would let one large
    set win on volume, because a scan of a common creature has eight near-
    identical printings and seven of them can sit in one core set.

    Deliberately not "the most common top match" either, which is the obvious
    rule and fails on exactly the batch this is for. A Magic card reprinted
    eight times has its top match scattered more or less at random across eight
    sets, so the set the seller actually opened might win only a fifth of the
    first places — while appearing somewhere in nearly every scan's candidate
    list, which is the signal this counts.
    """
    votes: dict[tuple[str, ...], float] = {}
    for pooled in pool.values():
        best: dict[tuple[str, ...], float] = {}
        for card_id, score in pooled.hits:
            card = cards.get(card_id)
            if card is None:
                continue
            key = _cohort_key(card, same_set)
            if score > best.get(key, -1.0):
                best[key] = score
        for key, score in best.items():
            votes[key] = votes.get(key, 0.0) + score
    return votes


def apply_cohort(
    session,
    job: db.ImportJob,
    pool: dict[int, Pooled],
    settings: Settings,
) -> None:
    """Second pass: hold the batch to the one game, or the one set, it mostly is.

    A scan is matched alone, but it did not arrive alone. The seller ticking
    "same game" or "same set" is asserting something about the physical stack
    that no single photograph contains, and it is the assertion that turns the
    other scans into evidence: if three hundred and eighty cards matched Base
    Set and this one matched a Magic reprint of the same artwork, the odds are
    not evenly split.

    So: work out what the batch agrees on, then walk back through it. A scan
    whose best match already belongs to that cohort is left exactly as it was
    and decided on its own merits. A scan whose best match does not is
    **searched again, inside the cohort** — recorded in `cohort_card_id`, which
    is a claim about the batch and not about the scan, and never auto-accepted.
    A heuristic about the neighbours is a good reason to put a card in front of
    a person and a bad reason to skip them.

    Searching again is the part that had to be learned. This used to read down
    the scan's existing candidate list for the first entry that conformed, and
    gave up when none of them did — which sounds equivalent and is not, because
    a global ranking is a poor place to look for the best member of a small
    set. A Dragon Ball card whose own printing sat at 0.83 was returned as a
    Magic card at 0.73 with nothing from its set in fifty rows, so the batch
    pass found nothing to move it to and stranded it on the wrong game while
    the right answer sat one filtered query away. Reading deeper is not the
    fix and no depth would have been: 161 cards out of 144,555 do not have to
    appear anywhere in a top-N to be the answer. Ask the narrower question
    instead — `search.search_within`, which is exact.

    So the only scan that still keeps the encoder's answer is one whose cohort
    turns out to hold no encoded card at all, which the election makes very
    nearly impossible: the cohort was elected by cards that matched, so at
    least one of them is encoded. It is counted rather than assumed away.
    """
    ids = {card_id for pooled in pool.values() for card_id, _ in pooled.hits}
    cards = _load_cards(session, ids)

    votes = _cohort_votes(pool, cards, job.same_set)
    if not votes:
        return
    # Ties broken by the cohort's own name so that two runs over the same batch
    # settle the same way. A coin flip here is a batch that re-points one way
    # today and the other way on a re-import, with no way to tell which.
    cohort = max(votes, key=lambda k: (votes[k], k))

    job.cohort_game = cohort[0]
    job.cohort_set = cohort[1] if job.same_set else None

    scans = {scan.id: scan for scan in job.scans}
    moved = stranded = 0
    for scan_id, pooled in pool.items():
        scan = scans.get(scan_id)
        if scan is None or not pooled.hits:
            continue

        top = cards.get(pooled.hits[0][0])
        if top is not None and _cohort_key(top, job.same_set) == cohort:
            if _job_accepts(session, pooled.hits[:CANDIDATE_COUNT], settings, job):
                _accept(session, scan, pooled.hits[0][0], job)
            continue

        within = search.search_within(
            session,
            pooled.vector,
            settings.embed_model,
            game=cohort[0],
            set_name=cohort[1] if job.same_set else "",
            k=1,
        )
        if not within:
            stranded += 1
            continue

        card_id, score = within[0]
        scan.cohort_card_id = card_id
        rank = next((n for n, (cid, _) in enumerate(pooled.hits) if cid == card_id), None)
        if rank is None or rank >= CANDIDATE_COUNT:
            # Only the top five are stored, so a pick the queue has no row for
            # would render with no score beside it and no way back to it.
            #
            # The rank written is the card's real place in the global search
            # when it had one — "the batch had to reach past nineteen
            # better-scoring cards for this" is the whole story of that row.
            # When it had none, it is filed at the end of what was looked at
            # rather than given an invented position: the filtered search
            # returns no global rank and computing one would mean scoring the
            # entire catalogue to answer a question nothing asks. Ordering is
            # all the column is for here, and last is where it belongs.
            session.add(
                db.Candidate(
                    scan_id=scan.id,
                    card_id=card_id,
                    score=score,
                    rank=len(pooled.hits) if rank is None else rank,
                )
            )
        moved += 1

    # Set before the commit, not after it. `run_import` happens to commit again
    # on its way to "done", so writing this afterwards worked by luck — and the
    # luck runs out the moment anything is added between the two.
    job.message = _cohort_message(job, cohort, moved, stranded)
    session.commit()


def _load_cards(session, ids: set[int]) -> dict[int, db.Card]:
    """Every candidate card in one batch's search results, by id.

    Chunked because the set is unbounded in principle — five thousand scans of
    twenty-five hits each — and a single `IN` with six figures of parameters is
    how a query that works on a test archive falls over on a real one.
    """
    from sqlalchemy import select

    cards: dict[int, db.Card] = {}
    ordered = sorted(ids)
    for start in range(0, len(ordered), 2000):
        chunk = ordered[start : start + 2000]
        for card in session.scalars(select(db.Card).where(db.Card.id.in_(chunk))):
            cards[card.id] = card
    return cards


def _cohort_message(job: db.ImportJob, cohort: tuple[str, ...], moved: int, stranded: int) -> str:
    """What the second pass did, in the words the import screen shows.

    Said out loud rather than left to be noticed, because this is the one
    setting that changes matches the encoder was confident about. A seller who
    ticked the box on a genuinely mixed batch has to be able to see that from
    the outside — the count of moved rows is the number that tells them.
    """
    what = "same set" if job.same_set else "same game"
    label = " · ".join(part for part in cohort if part)
    bits = [f"{what}: {label}", f"{moved} moved to it"]
    if stranded:
        # Not a routine outcome any more, and worded so it does not read like
        # one: since the move is a filtered search rather than a walk down the
        # candidate list, the only way to land here is a cohort with nothing
        # encoded in it. That is a catalogue problem, not a match problem.
        bits.append(f"{stranded} the catalogue had no encoded card for")
    return " · ".join(bits)


def _may_auto_accept(session, hits, settings: Settings, threshold: float | None = None) -> bool:
    """Three conditions, not one. A high score by itself is not evidence.

    Measured on Base Set: a clean scan of Machop scores 1.000 against the
    Normal printing and 0.946 against the 1st Edition — 0.054 apart, thirty
    times the price. A degraded Charizard scored 0.932 against Holofoil with
    the $10,000 1st Edition sitting at 0.913 behind it. Any rule that looks
    only at the top number auto-accepts those.

    So: the score must clear the threshold, it must clear the runner-up by a
    real margin, and — the rule that actually does the work — if the runner-up
    is *the same card in a different printing*, no score is good enough. That
    is precisely the case a photograph cannot settle, because the printings
    differ by a set symbol a few pixels wide and nothing else.
    """
    top_id, top_score = hits[0]
    # The threshold chosen for this job, falling back to the configured
    # default. Per job rather than global so that changing it later never
    # rewrites the reason an old scan was accepted.
    if top_score < (settings.auto_accept if threshold is None else threshold):
        return False
    if len(hits) < 2:
        return True

    second_id, second_score = hits[1]
    if top_score - second_score < settings.auto_accept_margin:
        return False

    top = session.get(db.Card, top_id)
    second = session.get(db.Card, second_id)
    if top is None or second is None:
        return False

    same_card = (
        top.name == second.name and top.number == second.number and top.variant != second.variant
    )
    if not same_card:
        return True

    # Same card, two printings. Whether that matters depends entirely on what
    # it costs to be wrong. Blocking every one of them is useless advice: in
    # Base Set every single card has a 1st Edition twin, so a blanket rule
    # sends all 5,000 of a dealer's scans to review and the tool does nothing.
    #
    # So compare the prices. Two printings within a few percent are worth the
    # same money and picking either is harmless. A twelvefold gap — Charizard
    # Holofoil at $855 against 1st Edition at $10,000 — is the whole reason
    # this queue exists.
    return not _prices_differ_materially(top.market, second.market)


def _prices_differ_materially(a: float | None, b: float | None) -> bool:
    """Unknown prices count as material: we cannot show a gap is safe."""
    if a is None or b is None:
        return True
    high, low = max(a, b), min(a, b)
    if high <= 0:
        return False
    if high - low < MIN_ABSOLUTE_GAP:
        return False
    return (high - low) / high > PRICE_GAP_TOLERANCE


def _add_to_inventory(
    session, scan, card_id: int, condition: str, user_id: int, finish: str
) -> None:
    session.add(
        db.InventoryItem(
            user_id=user_id,
            card_id=card_id,
            scan_id=scan.id,
            condition=condition,
            finish=finish,
        )
    )


async def rematch_scan(session, scan, settings: Settings) -> bool:
    """Re-encode one stored scan and replace its candidates.

    Returns True if it now has a match. Used after ingesting a set the seller
    actually collects: the images are already on disk, so re-uploading the same
    archive to benefit from a bigger catalogue would be pure waste.

    The scan's review state is deliberately *not* reset if it was already
    confirmed — a card someone has accepted into inventory is settled, and
    quietly re-deciding it behind their back is worse than leaving it alone.

    `chosen_card_id` survives for the same reason. The candidate list beneath
    it is replaced, so a re-match against a bigger catalogue still improves the
    runners-up on offer; what it must not do is overwrite the answer a person
    already gave with a fresh guess.

    `cohort_card_id` does *not* survive, and the difference is who made the
    claim. A person's choice is about the card. The batch's is about a search
    result, and this replaces that search result — so the pick is left pointing
    into a ranking that no longer exists, chosen over runners-up that are gone.
    The batch cannot be re-run for one scan, so the honest thing is to drop
    back to what the new search says and let the queue ask.
    """
    if scan.status == "confirmed":
        return False

    path = scan_path(scan.stored_path, settings.scans_dir)
    if path is None:
        logger.warning("cannot re-match %s: image is missing", scan.filename)
        return False

    vector = await embed_image(settings.embedder_url, path.read_bytes())
    images.make_display_copy(path, settings.display_dir, scan.stored_path)
    hits = search.search(session, vector, settings.embed_model, k=CANDIDATE_COUNT)

    for candidate in list(scan.candidates):
        session.delete(candidate)
    scan.cohort_card_id = None
    session.flush()

    if not hits:
        scan.status = "unmatched"
        scan.best_score = None
        return False

    scan.best_score = hits[0][1]
    for rank, (card_id, score) in enumerate(hits):
        session.add(db.Candidate(scan_id=scan.id, card_id=card_id, score=score, rank=rank))
    scan.status = "pending"
    scan.error = None
    return True

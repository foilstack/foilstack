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

Everything written here carries the owner of the job that produced it. The
catalogue is shared; scans and inventory are not.
"""

from __future__ import annotations

import asyncio
import logging
import os
import zipfile
from pathlib import Path, PurePosixPath

from foilstack import db, images, search
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
CANDIDATE_COUNT = 5

# How far two printings' prices may diverge before a match between them has to
# be reviewed. 20% of the dearer one, and never mind a gap under a dollar —
# below that the review costs more attention than the error costs money.
PRICE_GAP_TOLERANCE = 0.20
MIN_ABSOLUTE_GAP = 1.00


class ImportError_(RuntimeError):
    pass


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
    protecting. Discarded scans delete their rows, so this falls when they do.
    """
    from sqlalchemy import func, select

    total = session.scalar(
        select(func.coalesce(func.sum(db.Scan.size_bytes), 0)).where(db.Scan.user_id == user_id)
    )
    return int(total or 0)


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

    return sorted(written)


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
                await _match_one(session, scan, settings, job)
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


async def _match_one(session, scan, settings: Settings, job: db.ImportJob) -> None:
    path = scan_path(scan.stored_path, settings.scans_dir)
    if path is None:
        raise ImportError_(f"stored scan is missing: {scan.filename}")
    vector = await embed_image(settings.embedder_url, path.read_bytes())
    # After encoding, never before: the model gets the full-resolution original,
    # and the browser gets something it can actually load.
    images.make_display_copy(path, settings.display_dir, scan.stored_path)
    hits = search.search(session, vector, settings.embed_model, k=CANDIDATE_COUNT)
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
    for rank, (card_id, score) in enumerate(hits):
        session.add(db.Candidate(scan_id=scan.id, card_id=card_id, score=score, rank=rank))

    if _may_auto_accept(session, hits, settings, job.auto_accept):
        scan.status = "confirmed"
        scan.auto_accepted = 1
        session.flush()
        _add_to_inventory(
            session,
            scan,
            hits[0][0],
            job.default_condition or "NM",
            job.user_id,
            job.default_finish or "nonfoil",
        )
    else:
        scan.status = "pending"


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

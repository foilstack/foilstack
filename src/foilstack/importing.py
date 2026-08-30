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
from pathlib import Path, PurePosixPath

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
CANDIDATE_COUNT = 5
# How deep the search goes when the batch has been declared one game or one
# set. Five is the right number to *show* — past that the runners-up are noise
# a person has to read — but it is the wrong number to choose from: a Pokemon
# scan that matched five Magic cards has no conforming runner-up in its top
# five, and that is precisely the scan the setting exists to rescue. The extra
# twenty cost nothing in the index and are never stored unless one is used.
COHORT_CANDIDATE_COUNT = 25

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
        # second pass and the hits every scan is holding are kept in memory
        # until then. Held rather than stored: they are the working out, not
        # the answer, and only the one that gets used is worth a row. Five
        # thousand scans of twenty-five `(int, float)` pairs is a few megabytes
        # for the life of one job.
        pool: dict[int, list[tuple[int, float]]] | None = (
            {} if (job.same_game or job.same_set) else None
        )

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


async def _match_one(
    session,
    scan,
    settings: Settings,
    job: db.ImportJob,
    pool: dict[int, list[tuple[int, float]]] | None = None,
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
        pool[scan.id] = hits
        scan.status = "pending"
        return

    if _may_auto_accept(session, hits, settings, job.auto_accept):
        _accept(session, scan, hits[0][0], job)
    else:
        scan.status = "pending"


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
    pool: dict[int, list[tuple[int, float]]],
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
    for hits in pool.values():
        best: dict[tuple[str, ...], float] = {}
        for card_id, score in hits:
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
    pool: dict[int, list[tuple[int, float]]],
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
    and decided on its own merits. A scan whose best match does not is moved to
    its highest-scoring candidate that does — recorded in `cohort_card_id`,
    which is a claim about the batch and not about the scan, and never
    auto-accepted. A heuristic about the neighbours is a good reason to put a
    card in front of a person and a bad reason to skip them.

    A scan with nothing conforming anywhere in its candidates keeps the
    encoder's answer and goes to review too, because the one thing that is
    certain about it is that it contradicts the rest of the batch.
    """
    ids = {card_id for hits in pool.values() for card_id, _ in hits}
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
    for scan_id, hits in pool.items():
        scan = scans.get(scan_id)
        if scan is None or not hits:
            continue

        top = cards.get(hits[0][0])
        if top is not None and _cohort_key(top, job.same_set) == cohort:
            if _may_auto_accept(session, hits[:CANDIDATE_COUNT], settings, job.auto_accept):
                _accept(session, scan, hits[0][0], job)
            continue

        pick = next(
            (
                (rank, card_id, score)
                for rank, (card_id, score) in enumerate(hits)
                if card_id in cards and _cohort_key(cards[card_id], job.same_set) == cohort
            ),
            None,
        )
        if pick is None:
            stranded += 1
            continue

        rank, card_id, score = pick
        scan.cohort_card_id = card_id
        if rank >= CANDIDATE_COUNT:
            # Only the top five are stored, so a pick from deeper in the search
            # has no row yet and the queue would show a match with no score
            # beside it and no way back to it. Written at its real rank rather
            # than appended at five: the rank is what the encoder said, and
            # "the batch had to reach past nineteen better-scoring cards for
            # this" is the whole story of the row.
            session.add(db.Candidate(scan_id=scan.id, card_id=card_id, score=score, rank=rank))
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
        bits.append(f"{stranded} with nothing in it to move to")
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

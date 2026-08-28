"""Spin up a throwaway instance with sample data, for looking at the UI.

A disposable database and a signed-in account, so screenshots never require
touching a real deployment or anybody's password. Catalogue rows are copied
from whatever database DATABASE_URL points at, because the reference images are
what make the screens look like themselves — no inventory, scans or accounts
come across.

    uv run python scripts/preview.py --port 8099        # serve until Ctrl-C
    uv run python scripts/preview.py --shots ./shots    # screenshot and exit
    uv run python scripts/preview.py --demo src/foilstack/web/static/demo   # record the walkthrough
    uv run python scripts/preview.py --landing src/foilstack/web/static/shots  # the landing stills
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import create_engine, func, select, text

EMAIL = "preview@foilstack.invalid"
PASSWORD = "preview-only-password"

# How many scans wait in the review queue. Enough to fill the pane, so the
# screen shows what reviewing a batch is actually like.
PENDING = 7


def _admin_url() -> str:
    from foilstack.config import get_settings

    return get_settings().database_url


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--shots", type=Path, default=None)
    ap.add_argument("--demo", type=Path, default=None, help="record the walkthrough and exit")
    ap.add_argument(
        "--landing", type=Path, default=None, help="shoot the landing-page stills and exit"
    )
    ap.add_argument("--keep", action="store_true", help="leave the database behind")
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--height", type=int, default=900)
    ap.add_argument("--only", default=None, help="shoot just this screen by name")
    ap.add_argument("--scale", type=int, default=None, help="device pixel ratio for --demo")
    ap.add_argument("--keep-frames", action="store_true", help="leave --demo frames on disk")
    args = ap.parse_args(argv)

    source_url = _admin_url()
    name = f"foilstack_preview_{uuid.uuid4().hex[:8]}"
    base = source_url.rsplit("/", 1)[0]
    preview_url = f"{base}/{name}"

    admin = create_engine(source_url, isolation_level="AUTOCOMMIT", future=True)
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    print(f"created {name}")

    try:
        env = dict(os.environ, DATABASE_URL=preview_url)
        subprocess.run(
            ["alembic", "upgrade", "head"], check=True, env=env, stdout=subprocess.DEVNULL
        )
        _seed(source_url, preview_url)

        proc = subprocess.Popen(
            [
                "uvicorn",
                "foilstack.web.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(args.port),
                "--log-level",
                "warning",
            ],
            env=dict(
                env,
                FOILSTACK_MULTI_USER="true",
                FOILSTACK_SECRET_KEY="preview-only-secret",
                PYTHONPATH="src",
            ),
        )
        url = f"http://127.0.0.1:{args.port}"
        _wait(url)
        print(f"serving {url}  ({EMAIL} / {PASSWORD})")

        if args.demo:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from demo import main as record

            card_id = _first_card(preview_url)
            record(
                [
                    "--url",
                    url,
                    "--out",
                    str(args.demo),
                    "--email",
                    EMAIL,
                    "--password",
                    PASSWORD,
                    *(["--card", str(card_id)] if card_id else []),
                    *(["--scale", str(args.scale)] if args.scale else []),
                    *(["--keep-frames"] if args.keep_frames else []),
                ]
            )
            proc.terminate()
        elif args.landing:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from landing import main as shoot_landing

            card_id = _first_card(preview_url)
            shoot_landing(
                [
                    "--url",
                    url,
                    "--out",
                    str(args.landing),
                    "--email",
                    EMAIL,
                    "--password",
                    PASSWORD,
                    *(["--card", str(card_id)] if card_id else []),
                    *(["--scale", str(args.scale)] if args.scale else []),
                ]
            )
            proc.terminate()
        elif args.shots:
            from shots import main as shoot

            sys.path.insert(0, str(Path(__file__).resolve().parent))
            card_id = _first_card(preview_url)
            shoot(
                [
                    "--url",
                    url,
                    "--out",
                    str(args.shots),
                    "--email",
                    EMAIL,
                    "--password",
                    PASSWORD,
                    "--width",
                    str(args.width),
                    "--height",
                    str(args.height),
                    *(["--only", args.only] if args.only else []),
                    *(["--card", str(card_id)] if card_id else []),
                ]
            )
            proc.terminate()
        else:
            proc.wait()
    finally:
        if not args.keep:
            with admin.connect() as conn:
                conn.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :n"
                    ),
                    {"n": name},
                )
                conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
            print(f"dropped {name}")
    return 0


def _seed(source_url: str, preview_url: str) -> None:
    """A user, a slice of the catalogue, some scans and some inventory."""
    import datetime as dt

    from foilstack import db
    from foilstack.web import auth

    src = create_engine(source_url, future=True)
    with src.connect() as conn:
        # Real (scan, card) pairs from real matching, best score first — not a
        # scan and a card zipped together by position. A screenshot of this
        # screen argues that the top match is evidence; pairing an arbitrary
        # card with an arbitrary photograph and captioning it "99% match" makes
        # that argument dishonest, and it is obvious to anyone who plays.
        pairs = (
            conn.execute(
                text(
                    "SELECT s.id AS scan_id, s.stored_path, s.filename, s.auto_accepted,"
                    "       c.score, cd.id, cd.source, cd.source_id, cd.name, cd.game,"
                    "       cd.set_name, cd.number, cd.variant, cd.image_url, cd.market,"
                    "       cd.currency"
                    "  FROM scans s"
                    "  JOIN candidates c ON c.scan_id = s.id AND c.rank = 0"
                    "  JOIN cards cd ON cd.id = c.card_id"
                    " WHERE cd.image_url IS NOT NULL AND cd.market IS NOT NULL"
                    " ORDER BY c.score DESC LIMIT 60"
                )
            )
            .mappings()
            .all()
        )

        # The runners-up, from the same matching run.
        #
        # The preview used to seed one candidate per scan, which quietly made
        # the queue argue less than the product does: `also matched: ...` never
        # rendered, and the "wrong card?" panel opened on a heading that
        # promised a choice above a single option. The screen's whole claim is
        # that the top match is evidence rather than an assertion, and evidence
        # means the things it beat are visible.
        #
        # Real rows again, not invented ones. A fabricated runner-up captioned
        # with a percentage is the same dishonesty as a fabricated top match.
        alternates = (
            conn.execute(
                text(
                    "SELECT c.scan_id, c.score, c.rank,"
                    "       cd.id, cd.source, cd.source_id, cd.name, cd.game,"
                    "       cd.set_name, cd.number, cd.variant, cd.image_url, cd.market,"
                    "       cd.currency"
                    "  FROM candidates c"
                    "  JOIN cards cd ON cd.id = c.card_id"
                    " WHERE c.scan_id = ANY(:ids) AND c.rank BETWEEN 1 AND 3"
                    "   AND cd.image_url IS NOT NULL"
                    " ORDER BY c.scan_id, c.rank"
                ),
                {"ids": [r["scan_id"] for r in pairs]},
            )
            .mappings()
            .all()
        )

    if not pairs:
        raise SystemExit(
            "the source database has no matched scans to build a preview from.\n"
            "Import an archive there first — a preview seeded with invented "
            "pairs would show matches that are not matches."
        )
    # Distinct cards first, then the duplicates.
    #
    # Ordering purely by score put the same card in the queue twice, because
    # two scans of one card both match it and both score highly. On screen that
    # reads as a bug rather than as a duplicate, and the queue is the first
    # thing anyone sees. So: a run of distinct cards to review, and the repeats
    # held back for the confirmed rows, where a duplicate is the point — it is
    # what the inventory screen consolidates.
    distinct: list = []
    repeats: list = []
    seen_card: set[int] = set()
    for row in pairs:
        if row["id"] in seen_card:
            repeats.append(row)
        else:
            seen_card.add(row["id"])
            distinct.append(row)

    # Enough waiting in the queue to fill the pane and to be worth scrolling.
    # Two was enough to prove the screen renders and far too few to show what
    # reviewing actually feels like.
    pending = distinct[:PENDING]
    cards = pending + distinct[PENDING:] + repeats[:2]

    db.init(preview_url)
    session = db.session()
    user = db.User(email=EMAIL, password_hash=auth.hash_password(PASSWORD))
    session.add(user)
    session.commit()

    # Two scans can match the same printing — that is the duplicate case the
    # inventory screen exists to consolidate — so the catalogue rows behind
    # them have to be deduplicated before insert.
    kept_scans = {row["scan_id"] for row in cards}
    alts_by_scan: dict[int, list] = {}
    for alt in alternates:
        if alt["scan_id"] in kept_scans:
            alts_by_scan.setdefault(alt["scan_id"], []).append(alt)

    seen: set[int] = set()
    # The runners-up are catalogue rows like any other and have to exist before
    # a candidate can point at one.
    for row in list(cards) + [a for alts in alts_by_scan.values() for a in alts]:
        if row["id"] in seen:
            continue
        seen.add(row["id"])
        session.add(
            db.Card(
                **{
                    k: row[k]
                    for k in (
                        "id",
                        "source",
                        "source_id",
                        "name",
                        "game",
                        "set_name",
                        "number",
                        "variant",
                        "image_url",
                        "market",
                        "currency",
                    )
                }
            )
        )
    session.commit()

    job = db.ImportJob(
        user_id=user.id,
        filename="preview-batch.zip",
        status="done",
        total=len(cards),
        processed=len(cards),
    )
    session.add(job)
    session.commit()

    # Two scans left in the queue so the import screen has something to show,
    # the rest committed so inventory does — including a duplicate and a sale.
    for i, card in enumerate(cards):
        scan = db.Scan(
            job_id=job.id,
            user_id=user.id,
            filename=card["filename"],
            stored_path=card["stored_path"],
            status="pending" if i < PENDING else "confirmed",
            auto_accepted=card["auto_accepted"],
            best_score=float(card["score"]),
        )
        session.add(scan)
        session.commit()
        # The real score, so the number under the bar is the number the matcher
        # actually produced for these two images.
        session.add(
            db.Candidate(scan_id=scan.id, card_id=card["id"], score=float(card["score"]), rank=0)
        )
        for alt in alts_by_scan.get(card["scan_id"], []):
            # Skip a runner-up that is the top match again: two scans of one
            # printing is ordinary, and (scan, card) has to stay unique.
            if alt["id"] == card["id"]:
                continue
            session.add(
                db.Candidate(
                    scan_id=scan.id,
                    card_id=alt["id"],
                    score=float(alt["score"]),
                    rank=int(alt["rank"]),
                )
            )
        if i >= PENDING:
            session.add(
                db.InventoryItem(
                    user_id=user.id,
                    card_id=card["id"],
                    scan_id=scan.id,
                    condition="NM" if i % 3 else "LP",
                    finish="foil" if i % 4 == 0 else "nonfoil",
                    cost=round(float(card["market"]) * 0.4, 2),
                    status="sold" if i == 5 else "stock",
                    sold_price=round(float(card["market"]) * 0.9, 2) if i == 5 else None,
                    sold_at=(dt.datetime.now(dt.UTC) - dt.timedelta(days=9) if i == 5 else None),
                )
            )
    # A short run of price history on the first few cards, so the trend has a
    # shape to draw. The last card is left with a single reading on purpose —
    # that state has its own rendering and is worth being able to see.
    import random

    today = dt.date.today()

    # Price history goes on cards that are *in the inventory*, which is the only
    # place a chart can be reached from — `/inventory/<card>` is reached by
    # clicking a stock line. Attaching it to the first few cards overall put it
    # on the ones still sitting in the review queue, so every card page a
    # visitor could actually open drew an empty panel.
    #
    # Deduplicated too. Two scans matching one printing is the ordinary case —
    # it is the duplicate the inventory screen consolidates — and card_prices is
    # keyed on (card_id, sub_type), so the repeat raised a unique violation and
    # took the whole preview down with it.
    unique: list = []
    seen_price: set[int] = set()
    for card in cards[PENDING:]:
        if card["id"] in seen_price:
            continue
        seen_price.add(card["id"])
        unique.append(card)

    for n, card in enumerate(unique[:7]):
        base = float(card["market"] or 1.0)
        # The seventh gets exactly one reading — the state every card is in on
        # the day price recording starts, and the one that must not render as a
        # line through no information.
        backs = [0] if n == 6 else list(range(21, -1, -3))
        for back in backs:
            drift = 1 + (random.random() - 0.45) * 0.08 * (21 - back) / 21
            session.add(
                db.CardPriceHistory(
                    card_id=card["id"],
                    sub_type=card.get("variant") or "Normal",
                    recorded_on=today - dt.timedelta(days=back),
                    market=round(base * drift, 2),
                    low=round(base * drift * 0.88, 2),
                    mid=round(base * drift * 1.05, 2),
                    high=round(base * drift * 1.4, 2),
                )
            )
        session.add(
            db.CardPrice(
                card_id=card["id"],
                sub_type=card.get("variant") or "Normal",
                market=base,
                low=round(base * 0.88, 2),
                mid=round(base * 1.05, 2),
                high=round(base * 1.4, 2),
            )
        )
    session.commit()

    # One card with the ambiguity the picker exists for: three printings that
    # all answer to "foil", an order of magnitude apart.
    amb = unique[0]
    ambiguous = (
        ("1st Edition Holofoil", 30.0),
        ("Unlimited Holofoil", 6.4),
        ("Holofoil", 1.0),
    )
    for sub, mult in ambiguous:
        base = float(amb["market"] or 1.0) * mult
        session.merge(
            db.CardPrice(
                card_id=amb["id"],
                sub_type=sub,
                market=round(base, 2),
                low=round(base * 0.9, 2),
                mid=round(base * 1.05, 2),
                high=round(base * 1.3, 2),
            )
        )
        # History per printing, not just per card. The trend panel is keyed on
        # the printing a copy is declared as, so a card whose history was
        # written under "Normal" while its copies say "1st Edition Holofoil"
        # renders "no price history" — on the very card chosen to show the
        # feature off, because the ambiguous one is the interesting one.
        for back in range(21, -1, -3):
            drift = 1 + (random.random() - 0.45) * 0.09 * (21 - back) / 21
            session.merge(
                db.CardPriceHistory(
                    card_id=amb["id"],
                    sub_type=sub,
                    recorded_on=today - dt.timedelta(days=back),
                    market=round(base * drift, 2),
                    low=round(base * drift * 0.9, 2),
                    mid=round(base * drift * 1.05, 2),
                    high=round(base * drift * 1.3, 2),
                )
            )
    session.add(
        db.InventoryItem(
            user_id=user.id,
            card_id=amb["id"],
            condition="NM",
            finish="foil",
            cost=round(float(amb["market"] or 1.0) * 0.4, 2),
        )
    )
    session.commit()

    # A second copy of one card, so a quantity above 1 is visible.
    session.add(
        db.InventoryItem(
            user_id=user.id,
            card_id=cards[3]["id"],
            condition="NM",
            finish="nonfoil",
            cost=round(float(cards[3]["market"]) * 0.35, 2),
        )
    )
    session.commit()
    session.close()


def _first_card(preview_url: str) -> int | None:
    """A stock line worth opening: one with a price history behind it.

    The plain "first inventory row" this used to be was a coin toss, and both
    the demo and the landing stills exist to show a price trend — a card page
    reading "No price history for this card. Run foilstack sync-prices" is an
    illustration of the feature being absent. It got through once, into a
    screenshot placed directly under a paragraph about price history.

    Preferring the card with the most recorded price points also picks the most
    legible chart, since two points is a line segment rather than a trend.
    Falls back to the old behaviour when nothing has been synced at all.
    """
    from foilstack import db

    db.init(preview_url)
    session = db.session()
    try:
        with_history = session.scalar(
            select(db.InventoryItem.card_id)
            .join(db.CardPriceHistory, db.CardPriceHistory.card_id == db.InventoryItem.card_id)
            .group_by(db.InventoryItem.card_id)
            .order_by(func.count().desc())
            .limit(1)
        )
        return with_history or session.scalar(
            select(db.InventoryItem.card_id).order_by(db.InventoryItem.id)
        )
    finally:
        session.close()


def _wait(url: str, tries: int = 60) -> None:
    import httpx

    for _ in range(tries):
        try:
            if httpx.get(f"{url}/healthz", timeout=1.0).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"{url} never became ready")


if __name__ == "__main__":
    sys.exit(main())

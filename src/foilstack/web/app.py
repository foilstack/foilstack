"""The web application.

Server-rendered, no build step, no framework on the client. A self-hosted tool
that needs npm before it will show you a page is a tool most people will never
see, and there is nothing here that justifies the cost.

The chrome follows the design mock: a fixed-height shell, a rail, and a status
line. Screens are panes that scroll independently, so the queue you are working
through stays on screen while you decide about one card.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import mimetypes
import os
import shutil
import tempfile
from contextlib import asynccontextmanager
from hmac import compare_digest
from pathlib import Path

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import (
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from foilstack import __version__, db, importing, inventory, prices, search
from foilstack.config import get_settings
from foilstack.embedding import encoder_health
from foilstack.importing import run_import
from foilstack.plugins import export_plugins, source_plugins
from foilstack.web import auth, joblog, ratelimit
from foilstack.web.deps import api_owner, db_session, owner
from foilstack.web.routes import media

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent


def _register_mime_types(db: mimetypes.MimeTypes | None = None) -> None:
    """Type the assets whose extensions the host may not know.

    StaticFiles types every response with `mimetypes.guess_type`, which reads
    the system's mime table — and `python:3.12-slim` ships one that has never
    heard of either of these. The bundled fonts went out as `application/json`
    and the demo as `application/octet-stream`, which the `nosniff` header then
    tells the browser not to second-guess.

    Takes a registry so this is testable off a development machine, where
    `/etc/mime.types` already supplies the answers and an end-to-end check
    therefore passes whether or not this function exists.
    """
    add = mimetypes.add_type if db is None else db.add_type
    add("image/webp", ".webp")
    add("font/woff2", ".woff2")
    add("font/woff", ".woff")


_register_mime_types()

app = FastAPI(title="foilstack", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
app.include_router(media.router)
templates = Jinja2Templates(directory=BASE_DIR / "templates")

settings = get_settings()

# The channels offered on the listing screen. Every one of them is a CSV you
# upload yourself: there is no API client behind any of these names, and the
# screen says so rather than implying a connection that does not exist.
CHANNELS = [
    {"key": "tcgplayer", "name": "TCGplayer", "note": "csv upload · no api key held"},
    {"key": "ebay", "name": "eBay", "note": "csv upload · no oauth held"},
]


# Two budgets, both of which must allow an attempt through. The address budget
# stops one machine working through a password list; the account budget stops a
# botnet doing the same thing from a thousand addresses against one seller.
_login_ip = ratelimit.Limiter(settings.login_attempts, settings.login_window_s)
_login_account = ratelimit.Limiter(settings.login_attempts, settings.login_window_s)

# Registration is cheaper to abuse than login — every attempt that succeeds
# costs a row and a slot — so it gets a tighter budget on the address alone.
_register_ip = ratelimit.Limiter(max(3, settings.login_attempts // 2), settings.login_window_s)


def _money(n: float | None) -> str:
    if n is None:
        return "—"
    return ("-$" if n < 0 else "$") + f"{abs(n):,.2f}"


templates.env.filters["money"] = _money


def _asset_version() -> str:
    """A short hash of the static assets, appended to their URLs.

    Without it a CDN happily serves last week's CSS against today's HTML, and
    the page arrives with every new class name unstyled — which looks like
    broken CSS rather than a cache, and cost an hour to spot the first time.

    Every file that carries this query string has to be hashed into it. The
    stylesheet alone was enough until there was a script too: a JS change with
    no CSS change would have shipped a stale script behind an unchanged
    version, which is the same bug wearing a different hat.
    """
    digest = hashlib.sha256()
    for name in ("app.css", "zoom.js", "brand/mark.svg", "brand/favicon.svg"):
        try:
            digest.update((BASE_DIR / "static" / name).read_bytes())
        except OSError:
            return "0"

    # The demo, by size and mtime rather than by content. It carries the same
    # query string, so it has to be in the hash — a CDN held a stale
    # `application/octet-stream` copy of it through a deploy that fixed exactly
    # that, because the URL never changed. Reading three megabytes on every
    # request to learn something `stat` already knows would be the wrong way to
    # fix it.
    for name in ("demo/foilstack.webp", "demo/foilstack.gif"):
        try:
            info = (BASE_DIR / "static" / name).stat()
        except OSError:
            continue
        digest.update(f"{name}:{info.st_size}:{info.st_mtime_ns}".encode())

    return digest.hexdigest()[:10]


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Fails the boot rather than the first login: a multi-user deployment
    # signing sessions with the published development key is one anybody can
    # forge a session against.
    auth.check_secret(settings)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.refs_dir.mkdir(parents=True, exist_ok=True)
    settings.display_dir.mkdir(parents=True, exist_ok=True)
    db.init(settings.database_url)
    yield


app.router.lifespan_context = _lifespan


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    """Headers the browser needs in order to refuse things on our behalf.

    Deliberately not a script/style CSP. The screens carry inline `<script>`
    and inline `style=`, so a `script-src` policy strict enough to be worth
    having would switch half the interface off — that wants nonces threaded
    through every template, which is a real change and not a header. What is
    here is the part that costs nothing and still closes real holes:

    * `frame-ancestors` and `X-Frame-Options` — nobody frames this page, so
      nobody clickjacks the delete button on somebody's inventory.
    * `nosniff` — a scan uploaded as `card.jpg` is served as an image and must
      never be sniffed into something executable.
    * `Referrer-Policy` — inventory URLs carry card ids; those should not be
      handed to whatever a user clicks through to.
    * HSTS, but only on a request that already arrived over TLS. Sending it
      over plain HTTP is both ignored by browsers and a way to lock a
      self-hoster out of their own LAN deployment.
    """
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Permissions-Policy", "camera=(), microphone=(), geolocation=(), interest-cohort=()"
    )
    if request.url.scheme == "https":
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response


def _aware(when: dt.datetime) -> dt.datetime:
    """Postgres hands these back tz-aware; a stray naive one must not crash a page."""
    return when if when.tzinfo else when.replace(tzinfo=dt.UTC)


def _ago(then: dt.datetime | None) -> str:
    if then is None:
        return "never"
    if then.tzinfo is None:
        then = then.replace(tzinfo=dt.UTC)
    seconds = (dt.datetime.now(dt.UTC) - then).total_seconds()
    if seconds < 90:
        return "just now"
    for unit, size in (("min", 60), ("hr", 3600), ("d", 86400)):
        if seconds < size * 60 or unit == "d":
            return f"{int(seconds // size)} {unit} ago"
    return "a while ago"


def _chrome(session, request: Request, user: db.User) -> dict:
    """The values every page's shell needs, for this account only."""
    # One row is one card, and only cards still in stock count as "held".
    mine = (db.InventoryItem.user_id == user.id, db.InventoryItem.status == "stock")
    held = session.scalar(select(func.count(db.InventoryItem.id)).where(*mine)) or 0
    # Priced the same way the inventory screen prices it — per printing, with
    # any printing the seller named taking precedence. Summing `cards.market`
    # here was cheaper and wrong: that column is the plain printing's price, so
    # the topbar quoted one total while the table below it showed another.
    rows = inventory.items(session, user.id, status="stock")
    value = sum(r["market"] or 0 for r in rows)
    needs_printing = sum(1 for r in rows if r["printing_guessed"])
    # When the sync last *ran*, not when a price last moved.
    #
    # This read `max(cards.updated_at)`, which only advances when a number
    # actually changes — so a sync that ran ten minutes ago against an unchanged
    # upstream still reported "synced 10 hr ago", and the footer looked like a
    # stalled job. It sent someone to check whether the service was broken,
    # which is the opposite of what a status line is for. `sync_state` records
    # every run, including the ones that correctly did nothing.
    synced = session.scalar(
        select(func.max(db.SyncState.last_run_at)).where(db.SyncState.kind.like("prices%"))
    ) or session.scalar(select(func.max(db.Card.updated_at)))
    sources = session.scalars(select(db.Card.source).distinct()).all()

    return {
        "version": __version__,
        "git_sha": settings.git_sha,
        # Recomputed per request: the stylesheet is bind-mounted during
        # development, so caching this would defeat the point of having it.
        "asset_v": _asset_version(),
        "catalog_cards": session.scalar(select(func.count(db.Card.id))) or 0,
        "vector_count": search.count(session, settings.embed_model),
        "pending": session.scalar(
            select(func.count(db.Scan.id)).where(
                db.Scan.user_id == user.id, db.Scan.status == "pending"
            )
        )
        or 0,
        "inventory_rows": held,
        "inventory_count": held,
        "inventory_value": _money(value),
        "needs_printing": needs_printing,
        "listed_count": session.scalar(
            select(func.count(db.InventoryItem.id)).where(*mine, db.InventoryItem.listed == 1)
        )
        or 0,
        "host_label": request.url.netloc or "localhost",
        "user": user,
        "multi_user": settings.multi_user,
        "support_url": settings.support_url,
        "price_source": ", ".join(sources) if sources else "no catalogue",
        "synced_ago": _ago(synced),
    }


@app.get("/", response_class=HTMLResponse)
def page_landing(request: Request):
    """The front door. Deliberately not the tool: someone arriving cold needs to
    know what this is and that they can run it themselves before being handed a
    file picker."""
    return templates.TemplateResponse(
        request,
        "landing.html",
        {
            "nav": "landing",
            "version": __version__,
            "git_sha": settings.git_sha,
            "asset_v": _asset_version(),
            "support_url": settings.support_url,
            "multi_user": settings.multi_user,
            # The front door has to offer the door that is actually open. A
            # hosted deployment with registration closed must not invite people
            # to create an account they cannot have, and a self-hosted one has
            # no account to create at all.
            "registration_open": settings.allow_registration,
        },
    )


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
        .where(
            db.Scan.user_id == user_id,
            db.Scan.status.in_(("pending", "unmatched", "error")),
        )
        .order_by(db.Scan.id.desc())
        .limit(400)
    ).all()

    priced = inventory._prices_for(session, {c.card_id for s in scans for c in s.candidates})
    rows = []
    for scan in scans:
        top = scan.candidates[0] if scan.candidates else None
        needs_review = True  # nothing else is in this list any more
        alt = ""
        if scan.status == "error":
            alt = scan.error or "could not read this image"
        elif top is None:
            alt = "no match in the catalogue — is this game ingested?"
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
            alt = "low confidence — check the printing before committing"

        card = top.card if top else None
        rows.append(
            {
                "scan_id": scan.id,
                "filename": scan.filename,
                "needs_review": needs_review,
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
                "alternates": [
                    {
                        "card_id": c.card.id,
                        "name": c.card.name,
                        "variant": c.card.variant or "",
                        "pct": f"{c.score * 100:.0f}%",
                    }
                    for c in scan.candidates[1:]
                ],
            }
        )
    return rows


# ==========================================================================
# Accounts
# ==========================================================================


def _auth_page(
    request: Request,
    mode: str,
    *,
    next: str = "/app",
    error: str | None = None,
    email: str = "",
    status: int = 200,
):
    """The login/register screen. One builder rather than six copies of the
    same dict — the copies had drifted a hardcoded version string apiece."""
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "mode": mode,
            "next": next,
            "error": error,
            "email": email,
            "registration_open": settings.allow_registration,
            "needs_invite": bool(settings.invite_code),
            "version": __version__,
            "git_sha": settings.git_sha,
            "asset_v": _asset_version(),
            "support_url": settings.support_url,
        },
        status_code=status,
    )


@app.get("/login", response_class=HTMLResponse)
def page_login(request: Request, next: str = "/app"):
    if not settings.multi_user:
        return RedirectResponse("/app", status_code=303)
    return _auth_page(request, "login", next=next)


@app.post("/login", response_class=HTMLResponse)
def do_login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form("/app"),
    session=Depends(db_session),
):
    if not settings.multi_user:
        return RedirectResponse("/app", status_code=303)

    ip = ratelimit.client_ip(request)
    account = auth.normalise_email(email)
    # Checked before the password is verified, so a refused attempt costs an
    # dictionary lookup rather than an argon2 hash. That is the difference
    # between a limit that protects the machine and one that is a way to load
    # it up.
    wait = max(_login_ip.check(ip), _login_account.check(account))
    if wait > 0:
        return _auth_page(
            request, "login", next=next, error=ratelimit.wait_message(wait), email=email, status=429
        )

    try:
        user = auth.authenticate(session, email, password)
    except auth.AuthError as exc:
        _login_ip.record(ip)
        _login_account.record(account)
        return _auth_page(request, "login", next=next, error=str(exc), email=email, status=400)

    # A success clears both budgets. Someone who mistyped their password four
    # times and then remembered it should not carry those four into the rest
    # of their day.
    _login_ip.reset(ip)
    _login_account.reset(account)
    auth.touch_login(session, user)
    response = RedirectResponse(_safe_next(next), status_code=303)
    auth.issue(request, response, settings, user.id)
    return response


@app.get("/register", response_class=HTMLResponse)
def page_register(request: Request):
    if not settings.multi_user:
        return RedirectResponse("/app", status_code=303)
    if not settings.allow_registration:
        return _auth_page(
            request, "login", error="registration is closed on this server", status=403
        )
    return _auth_page(request, "register")


@app.post("/register", response_class=HTMLResponse)
def do_register(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    invite: str = Form(""),
    session=Depends(db_session),
):
    if not settings.multi_user:
        return RedirectResponse("/app", status_code=303)
    if not settings.allow_registration:
        return _auth_page(
            request, "login", error="registration is closed on this server", status=403
        )

    ip = ratelimit.client_ip(request)
    wait = _register_ip.check(ip)
    if wait > 0:
        return _auth_page(
            request, "register", error=ratelimit.wait_message(wait), email=email, status=429
        )

    if settings.invite_code and not compare_digest(invite.strip(), settings.invite_code):
        # Counts against the budget: without that, the code itself is
        # guessable at whatever rate the network allows.
        _register_ip.record(ip)
        return _auth_page(
            request, "register", error="that invite code is not valid", email=email, status=403
        )

    try:
        user = auth.register(session, settings, email, password)
    except auth.AuthError as exc:
        _register_ip.record(ip)
        return _auth_page(request, "register", error=str(exc), email=email, status=400)

    response = RedirectResponse("/app", status_code=303)
    auth.issue(request, response, settings, user.id)
    return response


@app.post("/logout")
def do_logout(request: Request, session=Depends(db_session)):
    # Drop the activity log with the session. It is small and it is ephemeral,
    # but on a shared machine "sign out" has to mean the next person to use
    # this browser cannot read what the last one imported or exported.
    user = auth.current_user(request, session, settings)
    if user is not None:
        joblog.forget(user.id)
    response = RedirectResponse("/", status_code=303)
    auth.clear(response)
    return response


def _safe_next(target: str) -> str:
    """Only ever redirect within this site.

    `?next=` is attacker-controlled, and a login form that will forward to
    `//evil.example` after a successful sign-in is a phishing primitive with
    our domain in the address bar.
    """
    if target.startswith("/") and not target.startswith("//"):
        return target
    return "/app"


# ==========================================================================
# Screens
# ==========================================================================


@app.get("/app", response_class=HTMLResponse)
def page_import(
    request: Request,
    filter: str = "all",
    session=Depends(db_session),
    user: db.User = Depends(owner),
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
            "max_images": int(os.getenv("FOILSTACK_MAX_IMAGES", "5000")),
            "max_mb": settings.max_archive_mb,
            "conditions": inventory.CONDITIONS,
            **_chrome(session, request, user),
        },
    )


@app.get("/matches")
def page_matches():
    """The review queue lives on the import screen now, beside what produced
    it. Kept as a redirect because it was a bookmarkable URL."""
    return RedirectResponse("/app?filter=review", status_code=307)


@app.get("/inventory", response_class=HTMLResponse)
def page_inventory(
    request: Request,
    q: str = "",
    rule: str = inventory.DEFAULT_RULE,
    show: str = "stock",
    session=Depends(db_session),
    user: db.User = Depends(owner),
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
            **_chrome(session, request, user),
        },
    )


@app.get("/inventory/{card_id}", response_class=HTMLResponse)
def page_card(
    card_id: int,
    request: Request,
    rule: str = inventory.DEFAULT_RULE,
    session=Depends(db_session),
    user: db.User = Depends(owner),
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
            **_chrome(session, request, user),
        },
    )


@app.get("/listings", response_class=HTMLResponse)
def page_listings(
    request: Request,
    id: list[int] | None = Query(None),
    rule: str = inventory.DEFAULT_RULE,
    channel: list[str] | None = Query(None),
    session=Depends(db_session),
    user: db.User = Depends(owner),
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
            **_chrome(session, request, user),
        },
    )


@app.get("/analytics", response_class=HTMLResponse)
def page_analytics(
    request: Request,
    session=Depends(db_session),
    user: db.User = Depends(owner),
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
            **_chrome(session, request, user),
        },
    )


@app.get("/plugins", response_class=HTMLResponse)
async def page_plugins(
    request: Request,
    session=Depends(db_session),
    user: db.User = Depends(owner),
):
    health = await encoder_health(settings.embedder_url)
    return templates.TemplateResponse(
        request,
        "plugins.html",
        {
            "nav": "plugins",
            "sources": source_plugins().values(),
            "exporters": export_plugins().values(),
            "encoder": health,
            "encoder_url": settings.embedder_url,
            "embed_model": settings.embed_model,
            **_chrome(session, request, user),
        },
    )


# ==========================================================================
# API
# ==========================================================================


@app.post("/api/import")
async def api_import(
    background: BackgroundTasks,
    archive: UploadFile = File(...),
    auto_accept: float = Form(None),
    default_condition: str = Form("NM"),
    default_finish: str = Form("nonfoil"),
    session=Depends(db_session),
    user: db.User = Depends(api_owner),
):
    if not (archive.filename or "").lower().endswith(".zip"):
        raise HTTPException(400, "expected a .zip archive")
    if default_condition not in inventory.CONDITIONS:
        raise HTTPException(400, "unknown condition")
    if default_finish not in inventory.FINISHES:
        raise HTTPException(400, "unknown finish")

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
                f"{settings.max_account_mb} MB limit — discard some scans first",
            )

    tmp = Path(tempfile.mkdtemp(prefix="foilstack-")) / "upload.zip"
    size = 0
    limit = settings.max_archive_mb * 1024 * 1024
    if settings.max_account_mb:
        limit = min(limit, ceiling - used)
    with open(tmp, "wb") as out:
        while chunk := await archive.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                out.close()
                shutil.rmtree(tmp.parent, ignore_errors=True)
                raise HTTPException(
                    413,
                    f"archive exceeds {limit / 1048576:.0f} MB "
                    "(the per-upload cap, or what is left of this account's quota)",
                )
            out.write(chunk)

    job = db.ImportJob(
        user_id=user.id,
        filename=archive.filename or "archive.zip",
        status="pending",
        auto_accept=auto_accept,
        default_condition=default_condition,
        default_finish=default_finish,
    )
    session.add(job)
    session.commit()

    joblog.add(user.id, f"POST /imports · {archive.filename} · {size / 1048576:.1f} MB")
    background.add_task(run_import, job.id, tmp, settings)
    return {"job_id": job.id}


@app.get("/api/jobs/{job_id}")
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


@app.post("/api/scans/{scan_id}/confirm")
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


@app.post("/api/scans/commit")
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


@app.post("/api/scans/{scan_id}/discard")
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


@app.post("/api/scans/discard-all")
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


@app.post("/api/listings/mark")
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


# Declared before the `/{item_id}` routes, and it has to stay there. FastAPI
# matches in declaration order, so with these the other way round a POST to
# /api/inventory/delete is matched by /api/inventory/{item_id}, "delete" fails
# to parse as an integer, and the endpoint answers 422 instead of running.
@app.post("/api/inventory/delete")
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
            "that sale and carries the cost basis behind your realised profit — "
            "delete those one at a time, from the card page, if you really mean to.",
        )

    count = len(items)
    _delete_items(session, items)
    session.commit()
    joblog.add(user.id, f"deleted {count} rows from inventory")
    return {"ok": True, "deleted": count}


@app.get("/api/inventory/{item_id}", response_class=HTMLResponse)
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
        },
    )


@app.post("/api/inventory/{item_id}")
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


@app.delete("/api/inventory/{item_id}")
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


# ==========================================================================
# Images and export
# ==========================================================================


@app.get("/export/{name}")
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


@app.get("/healthz", response_class=PlainTextResponse)
def healthz() -> str:
    """Liveness, and which build answered.

    The first line stays exactly `ok` so anything already watching this keeps
    working. The build line is what turns "is the deploy out" into one curl —
    the alternative is loading a page and reading a footer, which is how a
    service ran ten-hour-old code here without anyone noticing.
    """
    build = settings.git_sha or "unknown"
    return f"ok\nfoilstack {__version__} ({build})\n"

"""The web application.

Server-rendered, no build step, no framework on the client. A self-hosted tool
that needs npm before it will show you a page is a tool most people will never
see, and there is nothing here that justifies the cost.

The chrome follows the design mock: a fixed-height shell, a rail, and a status
line. Screens are panes that scroll independently, so the queue you are working
through stays on screen while you decide about one card.
"""

from __future__ import annotations

import logging
import mimetypes
from contextlib import asynccontextmanager

from fastapi import (
    Depends,
    FastAPI,
    Request,
)
from fastapi.responses import (
    HTMLResponse,
    PlainTextResponse,
)
from fastapi.staticfiles import StaticFiles

from foilstack import __version__, db
from foilstack.config import Settings, get_settings
from foilstack.embedding import encoder_health
from foilstack.plugins import export_plugins, source_plugins
from foilstack.web import auth, proof
from foilstack.web.chrome import (
    BASE_DIR,
    _asset_version,
    _chrome,
    templates,
)
from foilstack.web.deps import db_session, owner, settings_dep
from foilstack.web.routes import accounts, listings, media, scans
from foilstack.web.routes import inventory as inventory_routes

logger = logging.getLogger(__name__)


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
app.include_router(accounts.router)
app.include_router(scans.router)
app.include_router(inventory_routes.router)
app.include_router(listings.router)
app.include_router(media.router)

# Read once, at import, and used for exactly one thing: the size of the rate
# limiter windows below, which are a property of the process rather than of a
# request. Everything else takes `settings_dep` — see web/deps.py.
_boot = get_settings()


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Resolved here rather than read from a module global: startup is the one
    # place that genuinely wants the settings once, at boot.
    settings = get_settings()
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


@app.get("/", response_class=HTMLResponse)
def page_landing(
    request: Request,
    session=Depends(db_session),
    settings: Settings = Depends(settings_dep),
):
    """The front door. Deliberately not the tool: someone arriving cold needs to
    know what this is and that they can run it themselves before being handed a
    file picker.

    It does check for a session, though. Inviting somebody who is already
    signed in to create an account is the page admitting it has no idea who is
    reading it, and the one link they actually want — back into their
    inventory — is the one it does not offer.
    """
    viewer = auth.current_user(request, session, settings) if settings.multi_user else None
    return templates.TemplateResponse(
        request,
        "landing.html",
        {
            "nav": "landing",
            "viewer": viewer,
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
            # Looked up, never hardcoded: a card id is a row number in whichever
            # catalogue this instance built. Absent on a fresh install, and the
            # table simply renders without thumbnails.
            "proof_ids": proof.proof_card_ids(session),
        },
    )


# ==========================================================================
# Accounts
# ==========================================================================


# ==========================================================================
# Screens
# ==========================================================================


@app.get("/plugins", response_class=HTMLResponse)
async def page_plugins(
    request: Request,
    session=Depends(db_session),
    user: db.User = Depends(owner),
    settings: Settings = Depends(settings_dep),
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
            **_chrome(session, request, user, settings),
        },
    )


# ==========================================================================
# API
# ==========================================================================


# Declared before the `/{item_id}` routes, and it has to stay there. FastAPI
# matches in declaration order, so with these the other way round a POST to
# /api/inventory/delete is matched by /api/inventory/{item_id}, "delete" fails
# to parse as an integer, and the endpoint answers 422 instead of running.


# ==========================================================================
# Images and export
# ==========================================================================


@app.get("/healthz", response_class=PlainTextResponse)
def healthz(settings: Settings = Depends(settings_dep)) -> str:
    """Liveness, and which build answered.

    The first line stays exactly `ok` so anything already watching this keeps
    working. The build line is what turns "is the deploy out" into one curl —
    the alternative is loading a page and reading a footer, which is how a
    service ran ten-hour-old code here without anyone noticing.
    """
    build = settings.git_sha or "unknown"
    return f"ok\nfoilstack {__version__} ({build})\n"

"""A ceiling on how many bytes a request may send, enforced as they arrive.

Every upload route here already counted what it received — `api_import`
against the archive cap and the account's quota, the TCGplayer round trip
against its own — and every one of those counts ran after the bytes had
landed. FastAPI resolves a `File(...)` parameter before it calls the route,
and Starlette's multipart parser spools each file part to a temporary file
with no ceiling at all: its `max_part_size` is for ordinary form fields and
skips files by design. Measured, a 20 MB body sent against a 1 KB cap was
refused at 1 KB after all 20 MB had been written to `/tmp`, which in the
container is the disk the database lives on.

So this sits in front of the router and answers once, for every route, before
a parser sees the body: a declared `Content-Length` past the ceiling is refused
without reading any of it, and a body that declares none is counted as it
streams and cut off at the line.

The route checks stay, and are still the exact ones. This is the outer bound
on what may reach the disk at all; whose quota an upload spends is a question
for a route, because answering it needs a session this layer does not have.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from foilstack import importing, tcgplayer
from foilstack.config import Settings, get_settings

MB = 1024 * 1024

# Everything that is not an upload. The largest body a screen sends is the
# queue's Commit, one small object per scan in the open batch — about half a
# megabyte at the default image ceiling — so this is well clear of any honest
# request and still a ceiling. It needs to be one: every `Form(...)` route
# parses multipart too, and Starlette will hold a thousand one-megabyte fields
# in memory without complaint.
DEFAULT_BYTES = 8 * MB

# A multipart body is larger than the files in it. Every part carries a
# boundary and its own headers, a filename of up to 255 bytes among them, so
# an upload of exactly the promised size arrives over the promise — by more
# the more loose images it holds, since each one is its own part. A kilobyte a
# part is comfortably past what a browser writes. Without it the ceiling would
# refuse an honest upload for its framing, and break the size the import screen
# states by arithmetic.
PART_BYTES = 1024
# The settings sent beside the files: condition, finish, threshold, cohort.
FIELD_PARTS = 16


def _import_bytes(settings: Settings) -> int:
    return settings.max_archive_mb * MB + (importing.MAX_IMAGES + FIELD_PARTS) * PART_BYTES


def _tcgplayer_bytes(settings: Settings) -> int:
    return tcgplayer.MAX_UPLOAD_BYTES + FIELD_PARTS * PART_BYTES


# The routes that take a file, by path. Keyed by path because this runs before
# routing and there is no route object yet to ask — which means a rename goes
# stale here silently, and the upload falls to `DEFAULT_BYTES`. The suite checks
# every key against the application's real routes for that reason.
UPLOADS: dict[str, Callable[[Settings], int]] = {
    "/api/import": _import_bytes,
    "/export/tcgplayer/match": _tcgplayer_bytes,
}


def ceiling(method: str, path: str, settings: Settings) -> tuple[int, str]:
    """How many bytes this request may send, and what to say when it sends more.

    The import's refusal names `max_archive_mb` and not the ceiling itself,
    for the reason `extraction_ceiling` keeps its number in the log: the
    ceiling includes framing slack, and the seller was promised the other one.
    """
    if method == "POST" and path in UPLOADS:
        if path == "/api/import":
            message = (
                f"that upload is larger than the {settings.max_archive_mb} MB this "
                "site accepts. split it into smaller batches"
            )
        else:
            message = (
                f"that file is larger than {tcgplayer.MAX_UPLOAD_BYTES // MB} MB. "
                "filter the export to fewer sets and try again"
            )
        return UPLOADS[path](settings), message
    return DEFAULT_BYTES, "that request is larger than this site accepts"


def _declared_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", ()):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


class BodyLimit:
    """Refuse a request body past its ceiling before anything writes it down."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Per request, not at construction — see the note in web/deps.py.
        limit, message = ceiling(scope["method"], scope["path"], get_settings())

        declared = _declared_length(scope)
        if declared is not None and declared > limit:
            await JSONResponse({"detail": message}, status_code=413)(scope, receive, send)
            return

        received = 0

        async def counted() -> Message:
            nonlocal received
            event = await receive()
            if event["type"] == "http.request":
                received += len(event.get("body", b""))
                if received > limit:
                    # Raised from inside whatever is reading the body. FastAPI
                    # re-raises an HTTPException from body parsing untouched
                    # rather than folding it into its generic 400, so this
                    # reaches the seller as the 413 it is.
                    raise HTTPException(413, message)
            return event

        await self.app(scope, counted, send)

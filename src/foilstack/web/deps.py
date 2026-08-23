"""The dependencies every route shares.

Here rather than in app.py so a route module can take them without importing
the application object and creating a cycle.

`settings` is read once at import, which is the same thing app.py does and is
fine in production — the process reads its environment at boot and never
changes it. It is a trap in tests: a module imported before a fixture points
the application at a throwaway database keeps the first settings object it
saw, and the failure that follows names a password rather than an ordering.
Making settings something a route is *handed* would fix that properly, and is
a change worth making on its own rather than in passing.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from foilstack import db
from foilstack.config import get_settings
from foilstack.web import auth

settings = get_settings()


def db_session():
    """One session per request, always closed."""
    session = db.session()
    try:
        yield session
    finally:
        session.close()


def owner(request: Request, session=Depends(db_session)) -> db.User:
    """The account this request acts as.

    In single-user mode this is the local owner and never fails. In multi-user
    mode it redirects to the login screen. Every route that touches a seller's
    work depends on it, so there is no way to reach one of those queries
    without an id to scope it by.
    """
    return auth.require_user(request, session, settings)


def api_owner(request: Request, session=Depends(db_session)) -> db.User:
    """Same, but answering 401 instead of redirecting.

    A fetch() that follows a 303 to the login page succeeds with an HTML body,
    and the caller reports "saved" for a request that saved nothing.
    """
    user = auth.current_user(request, session, settings)
    if user is None:
        raise HTTPException(401, "sign in required")
    return user

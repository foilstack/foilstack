"""The dependencies every route shares.

Here rather than in app.py so a route module can take them without importing
the application object and creating a cycle.

Settings are resolved per call rather than bound at import. `get_settings` is
cached, so this costs a dict lookup — and it means `get_settings.cache_clear()`
actually takes effect here. Bound at import, it does not: a module imported
before a fixture points the application at a throwaway database keeps the
first settings object it ever saw, and every test then fails against the
developer's own database with an error that names a password rather than an
ordering. That cost hours, twice.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from foilstack import db
from foilstack.config import get_settings
from foilstack.web import auth


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
    return auth.require_user(request, session, get_settings())


def api_owner(request: Request, session=Depends(db_session)) -> db.User:
    """Same, but answering 401 instead of redirecting.

    A fetch() that follows a 303 to the login page succeeds with an HTML body,
    and the caller reports "saved" for a request that saved nothing.
    """
    user = auth.current_user(request, session, get_settings())
    if user is None:
        raise HTTPException(401, "sign in required")
    return user

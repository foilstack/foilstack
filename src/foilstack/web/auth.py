"""Accounts, sessions, and the single-user escape hatch.

Two modes, one query shape. In multi-user mode a visitor logs in and their
account id scopes everything they see. In single-user mode — the default, and
what a self-hoster gets — there is still exactly one `users` row and every
query is still scoped to it; the login screen is simply never shown.

That choice is the point. The alternative, a nullable owner meaning "mine",
puts a branch in front of every read, and the day someone forgets one is the
day a hosted seller sees another seller's cards.
"""

from __future__ import annotations

import logging
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import HTTPException, Request, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import func, select

from foilstack import db
from foilstack.config import Settings

logger = logging.getLogger(__name__)

COOKIE_NAME = "fs_session"
# Thirty days. Long enough that a shop does not log in every morning, short
# enough that a laptop left in a drawer stops being a key eventually.
MAX_AGE = 60 * 60 * 24 * 30

# The account a single-user install owns everything as.
LOCAL_EMAIL = "local@foilstack.invalid"

INSECURE_SECRET = "dev-insecure-change-me"

_hasher = PasswordHasher()

# The shortest password worth enforcing. Long enough to rule out `1234`, short
# enough that nobody reaches for a sticky note; complexity rules are left out
# deliberately, since they push people toward `Passw0rd!` and no further.
MIN_PASSWORD = 10


class AuthError(Exception):
    """A login or registration the user can fix, with a message they can read."""


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored: str, password: str) -> bool:
    try:
        _hasher.verify(stored, password)
    except (VerifyMismatchError, InvalidHashError):
        return False
    return True


def normalise_email(email: str) -> str:
    """Lowercased and stripped.

    Two accounts differing only in capitalisation are one person to everybody
    except the login form, and that mismatch reads to them as a lost inventory.
    """
    return email.strip().lower()


def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key, salt="foilstack-session")


def check_secret(settings: Settings) -> None:
    """Refuse to run multi-user with the published development key.

    The cookie is signed, not encrypted, so anyone holding the signing key can
    mint a session for any account. A default committed to a public repository
    is a key everyone holds.
    """
    if settings.multi_user and settings.secret_key == INSECURE_SECRET:
        raise RuntimeError(
            "FOILSTACK_MULTI_USER is on but FOILSTACK_SECRET_KEY is still the "
            "development default. Set it to a random value "
            "(`python -c 'import secrets; print(secrets.token_urlsafe(48))'`) "
            "before exposing this to anyone."
        )


def issue(request: Request, response: Response, settings: Settings, user_id: int) -> None:
    """Set the session cookie.

    `secure` follows the scheme this request actually arrived on, not a
    setting. Hard-coding it on breaks every plain-HTTP deployment in the most
    confusing way available — the login succeeds, the browser stores the
    cookie, refuses to send it back, and the user is bounced to the login form
    again with no error anywhere. Hard-coding it off would ship the session
    token in clear text over the network.

    Behind a TLS-terminating proxy the scheme is only correct if the proxy's
    `X-Forwarded-Proto` is honoured — uvicorn needs `--proxy-headers` for that,
    which is set in the compose command.
    """
    token = _serializer(settings).dumps({"uid": user_id})
    response.set_cookie(
        COOKIE_NAME, token,
        max_age=MAX_AGE,
        httponly=True,      # the session this carries is not script-readable
        samesite="lax",     # blocks the cross-site POST, keeps ordinary links working
        secure=request.url.scheme == "https",
        path="/",
    )


def clear(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def _session_user_id(request: Request, settings: Settings) -> int | None:
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        return None
    try:
        data = _serializer(settings).loads(raw, max_age=MAX_AGE)
    except BadSignature:
        return None
    uid = data.get("uid") if isinstance(data, dict) else None
    return int(uid) if isinstance(uid, int) else None


def local_owner(session) -> db.User:
    """The implicit account a single-user install runs as, created on demand.

    Its password hash is unusable rather than empty: the row must never be a
    way in if the same database is later switched to multi-user.
    """
    user = session.scalar(select(db.User).where(db.User.email == LOCAL_EMAIL))
    if user is None:
        user = db.User(email=LOCAL_EMAIL, password_hash="!", is_active=True)
        session.add(user)
        session.commit()
    return user


def current_user(request: Request, session, settings: Settings) -> db.User | None:
    if not settings.multi_user:
        return local_owner(session)
    uid = _session_user_id(request, settings)
    if uid is None:
        return None
    user = session.get(db.User, uid)
    if user is None or not user.is_active:
        return None
    return user


def require_user(request: Request, session, settings: Settings) -> db.User:
    """The signed-in account, or a redirect to the login screen.

    Raises `HTTPException(303)` rather than 401 so a browser follows it; API
    routes catch the same condition and answer 401 themselves.
    """
    user = current_user(request, session, settings)
    if user is None:
        raise HTTPException(
            status_code=303, detail="sign in required",
            headers={"Location": "/login"},
        )
    return user


def register(session, settings: Settings, email: str, password: str) -> db.User:
    email = normalise_email(email)
    if "@" not in email or len(email) < 3:
        raise AuthError("that does not look like an email address")
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"password must be at least {MIN_PASSWORD} characters")

    existing = session.scalar(
        select(db.User).where(func.lower(db.User.email) == email)
    )
    if existing is not None:
        # Deliberately the same wording the login form gives a wrong password.
        # Saying "that address is taken" turns this form into a way to test
        # whether someone has an account here.
        raise AuthError("could not create that account")

    user = db.User(email=email, password_hash=hash_password(password))
    session.add(user)
    session.commit()
    return user


def authenticate(session, email: str, password: str) -> db.User:
    email = normalise_email(email)
    user = session.scalar(select(db.User).where(func.lower(db.User.email) == email))
    if user is None or not user.is_active or not verify_password(user.password_hash, password):
        # One message for "no such account" and "wrong password" both, so the
        # form cannot be used to enumerate who has signed up.
        raise AuthError("email or password is incorrect")
    return user


def touch_login(session, user: db.User) -> None:
    import datetime as dt

    user.last_login_at = dt.datetime.now(dt.timezone.utc)
    session.commit()

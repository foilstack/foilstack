"""Signing in, signing up, signing out.

Split from app.py because it shares nothing with the screens behind it: no
inventory maths, no page chrome beyond the auth card's own template. What it
does own is the rate limiters, which live here rather than in app.py now that
these are the only routes that touch them.

Everything here is reachable by a stranger, which is the whole reason the
limiters exist: argon2 is expensive on purpose, so an unlimited login form is
also a way to burn the machine's CPU for free.
"""

from __future__ import annotations

from hmac import compare_digest

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from foilstack import __version__
from foilstack.config import Settings, get_settings
from foilstack.web import auth, joblog, ratelimit
from foilstack.web.chrome import _asset_version, templates
from foilstack.web.deps import db_session, settings_dep

router = APIRouter()

# Sized at import from the settings the process booted with. These windows are
# a property of the deployment, not of a request, so they do not move to the
# dependency.
_boot_limits = get_settings()

# Two budgets, both of which must allow an attempt through. The address budget
# stops one machine working through a password list; the account budget stops a
# botnet doing the same thing from a thousand addresses against one seller.
_login_ip = ratelimit.Limiter(_boot_limits.login_attempts, _boot_limits.login_window_s)
_login_account = ratelimit.Limiter(_boot_limits.login_attempts, _boot_limits.login_window_s)

# Registration is cheaper to abuse than login — every attempt that succeeds
# costs a row and a slot — so it gets a tighter budget on the address alone.
_register_ip = ratelimit.Limiter(
    max(3, _boot_limits.login_attempts // 2), _boot_limits.login_window_s
)


def _auth_page(
    request: Request,
    mode: str,
    settings: Settings,
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


@router.get("/login", response_class=HTMLResponse)
def page_login(
    request: Request,
    next: str = "/app",
    settings: Settings = Depends(settings_dep),
):
    if not settings.multi_user:
        return RedirectResponse("/app", status_code=303)
    return _auth_page(request, "login", settings, next=next)


@router.post("/login", response_class=HTMLResponse)
def do_login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form("/app"),
    session=Depends(db_session),
    settings: Settings = Depends(settings_dep),
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
            request,
            "login",
            settings,
            next=next,
            error=ratelimit.wait_message(wait),
            email=email,
            status=429,
        )

    try:
        user = auth.authenticate(session, email, password)
    except auth.AuthError as exc:
        _login_ip.record(ip)
        _login_account.record(account)
        return _auth_page(
            request, "login", settings, next=next, error=str(exc), email=email, status=400
        )

    # A success clears both budgets. Someone who mistyped their password four
    # times and then remembered it should not carry those four into the rest
    # of their day.
    _login_ip.reset(ip)
    _login_account.reset(account)
    auth.touch_login(session, user)
    response = RedirectResponse(_safe_next(next), status_code=303)
    auth.issue(request, response, settings, user.id)
    return response


@router.get("/register", response_class=HTMLResponse)
def page_register(request: Request, settings: Settings = Depends(settings_dep)):
    if not settings.multi_user:
        return RedirectResponse("/app", status_code=303)
    if not settings.allow_registration:
        return _auth_page(
            request, "login", settings, error="registration is closed on this server", status=403
        )
    return _auth_page(request, "register", settings)


@router.post("/register", response_class=HTMLResponse)
def do_register(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    invite: str = Form(""),
    session=Depends(db_session),
    settings: Settings = Depends(settings_dep),
):
    if not settings.multi_user:
        return RedirectResponse("/app", status_code=303)
    if not settings.allow_registration:
        return _auth_page(
            request, "login", settings, error="registration is closed on this server", status=403
        )

    ip = ratelimit.client_ip(request)
    wait = _register_ip.check(ip)
    if wait > 0:
        return _auth_page(
            request,
            "register",
            settings,
            error=ratelimit.wait_message(wait),
            email=email,
            status=429,
        )

    if settings.invite_code and not compare_digest(invite.strip(), settings.invite_code):
        # Counts against the budget: without that, the code itself is
        # guessable at whatever rate the network allows.
        _register_ip.record(ip)
        return _auth_page(
            request,
            "register",
            settings,
            error="that invite code is not valid",
            email=email,
            status=403,
        )

    try:
        user = auth.register(session, settings, email, password)
    except auth.AuthError as exc:
        _register_ip.record(ip)
        return _auth_page(request, "register", settings, error=str(exc), email=email, status=400)

    response = RedirectResponse("/app", status_code=303)
    auth.issue(request, response, settings, user.id)
    return response


@router.post("/logout")
def do_logout(
    request: Request,
    session=Depends(db_session),
    settings: Settings = Depends(settings_dep),
):
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

import contextvars

import bcrypt
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqladmin.authentication import AuthenticationBackend
from starlette.requests import Request

from shofar_automation.admin_models import engine, StaffUser, staff_user_units_table

# ---- Auth ----
#
# authenticate_staff_user() is the single source of truth for turning a
# username/password into a session dict. It's used both by SQLAdmin's own
# AdminAuth backend below and by the /login page in web/auth.py that fronts
# the bulk-campaign UI - so there's one login, one session shape, and staff
# only ever have to sign in once to reach either part of the app.
#
# NOTE: this previously queried a `StaffUserUnit` class that was
# never defined anywhere (only the `staff_user_units_table` Core
# Table existed) - every login attempt raised a NameError. Fixed here by
# querying the Core Table directly via its `.c` column collection.

import secrets

# A hash of a random, never-used password. Verified against on login
# attempts for a username that doesn't exist, so a bcrypt comparison always
# runs either way and response time can't be used to enumerate valid
# usernames.
_DUMMY_HASH = bcrypt.hashpw(secrets.token_urlsafe(32).encode(), bcrypt.gensalt()).decode()

def authenticate_staff_user(username: str, password: str) -> dict | None:
    """Returns a session-ready dict on success, or None on bad credentials
    / inactive user. Does not touch request.session itself, so callers
    (SQLAdmin's AdminAuth.login, the sole login path) can each decide when
    to commit it."""
    if not username or not password:
        return None

    with Session(engine) as session:
        user = session.execute(
            select(StaffUser).where(StaffUser.username == username, StaffUser.active == True)
        ).scalar_one_or_none()

        if not user or not user.password_hash:
            bcrypt.checkpw(password.encode(), _DUMMY_HASH.encode())
            return None

        if not bcrypt.checkpw(password.encode(), user.password_hash.encode()):
            return None

        unit_ids = [
            r[0] for r in session.execute(
                select(staff_user_units_table.c.unit_id).where(
                    staff_user_units_table.c.staff_user_id == user.id
                )
            ).all()
        ]

    return {
        "user_id": user.id,
        "username": user.username,
        "is_superadmin": bool(user.is_superadmin),
        "unit_ids": unit_ids,
    }


# Populated in AdminAuth.authenticate() on every request; read by
# ScopedModelView.scaffold_form() (admin_scoping.py) to filter relationship
# dropdowns (e.g. the unit picker) down to the current user's scope.
# scaffold_form has no `request` param, so a contextvar is the only
# way to pass this through.
#
# Concurrency note: under normal uvicorn dispatch, each HTTP request -
# including subsequent requests on a reused keep-alive connection - runs
# in its own asyncio.Task, and contextvars.Context is copied fresh at task
# creation. So current_scope.set() in one request's task is never visible
# to a concurrently-running request's task; cross-user leakage of the kind
# "user A's scope answers user B's dropdown query" is not possible here.
#
# What IS worth guarding against: a set() with no matching reset() means
# the value lives until something else overwrites it - fine today because
# authenticate() runs unconditionally before anything reads current_scope,
# but fragile against a future early-return branch (e.g. a cached-session
# fast path) being added above the .set() call. ScopeCleanupMiddleware
# below closes that gap by resetting the var to its default at the end of
# every request, regardless of which code path (if any) set it.
#
# IMPORTANT: this must stay a single process-wide instance - always import
# it from here, never redefine it in another module.
current_scope: contextvars.ContextVar[tuple[bool, list[int]] | None] = (
    contextvars.ContextVar("current_scope", default=None)
)


class ScopeCleanupMiddleware:
    """Plain ASGI middleware - deliberately NOT starlette.middleware.base.
    BaseHTTPMiddleware, which runs the downstream app in a separate task
    spawned via an anyio task group. ContextVar writes made in that child
    task (i.e. inside AdminAuth.authenticate()) don't propagate back to
    the middleware's own context, and calling .reset(token) from a
    different context than the one that produced the token raises
    ValueError: "Token was created in a different Context".

    A plain ASGI middleware calls self.app(scope, receive, send) directly
    in the same task, so the set() in authenticate() and the reset() here
    share one context and this is safe.

    Register by wrapping the app directly (app = ScopeCleanupMiddleware(app))
    rather than via app.add_middleware(), and make sure this wraps
    everything SQLAdmin touches - i.e. apply it before setup_admin(app)
    is called, since setup_admin(app) must remain the last line in
    main.py per the Starlette root-mount shadowing constraint.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        token = current_scope.set(None)
        try:
            await self.app(scope, receive, send)
        finally:
            current_scope.reset(token)


class AdminAuth(AuthenticationBackend):
    async def login(self, request: Request):
        # This is now the ONLY login path in the app (see web/auth.py -
        # the old separate /login page was removed once SQLAdmin got
        # mounted at the site root, since both would otherwise fight over
        # the same "/login" path). Brute-force lockout, previously in the
        # old page's own submit handler, moved here so it isn't lost.
        from shofar_automation.web import login_security

        form = await request.form()
        username = (form.get("username") or "").strip()
        password = form.get("password") or ""

        ip = login_security.get_client_ip(request)
        ukey = login_security.username_key(username)
        ikey = login_security.ip_key(ip)

        for key in (ukey, ikey):
            if login_security.check_lockout(key) is not None:
                login_security.log_event("LOGIN_LOCKED", ip, username, identifier=key)
                return False  # SQLAdmin shows a generic "Invalid credentials." either way

        session_data = authenticate_staff_user(username, password) if username and password else None
        if session_data is None:
            login_security.record_failed_attempt(ukey)
            login_security.record_failed_attempt(ikey)
            login_security.log_event("LOGIN_FAILED", ip, username)
            return False

        login_security.clear_attempts(ukey)
        login_security.clear_attempts(ikey)
        login_security.log_event("LOGIN_SUCCESS", ip, username)
        request.session.update(session_data)

        # Returning a Response here (rather than True) is honored as-is by
        # SQLAdmin's login route instead of its own default redirect to
        # the admin index - lands people on the campaign dashboard, which
        # is the more useful landing page for most staff.
        from starlette.responses import RedirectResponse
        return RedirectResponse(url="/campaigns", status_code=302)

    async def logout(self, request: Request) -> bool:
        request.session.clear()
        return True

    async def authenticate(self, request: Request) -> bool:
        if "user_id" not in request.session:
            return False
        current_scope.set((
            request.session.get("is_superadmin", False),
            request.session.get("unit_ids", []),
        ))
        return True

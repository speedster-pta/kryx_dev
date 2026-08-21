"""
storage/email_verification.py

Token issue/consume for confirming a self-serve signup's email address
(see schema.py's email_verification_tokens table docstring and
web/signup_router.py's /signup/verify route). Verifying doesn't gate login
or payment - it's a second, independent condition checked alongside a
successful payment before storage.organisations.activate_organisation() is
actually called (see storage.organisations.is_org_email_verified).
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from ._db import _connect

TOKEN_TTL_HOURS = 24


def create_email_verification_token(user_id: int) -> str:
    """Invalidates any previous unused token for this user first, so a
    resend can't leave two simultaneously-valid links outstanding."""
    now = datetime.now(timezone.utc)
    token = secrets.token_urlsafe(32)
    expires_at = (now + timedelta(hours=TOKEN_TTL_HOURS)).isoformat()
    with _connect() as conn:
        conn.execute(
            "UPDATE email_verification_tokens SET used_at = ? WHERE user_id = ? AND used_at IS NULL",
            (now.isoformat(), user_id),
        )
        conn.execute(
            "INSERT INTO email_verification_tokens (user_id, token, expires_at, created_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, token, expires_at, now.isoformat()),
        )
    return token


def consume_email_verification_token(token: str) -> int | None:
    """Marks the token used and returns its user_id if it was valid
    (exists, unused, unexpired) - returns None without side effects
    otherwise, so an already-used or expired token can't be replayed."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT user_id, expires_at, used_at FROM email_verification_tokens WHERE token = ?",
            (token,),
        ).fetchone()
        if not row:
            return None
        user_id, expires_at, used_at = row
        if used_at is not None:
            return None
        if datetime.fromisoformat(expires_at) < datetime.now(timezone.utc):
            return None
        conn.execute(
            "UPDATE email_verification_tokens SET used_at = ? WHERE token = ?",
            (datetime.now(timezone.utc).isoformat(), token),
        )
        return user_id


def mark_email_verified(user_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET email_verified_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), user_id),
        )

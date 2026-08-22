"""Account-management endpoints for the logged-in staff user (self-service
password change, etc.) - separate from auth.py, which only provides the
get_current_web_user session dependency, not endpoints.

Split out of campaigns_router.py, which this never belonged in.
"""
from fastapi import APIRouter, Depends, Form, HTTPException, Request

import bcrypt

from autosend import storage
from autosend.integrations import mailer
from autosend.password_policy import validate_password_strength
from autosend.utils.logging import get_logger
from autosend.web import login_security
from autosend.web.auth import get_current_web_user

logger = get_logger(__name__)

router = APIRouter()


@router.post("/api/account/password")
def change_own_password(current_password: str = Form(...), new_password: str = Form(...),
                         user: dict = Depends(get_current_web_user)):
    try:
        validate_password_strength(new_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    account = storage.get_user_by_id(user["id"])
    if not account or not account["password_hash"] or not bcrypt.checkpw(
        current_password.encode(), account["password_hash"].encode()
    ):
        raise HTTPException(status_code=401, detail="Current password is incorrect")

    new_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    storage.update_staff_password(user["id"], new_hash)

    return {"status": "password_changed"}


@router.post("/api/account/username")
def change_own_username(request: Request, new_username: str = Form(...),
                         user: dict = Depends(get_current_web_user)):
    new_username = new_username.strip()
    if not new_username:
        raise HTTPException(status_code=400, detail="Username cannot be empty")

    if new_username != user["username"] and storage.get_user(new_username):
        raise HTTPException(status_code=409, detail=f"Username '{new_username}' is already taken")

    storage.update_staff_username(user["id"], new_username)
    # users.username is unique, so the session's cached copy (set at login,
    # read back by get_current_web_user) would otherwise show the old name
    # for the rest of this session.
    request.session["username"] = new_username

    return {"status": "username_changed", "username": new_username}


@router.post("/api/account/email")
def change_own_email(request: Request, new_email: str = Form(...),
                      user: dict = Depends(get_current_web_user)):
    """Notifications and invoices go out per org-admin (each org-admin has
    their own email), not to one shared organisation address - see
    storage/schema.py, there is no organisations.email column."""
    new_email = new_email.strip()
    if not new_email or "@" not in new_email:
        raise HTTPException(status_code=400, detail="Please enter a valid email address")

    account = storage.get_user_by_id(user["id"])
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    if new_email == account.get("email"):
        return {"status": "unchanged", "email": new_email}

    storage.update_staff_email(user["id"], new_email)

    token = storage.create_email_verification_token(user["id"])
    verify_url = f"{str(request.base_url).rstrip('/')}/signup/verify?token={token}"
    text_body, html_body = mailer.render_verification_email(verify_url)
    try:
        mailer.send_email(
            to_address=new_email,
            subject="Verify your email address",
            text_body=text_body,
            html_body=html_body,
        )
    except Exception:
        logger.exception("Failed to send verification email to %s", new_email)
        # The address is already saved - the user can retry via
        # /api/account/resend-verification, so this isn't a hard failure.

    return {"status": "email_changed", "email": new_email}


@router.post("/api/account/resend-verification")
def resend_verification_email(request: Request, user: dict = Depends(get_current_web_user)):
    """Rate-limited per-user, same lockout mechanism web/login_security.py
    already uses for /login and /signup (5 attempts / 15 min window / 15
    min lockout) - keyed by user id rather than IP/username since this is
    behind a session, not a public form."""
    account = storage.get_user_by_id(user["id"])
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    if account.get("email_verified_at"):
        return {"status": "already_verified"}
    if not account.get("email"):
        raise HTTPException(status_code=400, detail="No email address on file for this account")

    rkey = f"resend_verify:{account['id']}"
    if login_security.check_lockout(rkey) is not None:
        raise HTTPException(
            status_code=429, detail="Too many resend attempts. Please try again later."
        )
    login_security.record_failed_attempt(rkey)

    token = storage.create_email_verification_token(account["id"])
    verify_url = f"{str(request.base_url).rstrip('/')}/signup/verify?token={token}"
    text_body, html_body = mailer.render_verification_email(verify_url)
    try:
        mailer.send_email(
            to_address=account["email"],
            subject="Verify your email address",
            text_body=text_body,
            html_body=html_body,
        )
    except Exception:
        logger.exception("Failed to resend verification email to %s", account["email"])
        raise HTTPException(
            status_code=502, detail="Could not send the verification email - please try again shortly"
        )

    return {"status": "verification_sent"}

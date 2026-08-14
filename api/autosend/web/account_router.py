"""Account-management endpoints for the logged-in staff user (self-service
password change, etc.) - separate from auth.py, which only provides the
get_current_web_user session dependency, not endpoints.

Split out of campaigns_router.py, which this never belonged in.
"""
from fastapi import APIRouter, Depends, Form, HTTPException, Request

import bcrypt

from autosend import storage
from autosend.web.auth import get_current_web_user

router = APIRouter()

MIN_PASSWORD_LENGTH = 8


@router.post("/api/account/password")
def change_own_password(current_password: str = Form(...), new_password: str = Form(...),
                         user: dict = Depends(get_current_web_user)):
    if len(new_password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(status_code=400, detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters")

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

"""Account-management endpoints for the logged-in staff user (self-service
password change, etc.) - separate from auth.py, which only provides the
get_current_web_user session dependency, not endpoints.

Split out of campaigns_router.py, which this never belonged in.
"""
from fastapi import APIRouter, Depends, Form, HTTPException

import bcrypt

from shofar_automation import storage
from shofar_automation.web.auth import get_current_web_user

router = APIRouter()

MIN_PASSWORD_LENGTH = 8


@router.post("/api/account/password")
def change_own_password(current_password: str = Form(...), new_password: str = Form(...),
                         user: dict = Depends(get_current_web_user)):
    if len(new_password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(status_code=400, detail=f"Password must be at least {MIN_PASSWORD_LENGTH} characters")

    staff_user = storage.get_staff_user_by_id(user["id"])
    if not staff_user or not staff_user["password_hash"] or not bcrypt.checkpw(
        current_password.encode(), staff_user["password_hash"].encode()
    ):
        raise HTTPException(status_code=401, detail="Current password is incorrect")

    new_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    storage.update_staff_password(user["id"], new_hash)

    return {"status": "password_changed"}

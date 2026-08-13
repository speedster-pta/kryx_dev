"""Session dependency for the bulk-campaign API endpoints.

SQLAdmin (mounted at the site root, see admin.py) is now the only login
path in the app - its own /login, reskinned to match, handles
authentication (including brute-force lockout, folded into
admin.AdminAuth.login) and sets this same session. This module just reads
that session back out for the plain API endpoints in campaigns_router.py
that aren't behind SQLAdmin's own login_required (CSV upload, etc.).
"""
from fastapi import HTTPException, Request


def get_current_web_user(request: Request) -> dict:
    """Dependency for API endpoints under the bulk-campaign UI. Raises a
    303 to /login (handled by main.py's exception handler) if not signed
    in - the page-serving routes themselves (admin.CampaignsView,
    admin.AccountView) are already gated by SQLAdmin's own login_required,
    this is only reached by their supporting fetch()-based API calls."""
    if "user_id" not in request.session:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return {
        "id": request.session["user_id"],
        "username": request.session["username"],
        "is_superadmin": request.session["is_superadmin"],
        "unit_ids": request.session["unit_ids"],
    }

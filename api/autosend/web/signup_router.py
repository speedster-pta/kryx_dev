"""Public, logged-out self-serve organisation signup.

This is the ONLY way an organisation gets created outside a superadmin
action - there is no "create organisation" button anywhere in the
logged-in admin for an org admin (see admin_views.OrganisationAdmin's
docstring). Anyone can visit /signup, name a new organisation, and become
its first (and, until they add more, only) org admin - but once inside
that org, there's no path back to creating a second one from the same
session; the only way to get another org is to sign up again as a
different account.

Plain APIRouter (not a sqladmin BaseView) since it must be reachable
while logged out, unlike every BaseView page in admin_pages.py, which
sits behind SQLAdmin's own login_required. Registered in main.py
alongside the other plain routers, before setup_admin() - same ordering
constraint documented there (SQLAdmin's root Mount would otherwise
shadow it).
"""
import re
from pathlib import Path

import bcrypt
from fastapi import APIRouter, Form, Request
from fastapi.templating import Jinja2Templates
from starlette.responses import RedirectResponse

from autosend import storage

router = APIRouter()

# Own Jinja2Templates instance pointed at the same directory main.py uses -
# importing main.py's instance directly would be a circular import
# (main.py imports this router).
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "web" / "sqladmin_theme"))


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "org"


def _unique_org_slug(name: str) -> str:
    """organisations.slug is globally UNIQUE (unlike units.slug, which is
    only unique per-org) - so unlike UnitAdmin's _slugify(), signup needs
    to handle collisions itself by appending -2, -3, ... until free."""
    base = _slugify(name)
    slug = base
    suffix = 2
    while storage.get_organisation_by_slug(slug) is not None:
        slug = f"{base}-{suffix}"
        suffix += 1
    return slug


@router.get("/signup")
async def signup_page(request: Request):
    return templates.TemplateResponse(request, "signup.html", {"error": None})


@router.post("/signup")
async def signup_submit(
    request: Request,
    org_name: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
):
    def _error(message: str):
        return templates.TemplateResponse(
            request, "signup.html", {"error": message}, status_code=400
        )

    org_name = org_name.strip()
    username = username.strip()
    if not org_name or not username or not password:
        return _error("Organisation name, username, and password are all required.")
    if password != confirm_password:
        return _error("Passwords did not match.")
    if storage.get_user(username):
        return _error(f"Username '{username}' is already taken.")

    org = storage.create_organisation(org_name, _unique_org_slug(org_name))
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    user_id = storage.create_user(
        username, password_hash, org_id=org.id, is_org_admin=True,
    )

    request.session.update({
        "user_id": user_id,
        "username": username,
        "is_superadmin": False,
        "is_org_admin": True,
        "org_id": org.id,
        "unit_ids": [],
    })
    return RedirectResponse(url="/campaigns", status_code=303)

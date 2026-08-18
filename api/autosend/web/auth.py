"""Session dependency for the bulk-campaign API endpoints.

SQLAdmin (mounted at the site root, see admin.py) is now the only login
path in the app - its own /login, reskinned to match, handles
authentication (including brute-force lockout, folded into
admin.AdminAuth.login) and sets this same session. This module just reads
that session back out for the plain API endpoints in campaigns_router.py
that aren't behind SQLAdmin's own login_required (CSV upload, etc.).
"""
from fastapi import HTTPException, Request


def resolve_unit_ids(session: dict) -> list[int]:
    """Single choke point for "which unit ids can this session see" -
    plain staff get their explicit, session-stored assignment
    (user_units, set at login); an org admin's effective scope is
    "every unit in my org", resolved fresh from storage on every call
    (not cached in session) so a unit created mid-session is visible
    without a re-login. Superadmin callers should check is_superadmin
    first and skip calling this entirely (no unit filter at all), same as
    they already do everywhere this was previously read directly off the
    session."""
    if session.get("is_org_admin"):
        from autosend import storage

        return storage.get_unit_ids_for_org(session["org_id"])
    return session.get("unit_ids", [])


def pco_module_visible(request: Request) -> bool:
    """Single source of truth for "should this session see PCO-driven
    Automations UI right now" - used for the Automations nav link
    (registered as a Jinja global, see admin.py), AutomationsView's
    is_accessible/is_visible, and automations_router.py's dependency
    gate, so all three stay in lockstep instead of drifting.

    Superadmin always sees it (spans every org, same bypass pattern as
    unit scoping elsewhere) - everyone else is gated on whether their own
    org currently has the PCO module enabled."""
    if request.session.get("is_superadmin", False):
        return True
    org_id = request.session.get("org_id")
    if org_id is None:
        return False
    from autosend import storage

    return storage.is_enabled(org_id, storage.MODULE_PCO)


def email_wa_module_visible(request: Request) -> bool:
    """Same shape/purpose as pco_module_visible above, for the
    email-to-WhatsApp module (storage.MODULE_EMAIL_WA) - used for the
    Automations page's Email-to-WhatsApp section, the Email-to-WhatsApp
    Settings nav link/page, and web/email_wa_router.py's dependency gate.
    Deliberately a separate function rather than a parameterised one
    shared with pco_module_visible, matching that function's own
    docstring rationale for being a single, simple source of truth per
    module rather than a generic helper every caller has to parameterise
    correctly."""
    if request.session.get("is_superadmin", False):
        return True
    org_id = request.session.get("org_id")
    if org_id is None:
        return False
    from autosend import storage

    return storage.is_enabled(org_id, storage.MODULE_EMAIL_WA)


def org_active(request: Request) -> bool:
    """Jinja global (registered alongside pco_visible/email_wa_visible in
    admin.py) for showing an "organisation inactive" banner - superadmins
    have no owning org and are never blocked, same bypass as
    pco_module_visible/email_wa_module_visible above. This only reflects
    UI-visible status; the actual send-blocking enforcement lives at each
    send-triggering choke point (storage.is_org_active's own callers),
    not here."""
    if request.session.get("is_superadmin", False):
        return True
    org_id = request.session.get("org_id")
    from autosend import storage

    return storage.is_org_active(org_id)


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
        "is_org_admin": request.session.get("is_org_admin", False),
        "org_id": request.session.get("org_id"),
        "unit_ids": resolve_unit_ids(request.session),
    }

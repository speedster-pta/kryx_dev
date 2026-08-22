"""Session dependency for the bulk-campaign API endpoints.

SQLAdmin (mounted at the site root, see admin.py) is now the only login
path in the app - its own /login, reskinned to match, handles
authentication (including brute-force lockout, folded into
admin.AdminAuth.login) and sets this same session. This module just reads
that session back out for the plain API endpoints in campaigns_router.py
that aren't behind SQLAdmin's own login_required (CSV upload, etc.).
"""
from fastapi import HTTPException, Request

from autosend import storage


def resolve_unit_ids(session: dict) -> list[int]:
    """Single choke point for "which unit ids can this session see" -
    plain users get their explicit, session-stored assignment
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


def sme_metrics_module_visible(request: Request) -> bool:
    """Same shape/purpose as pco_module_visible above, for the SME
    Metrics module (storage.MODULE_SME_METRICS) - used for the
    Automations page's SME Metrics section, the SME Metrics Settings nav
    link/page, and web/sme_metrics_router.py's dependency gate.
    Deliberately a separate function rather than a parameterised one
    shared with pco_module_visible, matching that function's own
    docstring rationale for being a single, simple source of truth per
    module rather than a generic helper every caller has to parameterise
    correctly.

    Named email_wa_module_visible before SME Metrics was split into its
    own module (see storage/modules.py's MODULE_SME_METRICS docstring) -
    that name now belongs to the function below, for the new, unrelated,
    genuinely generic Email-to-WhatsApp module."""
    if request.session.get("is_superadmin", False):
        return True
    org_id = request.session.get("org_id")
    if org_id is None:
        return False
    from autosend import storage

    return storage.is_enabled(org_id, storage.MODULE_SME_METRICS)


def email_wa_module_visible(request: Request) -> bool:
    """Same shape/purpose as pco_module_visible above, for the new,
    from-scratch generic Email-to-WhatsApp module (storage.MODULE_EMAIL_WA) -
    used for the Automations page's Email-to-WhatsApp section, the
    Email-to-WhatsApp Settings nav link/page, and
    web/email_wa_router.py's dependency gate. Not to be confused with
    sme_metrics_module_visible above, which used to be this function
    before SME Metrics became its own module."""
    if request.session.get("is_superadmin", False):
        return True
    org_id = request.session.get("org_id")
    if org_id is None:
        return False
    from autosend import storage

    return storage.is_enabled(org_id, storage.MODULE_EMAIL_WA)


def ical_module_visible(request: Request) -> bool:
    """Same shape/purpose as pco_module_visible above, for the Calendar
    Invites module (storage.MODULE_ICAL) - used by admin_pages.TemplatesView
    to decide whether the WhatsApp template button builder should offer a
    "Calendar invite" preset that fills in the iCal feed's base URL."""
    if request.session.get("is_superadmin", False):
        return True
    org_id = request.session.get("org_id")
    if org_id is None:
        return False
    from autosend import storage

    return storage.is_enabled(org_id, storage.MODULE_ICAL)


def stitch_module_visible(request: Request) -> bool:
    """Same shape/purpose as pco_module_visible above, for the Stitch
    payments module (storage.MODULE_STITCH) - used by
    admin_pages.TemplatesView to decide whether the WhatsApp template
    button builder should offer the "Stitch payment link" preset, and by
    web/automations_router.py to decide whether a unit's Stitch
    credentials being active actually counts (an org that hasn't bought/
    enabled this module gets no Stitch functionality regardless of what
    a unit's own "Active" checkbox says - see
    services/registration_poller.py's send-time gate for the other half
    of this check)."""
    if request.session.get("is_superadmin", False):
        return True
    org_id = request.session.get("org_id")
    if org_id is None:
        return False
    from autosend import storage

    return storage.is_enabled(org_id, storage.MODULE_STITCH)


def visible_automation_modules(request: Request) -> list[dict]:
    """Single choke point for "which per-integration Automations nav
    entries should this session see, and in what order" - registered as
    a Jinja global (see admin.py/main.py) alongside pco_visible/
    sme_metrics_visible/email_wa_visible above, so layout.html doesn't
    have to hand-check each *_module_visible flag itself and grow a new
    branch every time an integration ships. Each entry points at that
    integration's own Automations page (admin_pages.AutomationsView)
    rather than the combined /automations route, which now only exists
    as a redirect for old links/bookmarks.

    layout.html uses the length of this list to decide the nav shape: 0
    entries hides the Automations nav item entirely, exactly 1 renders it
    as a single direct link (as before the per-integration split), and 2+
    renders it as a dropdown - this is also why a superadmin (who always
    sees every module, see pco_module_visible/sme_metrics_module_visible/
    email_wa_module_visible) is the case that will hit the dropdown
    soonest as more integrations are added."""
    # Alphabetical by label, same reasoning as storage.AVAILABLE_MODULES'
    # own ordering comment - this is UI order, not a meaningful priority.
    modules = []
    if email_wa_module_visible(request):
        modules.append({"key": "email-wa", "label": "Email-to-WhatsApp", "url": "/automations/email-wa"})
    if pco_module_visible(request):
        modules.append({"key": "pco", "label": "Planning Center", "url": "/automations/pco"})
    if sme_metrics_module_visible(request):
        modules.append({"key": "sme-metrics", "label": "SME Metrics", "url": "/automations/sme-metrics"})
    return modules


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


def email_verified(request: Request) -> bool:
    """Jinja global (registered alongside org_active above) for showing a
    "please verify your email" banner - superadmins have no signup email
    to verify and are never blocked, same bypass as org_active. Purely
    informational: verifying doesn't block login or sending, only whether
    billing/engine.py's payment-success handlers are allowed to flip
    is_org_active (see storage.organisations.is_org_email_verified)."""
    if request.session.get("is_superadmin", False):
        return True
    user_id = request.session.get("user_id")
    if user_id is None:
        return True
    from autosend import storage

    user = storage.get_user_by_id(user_id)
    return bool(user and user.get("email_verified_at"))


def get_current_web_user(request: Request) -> dict:
    """Dependency for API endpoints under the bulk-campaign UI. Raises a
    303 to /login (handled by main.py's exception handler) if not signed
    in - the page-serving routes themselves (admin.CampaignsView,
    admin.AccountView) are already gated by SQLAdmin's own login_required,
    this is only reached by their supporting fetch()-based API calls."""
    if "user_id" not in request.session:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    # email resolved fresh from storage on every call (same reasoning as
    # resolve_unit_ids below - it's a login-time detail that can change,
    # e.g. via AccountView, without a re-login), rather than cached in
    # the session at login time.
    user_row = storage.get_user_by_id(request.session["user_id"])
    return {
        "id": request.session["user_id"],
        "username": request.session["username"],
        "email": (user_row or {}).get("email"),
        "email_verified_at": (user_row or {}).get("email_verified_at"),
        "is_superadmin": request.session["is_superadmin"],
        "is_org_admin": request.session.get("is_org_admin", False),
        "org_id": request.session.get("org_id"),
        "unit_ids": resolve_unit_ids(request.session),
    }

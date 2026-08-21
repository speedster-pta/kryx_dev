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

A signup also fires off a best-effort email-verification link (see
_send_verification_email and the /signup/verify route below). Verifying
does NOT block login or payment - the new org-admin is logged in and can
go straight through Paystack checkout regardless. It only gates whether
billing/engine.py's payment-success handlers are allowed to actually flip
is_org_active (storage.is_org_email_verified) - see that module's
confirm_payment()/run_recurring_billing() for the other half of this.
"""
from pathlib import Path

import bcrypt
from fastapi import APIRouter, Form, Request
from fastapi.templating import Jinja2Templates
from starlette.responses import RedirectResponse

from autosend import storage
from autosend.integrations import mailer
from autosend.password_policy import validate_password_strength
from autosend.utils.logging import get_logger
from autosend.web import login_security

logger = get_logger(__name__)

router = APIRouter()

# Own Jinja2Templates instance pointed at the same directory main.py uses -
# importing main.py's instance directly would be a circular import
# (main.py imports this router).
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "web" / "sqladmin_theme"))


def _send_verification_email(request: Request, user_id: int, email: str) -> None:
    """Best-effort - a Mailtrap outage or unconfigured platform_email_settings
    must never block a signup that's otherwise already succeeded (the user
    is already created and logged in by the time this runs). The user can
    always get a fresh link later via POST /api/account/resend-verification
    (web/account_router.py)."""
    token = storage.create_email_verification_token(user_id)
    verify_url = f"{str(request.base_url).rstrip('/')}/signup/verify?token={token}"
    text_body, html_body = mailer.render_verification_email(verify_url, welcome=True)
    try:
        mailer.send_email(
            to_address=email,
            subject="Verify your email address",
            text_body=text_body,
            html_body=html_body,
        )
    except Exception:
        logger.exception("Failed to send signup verification email to %s", email)


@router.get("/signup")
async def signup_page(request: Request):
    return templates.TemplateResponse(request, "signup.html", {"error": None})


@router.post("/signup")
async def signup_submit(
    request: Request,
    org_name: str = Form(...),
    username: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
):
    def _error(message: str, status_code: int = 400):
        return templates.TemplateResponse(
            request, "signup.html", {"error": message}, status_code=status_code
        )

    ip = login_security.get_client_ip(request)
    ikey = login_security.signup_ip_key(ip)

    # Every submission counts, not just failed ones - unlike /login there's
    # no legitimate reason to resubmit this form repeatedly in a short
    # window, so this is a volume limiter rather than a credential guard.
    if login_security.check_lockout(ikey) is not None:
        login_security.log_event("SIGNUP_LOCKED", ip, username, org_name=org_name)
        return _error(
            "Too many signup attempts from this network. Please try again later.",
            status_code=429,
        )
    login_security.record_failed_attempt(ikey)

    org_name = org_name.strip()
    username = username.strip()
    email = email.strip()
    if not org_name or not username or not email or not password:
        return _error("Organisation name, username, email, and password are all required.")
    if password != confirm_password:
        return _error("Passwords did not match.")
    try:
        validate_password_strength(password)
    except ValueError as exc:
        return _error(str(exc))
    if storage.get_user(username):
        return _error(f"Username '{username}' is already taken.")

    # active=False: this is a paid product, so a public self-serve signup
    # can configure numbers/integrations straight away but can't send
    # until a superadmin activates the org (see storage.is_org_active).
    org = storage.create_organisation(org_name, storage.generate_unique_slug(org_name), active=False)
    login_security.log_event("SIGNUP_SUCCESS", ip, username, org_name=org_name)
    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    user_id = storage.create_user(
        username, password_hash, org_id=org.id, is_org_admin=True, email=email,
    )
    # create_organisation() provisions exactly one unit ("Main") in the same
    # transaction, so this is always that unit - explicit user_units row so
    # the first user shows up as unit staff, even though org-admin scope
    # already resolves to every unit in the org regardless (web/auth.py::resolve_unit_ids).
    main_unit_ids = storage.get_unit_ids_for_org(org.id)
    if main_unit_ids:
        storage.assign_staff_unit(user_id, main_unit_ids[0])

    _send_verification_email(request, user_id, email)

    request.session.update({
        "user_id": user_id,
        "username": username,
        "is_superadmin": False,
        "is_org_admin": True,
        "org_id": org.id,
        "unit_ids": main_unit_ids,
    })
    # Straight to plan selection, not /campaigns - this is the pricing-page
    # step of the platform billing flow (see billing/engine.py): pick a
    # plan/add-ons/coupon, then get redirected to a real Paystack checkout.
    # The org is already active=False regardless, so nothing is lost by
    # visiting /campaigns first via "Skip for now" on that page - a
    # superadmin can also comp the org later without this step ever
    # completing (admin_org_pages.BillingDashboardView).
    return RedirectResponse(url="/signup/plan", status_code=303)


@router.get("/signup/plan")
async def signup_plan_page(request: Request):
    if "org_id" not in request.session:
        return RedirectResponse(url="/signup", status_code=303)
    plans = storage.list_plans()
    addons = storage.list_addons()
    return templates.TemplateResponse(
        request, "signup_plan.html", {"plans": plans, "addons": addons, "error": None}
    )


@router.get("/signup/verify")
async def signup_verify(request: Request, token: str):
    user_id = storage.consume_email_verification_token(token)
    if user_id is None:
        return templates.TemplateResponse(
            request, "signup_verify_result.html",
            {
                "success": False,
                "message": (
                    "That verification link is invalid or has expired. Log in and "
                    "use the \"Resend verification email\" link to get a new one."
                ),
            },
            status_code=400,
        )
    storage.mark_email_verified(user_id)
    user = storage.get_user_by_id(user_id)
    if user and user.get("org_id"):
        # A payment that already succeeded before this click couldn't flip
        # is_org_active on its own (billing/engine.py's two activation call
        # sites both require email verification too) - finish the job now
        # from this side if that's what happened.
        subscription = storage.get_subscription(user["org_id"])
        if subscription is not None and subscription.status == "active":
            storage.activate_organisation(user["org_id"])
    return templates.TemplateResponse(
        request, "signup_verify_result.html",
        {"success": True, "message": "Thanks - your email address is confirmed."},
    )

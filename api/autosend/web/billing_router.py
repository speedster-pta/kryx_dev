"""Org-admin-facing billing endpoints - current plan, add-ons, pending
downgrade, and the Paystack checkout/callback flow.

Session/org-scoping follows the same convention as every other org-scoped
web router in this codebase (see web/auth.py::get_current_web_user) -
org_id always comes from the session, never trusted from a client-posted
value. Superadmins have no owning org of their own (org_id is None in
their session) - these routes are for an org managing its own billing,
not a superadmin managing every org's (that's
admin_org_pages.BillingDashboardView's job), so a superadmin with no
org_id is treated the same as "not permitted" here rather than given a
free pass.
"""
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from autosend import storage
from autosend.billing import engine, entitlements
from autosend.web.auth import get_current_web_user

router = APIRouter(prefix="/billing", tags=["billing"])

# Own Jinja2Templates instance, same reasoning as web/signup_router.py's -
# pointed at the same theme directory main.py uses, but importing main.py's
# instance directly would be a circular import (main.py imports this router).
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "web" / "sqladmin_theme"))


def _require_org_id(user: dict) -> int:
    org_id = user.get("org_id")
    if org_id is None:
        raise HTTPException(status_code=403, detail="Not permitted - no organisation on this session")
    return org_id


@router.get("")
def billing_dashboard(user: dict = Depends(get_current_web_user)):
    org_id = _require_org_id(user)
    subscription = storage.get_subscription(org_id)
    plans = storage.list_plans()
    addons = storage.list_addons()

    active_addon_keys = (
        storage.list_active_addons_for_subscription(subscription.id) if subscription else []
    )
    # A 'capacity' add-on (extra seat/number/unit) can be bought more than
    # once - list_active_addons_for_subscription returns one entry per
    # active subscription_items row, so counting occurrences here gives
    # "how many of this add-on are currently active", not just whether
    # it's active at all.
    active_addon_counts: dict[str, int] = {}
    for key in active_addon_keys:
        active_addon_counts[key] = active_addon_counts.get(key, 0) + 1
    active_addons = []
    for addon in addons:
        if addon["key"] in active_addon_counts:
            active_addons.append({**addon, "quantity": active_addon_counts[addon["key"]]})

    current_plan = None
    pending_downgrade_plan = None
    if subscription:
        for plan in plans:
            if plan["id"] == subscription.plan_id:
                current_plan = plan
            if plan["id"] == subscription.pending_downgrade_plan_id:
                pending_downgrade_plan = plan

    return {
        "status": subscription.status if subscription else "no_subscription",
        "current_plan": current_plan,
        "active_addons": active_addons,
        "available_plans": plans,
        "available_addons": addons,
        "pending_downgrade_plan": pending_downgrade_plan,
        "pending_downgrade_effective_at": subscription.pending_downgrade_effective_at if subscription else None,
        "current_period_end": subscription.current_period_end if subscription else None,
        "cancel_at": subscription.cancel_at if subscription else None,
    }


@router.post("/addons/{addon_key}/add")
def add_addon(addon_key: str, user: dict = Depends(get_current_web_user)):
    org_id = _require_org_id(user)
    try:
        engine.add_addon(org_id, addon_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "added", "addon_key": addon_key}


@router.post("/messages/purchase")
async def purchase_messages(user: dict = Depends(get_current_web_user)):
    """One-time top-up of 1000 non-expiring messages - see
    billing/engine.py::purchase_message_addon. Deliberately a separate
    route from POST /billing/addons/{addon_key}/add: that route creates a
    recurring subscription_items row (engine.add_addon now rejects the
    'messages' add-on outright to keep the two paths from being confused),
    this one charges once and credits a persisted balance."""
    org_id = _require_org_id(user)
    try:
        amount_cents = await engine.purchase_message_addon(org_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "purchased", "amount_cents": amount_cents}


@router.post("/addons/{addon_key}/remove")
def remove_addon(addon_key: str, user: dict = Depends(get_current_web_user)):
    org_id = _require_org_id(user)
    try:
        engine.remove_addon(org_id, addon_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "removed", "addon_key": addon_key}


@router.post("/plan")
def change_plan(plan_key: str = Form(...), user: dict = Depends(get_current_web_user)):
    org_id = _require_org_id(user)
    try:
        engine.change_plan(org_id, plan_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "plan_changed", "plan_key": plan_key}


@router.post("/downgrade/cancel")
def cancel_downgrade(user: dict = Depends(get_current_web_user)):
    org_id = _require_org_id(user)
    try:
        engine.cancel_pending_downgrade(org_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "downgrade_cancelled"}


@router.post("/cancel")
def cancel_subscription(user: dict = Depends(get_current_web_user)):
    org_id = _require_org_id(user)
    try:
        engine.cancel_subscription(org_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "cancellation_scheduled"}


@router.post("/cancel/undo")
def undo_cancel_subscription(user: dict = Depends(get_current_web_user)):
    org_id = _require_org_id(user)
    try:
        engine.cancel_pending_cancellation(org_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"status": "cancellation_undone"}


@router.post("/subscribe")
async def subscribe(
    request: Request,
    plan_key: str = Form(...),
    addon_keys: str = Form(""),
    coupon_code: str | None = Form(None),
    user: dict = Depends(get_current_web_user),
):
    """Starts a fresh subscription checkout - addon_keys is a
    comma-separated list (kept as a plain form field rather than a repeated
    field, simplest shape for a form POST from the billing page)."""
    org_id = _require_org_id(user)
    # Paystack requires a real email address to initialize a transaction -
    # username isn't guaranteed to be one (see storage.users.get_user's
    # underlying schema), so this fails loudly rather than silently
    # sending Paystack a non-email string.
    email = user.get("email")
    if not email:
        raise HTTPException(
            status_code=400,
            detail="No email address on file - add one in Account settings before subscribing.",
        )
    keys = [k.strip() for k in addon_keys.split(",") if k.strip()]
    callback_url = str(request.base_url).rstrip("/") + "/billing/callback"

    try:
        checkout_url = await engine.start_subscription(
            org_id=org_id,
            plan_key=plan_key,
            addon_keys=keys,
            coupon_code=coupon_code or None,
            email=email,
            callback_url=callback_url,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {"checkout_url": checkout_url}


@router.get("/callback")
async def billing_callback(reference: str):
    """Paystack redirects the payer's browser here after checkout - ack
    then confirm inline (not background_tasks like the webhook handlers
    in integrations/webhooks.py, since this is a synchronous
    browser-facing redirect and confirm_payment is fast/idempotent, not a
    slow retry-prone external call chain worth acking ahead of).

    Redirects to the templated /billing/success page, not the plain
    GET /billing JSON API above - that endpoint is for the fetch()-based
    self-service billing UI, not a browser-facing landing page."""
    await engine.confirm_payment(reference)
    return RedirectResponse(url="/billing/success", status_code=303)


@router.get("/manage")
def billing_manage_page(request: Request, user: dict = Depends(get_current_web_user)):
    """The persistent, org-admin-facing billing management page - unlike
    /billing/success (a one-time post-payment landing page), this is
    reachable any time to change plan, add/remove add-ons, or cancel/
    un-cancel the subscription. Renders from the same GET "" JSON shape
    above rather than duplicating the query logic."""
    org_id = _require_org_id(user)
    data = billing_dashboard(user)
    active_addon_keys = {a["key"] for a in data["active_addons"]}
    # The 'messages' capacity add-on is a one-time top-up (see
    # billing/engine.py::purchase_message_addon), not a recurring
    # subscription_items add-on like seat/number/unit - it's excluded from
    # every list below and surfaced separately as `messages_addon` instead,
    # with its own "Buy 1000 more" button/route in the Messages card.
    messages_addon = next(
        (a for a in data["available_addons"] if a["kind"] == "capacity" and a.get("capacity_key") == "messages"),
        None,
    )
    # 'capacity' add-ons (extra seat/number/unit) stay listed as buyable
    # even once active - they're bought in multiples, so "already have
    # one" isn't a reason to hide the option to add another. 'integration'
    # add-ons are a plain on/off toggle, so one already active does hide
    # it from the "add" list (there's nothing to stack).
    available_addons = [
        a for a in data["available_addons"]
        if a.get("capacity_key") != "messages"
        and (a["kind"] == "capacity" or a["key"] not in active_addon_keys)
    ]
    active_capacity_addons = [a for a in data["active_addons"] if a["kind"] == "capacity"]
    available_capacity_addons = [a for a in available_addons if a["kind"] == "capacity"]
    # Integrations listed alphabetically by name, not the underlying
    # list_addons() price ordering - same reasoning as
    # admin_org_pages.BillingCatalogueView's superadmin-facing list.
    active_integration_addons = sorted((a for a in data["active_addons"] if a["kind"] != "capacity"), key=lambda a: a["name"].lower())
    available_integration_addons = sorted((a for a in available_addons if a["kind"] != "capacity"), key=lambda a: a["name"].lower())
    # get_org_message_usage tolerates org_id having no subscription row yet
    # (falls back to the standard-plan defaults, zero add-on balance) - safe
    # to compute unconditionally even though the template only renders it in
    # the {% else %} (already-subscribed) branch below.
    message_usage = entitlements.get_org_message_usage(org_id)
    return templates.TemplateResponse(
        request,
        "billing_manage.html",
        {
            **data,
            "available_addons_to_add": available_addons,
            "active_capacity_addons": active_capacity_addons,
            "active_integration_addons": active_integration_addons,
            "available_capacity_addons": available_capacity_addons,
            "available_integration_addons": available_integration_addons,
            "message_usage": message_usage,
            "messages_addon": messages_addon,
        },
    )


@router.get("/success")
def billing_success(request: Request, user: dict = Depends(get_current_web_user)):
    org_id = _require_org_id(user)
    subscription = storage.get_subscription(org_id)
    plans = storage.list_plans()
    current_plan = None
    if subscription:
        for plan in plans:
            if plan["id"] == subscription.plan_id:
                current_plan = plan
    active_addon_keys = (
        storage.list_active_addons_for_subscription(subscription.id) if subscription else []
    )
    addons = [a for a in storage.list_addons() if a["key"] in active_addon_keys]
    return templates.TemplateResponse(
        request,
        "billing_success.html",
        {
            "subscription_active": bool(subscription and subscription.status == "active"),
            "current_plan": current_plan,
            "active_addons": addons,
        },
    )

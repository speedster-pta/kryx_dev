"""WhatsApp Embedded Signup: unit picker + OAuth callback.

Replaces manual WhatsAppNumber creation as the primary onboarding path
(WhatsAppNumberAdmin's manual create form stays as a fallback, per
Phillip's explicit call). Two routes:

  GET  /add-number           - picker page (BaseView, in admin_pages.py,
                                sidebar-visible, sqladmin's own
                                login_required gates it)
  POST /onboarding/start     - writes the pending intent, redirects to
                                Meta's Embedded Signup URL
  GET  /oauth/meta/whatsapp  - Meta's redirect_uri; exchanges the code,
                                discovers the WABA/number, creates the row

The picker's GET page itself is NOT in this file - it's a BaseView (see
admin_pages.OnboardingView) since it needs to render inside sqladmin's
layout/sidebar. This file is a plain APIRouter (registered in main.py
alongside campaigns_router etc., before setup_admin()) because
/oauth/meta/whatsapp must be reachable at the exact literal path
registered in the Embedded Signup URL/App Dashboard - a BaseView's
@expose works too, but keeping the OAuth mechanics separate from the
page-rendering shells matches how templates_router.py/webhooks.py already
split "does real work" from "renders a page shell".

Correlation problem this solves: Meta's redirect_uri receives only an
exchangeable `code` - no state we control comes back with it (see
onboarding-customers-as-a-tech-provider docs, June 2026). So "which
unit does this belong to" has to be established BEFORE the
redirect, and picked back up when the SAME staff member's browser lands
back on /oauth/meta/whatsapp - see storage.create_onboarding_intent()/
consume_latest_onboarding_intent() in units.py.
"""
import hashlib
import hmac

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException
from starlette.responses import RedirectResponse

from autosend import storage
from autosend.utils.logging import get_logger
from autosend.web.auth import get_current_web_user

router = APIRouter()
logger = get_logger(__name__)

GRAPH_BASE = "https://graph.facebook.com/v21.0"

# How long a picked-but-not-yet-completed onboarding intent stays valid.
# Generous enough to survive someone getting distracted mid-flow, tight
# enough that a callback days later (e.g. a bookmarked/replayed URL)
# can't silently attach a number to a stale intent.
INTENT_MAX_AGE_MINUTES = 30

EMBEDDED_SIGNUP_BASE_URL = "https://business.facebook.com/messaging/whatsapp/onboard/"
REDIRECT_URI = "https://oauth.kryx.co.za/oauth/meta/whatsapp"


def _require_meta_settings() -> dict:
    settings = storage.get_meta_platform_settings()
    if not settings or not settings.get("app_secret"):
        raise HTTPException(
            status_code=503,
            detail="Meta Platform Settings aren't configured yet - a superadmin "
                   "needs to fill these in under Meta Platform Settings first.",
        )
    return settings


def _require_redirect_settings() -> dict:
    """Lighter check for onboarding_start - building the Embedded Signup
    redirect URL only needs app_id and config_id, both of which are
    already plainly visible in that URL once built (see the URL Phillip
    originally shared - app_id/config_id are query params, not secrets).
    app_secret is only needed later, server-to-server, in the OAuth
    callback (_require_meta_settings above) - gating the redirect step on
    it too would block "Connect via WhatsApp" from working until every
    field is filled in, even though the redirect itself doesn't touch
    app_secret."""
    settings = storage.get_meta_platform_settings()
    if not settings or not settings.get("app_id") or not settings.get("config_id"):
        raise HTTPException(
            status_code=503,
            detail="Meta Platform Settings aren't configured yet - a superadmin "
                   "needs to fill in at least App ID and Config ID under Meta "
                   "Platform Settings first.",
        )
    return settings


@router.post("/onboarding/start")
async def onboarding_start(
    unit_id: int = Form(...),
    user: dict = Depends(get_current_web_user),
):
    """Writes the pending intent, then redirects the staff member's
    browser straight to Meta. Unit choice is validated against
    the staff member's own session-scoped unit_ids here - same
    check ScopedModelView.insert_model does for WhatsAppNumberAdmin - so
    this can't be used to onboard a number into a unit the staff
    member doesn't have access to, even by hand-crafting the POST."""
    if not user["is_superadmin"] and unit_id not in user["unit_ids"]:
        raise HTTPException(status_code=403, detail="Not authorized for this unit")

    settings = _require_redirect_settings()
    storage.create_onboarding_intent(user_id=user["id"], unit_id=unit_id)

    params = (
        f"app_id={settings['app_id']}"
        f"&config_id={settings['config_id']}"
        f'&extras=%7B%22version%22%3A%22v4%22%2C%22sessionInfoVersion%22%3A%223%22%2C%22featureType%22%3A%22whatsapp_business_app_onboarding%22%7D'
        f"&redirect_uri={REDIRECT_URI}"
    )
    return RedirectResponse(url=f"{EMBEDDED_SIGNUP_BASE_URL}?{params}", status_code=303)


async def _exchange_code_for_business_token(code: str, settings: dict) -> str:
    """Per Meta's Embedded Signup docs (Access Tokens guide / Embedded
    Signup overview, June 2026): the code Meta hands back after a
    completed flow exchanges directly for a Business Integration System
    User access token ("business token") - a single server-to-server
    call, no business_portfolio_id needed as input for this step."""
    async with httpx.AsyncClient(base_url=GRAPH_BASE, timeout=30) as client:
        response = await client.get(
            "/oauth/access_token",
            params={"client_id": settings["app_id"], "client_secret": settings["app_secret"], "code": code},
        )
    if response.status_code >= 400:
        logger.error("Meta code exchange error %s: %s", response.status_code, response.text)
        raise HTTPException(status_code=502, detail="Failed to exchange Embedded Signup code with Meta")
    data = response.json()
    token = data.get("access_token")
    if not token:
        raise HTTPException(status_code=502, detail="Meta did not return a business token")
    return token


async def _discover_waba_ids(business_token: str, settings: dict) -> list[str]:
    """We skipped the JS SDK, so Embedded Signup never handed us the WABA
    ID directly (that only happens via the postMessage the JS SDK
    listens for). debug_token introspection is the standard way to find
    out what a freshly-minted token actually grants access to: its
    granular_scopes list includes whatsapp_business_management with a
    target_ids array - exactly the WABA ID(s) just granted."""
    async with httpx.AsyncClient(base_url=GRAPH_BASE, timeout=30) as client:
        response = await client.get(
            "/debug_token",
            params={
                "input_token": business_token,
                "access_token": f"{settings['app_id']}|{settings['app_secret']}",
            },
        )
    if response.status_code >= 400:
        logger.error("Meta debug_token error %s: %s", response.status_code, response.text)
        raise HTTPException(status_code=502, detail="Failed to inspect the new Embedded Signup token")

    scopes = response.json().get("data", {}).get("granular_scopes", [])
    for scope in scopes:
        if scope.get("scope") == "whatsapp_business_management":
            return scope.get("target_ids", [])
    return []


async def _fetch_phone_numbers(waba_id: str, business_token: str) -> list[dict]:
    async with httpx.AsyncClient(base_url=GRAPH_BASE, timeout=30) as client:
        response = await client.get(
            f"/{waba_id}/phone_numbers",
            headers={"Authorization": f"Bearer {business_token}"},
        )
    if response.status_code >= 400:
        logger.error("Meta phone_numbers list error %s: %s", response.status_code, response.text)
        raise HTTPException(status_code=502, detail="Failed to list phone numbers for the new WhatsApp Business Account")
    return response.json().get("data", [])


@router.get("/oauth/meta/whatsapp")
async def oauth_meta_whatsapp_callback(
    code: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    user: dict = Depends(get_current_web_user),
):
    """Meta's redirect_uri. Runs in the same staff member's browser
    session that clicked "Connect" on the picker page - that's what makes
    consume_latest_onboarding_intent(user['id'], ...) safe to trust
    without any state param from Meta."""
    if error:
        logger.warning("Embedded Signup returned an error: %s - %s", error, error_description)
        raise HTTPException(status_code=400, detail=error_description or error)
    if not code:
        raise HTTPException(status_code=400, detail="Missing code from Embedded Signup redirect")

    intent = storage.consume_latest_onboarding_intent(user["id"], max_age_minutes=INTENT_MAX_AGE_MINUTES)
    if not intent:
        raise HTTPException(
            status_code=400,
            detail="No pending onboarding request found for your session (it may have "
                   "expired). Go back to Add Number and try again.",
        )

    settings = _require_meta_settings()
    business_token = await _exchange_code_for_business_token(code, settings)
    waba_ids = await _discover_waba_ids(business_token, settings)

    if not waba_ids:
        raise HTTPException(
            status_code=502,
            detail="Embedded Signup completed but no WhatsApp Business Account was "
                   "granted - nothing to add. If this persists, use the manual Add "
                   "Number form under WhatsApp Numbers instead.",
        )
    if len(waba_ids) > 1:
        # Multi-WABA grants are possible (Meta's docs note waba_ids can be
        # a list) but rare for this org's shape (one unit number
        # per flow) - rather than guess which one the staff member meant,
        # fail clearly and point at the manual fallback, which Phillip
        # confirmed stays available for exactly this kind of edge case.
        logger.warning("Embedded Signup granted multiple WABAs in one flow: %s", waba_ids)
        raise HTTPException(
            status_code=501,
            detail=f"This flow granted access to {len(waba_ids)} WhatsApp Business "
                   f"Accounts at once, which isn't supported here yet. Add these "
                   f"numbers manually under WhatsApp Numbers instead: {', '.join(waba_ids)}",
        )

    waba_id = waba_ids[0]
    phone_numbers = await _fetch_phone_numbers(waba_id, business_token)
    if not phone_numbers:
        raise HTTPException(
            status_code=502,
            detail="Embedded Signup completed but the new WhatsApp Business Account "
                   "has no phone numbers yet. Finish adding a number in Meta's flow, "
                   "or use the manual Add Number form.",
        )

    created_ids = []
    for phone in phone_numbers:
        label = phone.get("verified_name") or phone.get("display_phone_number") or "WhatsApp Number"
        number_id = storage.create_whatsapp_number(
            unit_id=intent["unit_id"],
            label=label,
            phone_number_id=phone["id"],
            access_token=business_token,
            waba_id=waba_id,
            onboarded_via="embedded_signup",
            display_phone_number=phone.get("display_phone_number"),
        )
        created_ids.append(number_id)

    logger.info(
        "Embedded Signup created %d WhatsApp number(s) for unit_id=%s via user_id=%s: %s",
        len(created_ids), intent["unit_id"], user["id"], created_ids,
    )

    return RedirectResponse(url="/whatsapp-numbers", status_code=303)

"""WhatsApp Embedded Signup: unit picker + JS SDK completion.

Replaces manual WhatsAppNumber creation as the primary onboarding path
(WhatsAppNumberAdmin's manual create form stays as a fallback, per
Phillip's explicit call). Two routes:

  GET  /add-number          - picker page (BaseView, in admin_pages.py,
                               sidebar-visible, sqladmin's own
                               login_required gates it) - also renders the
                               Facebook JS SDK bootstrap/FB.login() call
  POST /onboarding/start    - writes the pending intent (fetch()'d before
                               FB.login() is invoked client-side)
  POST /onboarding/complete - fetch()'d from add_number.html's FB.login()
                               callback once Meta hands back an
                               exchangeable `code`; exchanges it, discovers
                               the WABA/number, creates the row

The picker's GET page itself is NOT in this file - it's a BaseView (see
admin_pages.OnboardingView) since it needs to render inside sqladmin's
layout/sidebar.

Earlier version of this file drove a server-side OAuth redirect (POST
/onboarding/start redirecting the browser to
business.facebook.com/messaging/whatsapp/onboard/ with a redirect_uri,
expecting Meta to navigate back to a GET /oauth/meta/whatsapp with a
`code`). That never works in production: Meta's Embedded Signup does not
support a plain redirect_uri callback at all - it requires the Facebook JS
SDK's FB.login() with a config_id, and delivers the WABA ID/phone number
ID/exchangeable code to the window that opened the flow via
window.postMessage and FB.login()'s own response callback, never a
top-level browser redirect. The parent single-tenant project hit exactly
this (every onboarding intent had `consumed_at` NULL) before switching to
this JS SDK approach - ported here before Kryx made the same mistake in
production.

Correlation problem this still solves: Meta's code exchange returns only
an exchangeable `code` - no state we control comes back with it. So
"which unit does this belong to" has to be established BEFORE
FB.login() runs (via POST /onboarding/start), and picked back up when the
SAME user's browser calls POST /onboarding/complete - see
storage.create_onboarding_intent()/consume_latest_onboarding_intent() in
units.py.
"""
import base64
import hashlib
import hmac
import json

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException
from pydantic import BaseModel

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
    """Writes the pending intent just before add_number.html's JS calls
    FB.login(). Unit choice is validated against the user's own
    session-scoped unit_ids here - same check ScopedModelView.insert_model
    does for WhatsAppNumberAdmin - so this can't be used to onboard a
    number into a unit the user doesn't have access to, even by
    hand-crafting the POST."""
    if not user["is_superadmin"] and unit_id not in user["unit_ids"]:
        raise HTTPException(status_code=403, detail="Not authorized for this unit")

    _require_redirect_settings()
    storage.create_onboarding_intent(user_id=user["id"], unit_id=unit_id)
    return {"ok": True}


async def _exchange_code_for_business_token(code: str, settings: dict) -> str:
    """POST with a JSON body, not GET-with-query-params (which also put
    client_secret in a URL - more likely to end up logged somewhere than a
    POST body). No redirect_uri: Meta rejects one here with "Error
    validating verification code. Please make sure your redirect_uri is
    identical to the one you used in the OAuth dialog request" (subcode
    36008) - the JS SDK's FB.login({config_id, ...}) call (see
    add_number.html) never takes a redirect_uri param in the first place,
    so there's nothing for us to echo back here."""
    async with httpx.AsyncClient(base_url=GRAPH_BASE, timeout=30) as client:
        response = await client.post(
            "/oauth/access_token",
            json={
                "client_id": settings["app_id"],
                "client_secret": settings["app_secret"],
                "grant_type": "authorization_code",
                "code": code,
            },
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
    """Fallback for when the JS SDK's postMessage FINISH event didn't
    supply a waba_id (see onboarding_complete). debug_token introspection
    is the standard way to find out what a freshly-minted token actually
    grants access to: its granular_scopes list includes
    whatsapp_business_management with a target_ids array - exactly the
    WABA ID(s) just granted."""
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


async def _subscribe_app_to_waba(waba_id: str, business_token: str) -> None:
    """Meta doesn't send any webhook events (messages, statuses,
    account_update) for a WABA until an app is subscribed to it - Embedded
    Signup does NOT do this automatically. Without this, a newly onboarded
    number sends fine but never receives anything in the Inbox, because
    GET /{waba_id}/subscribed_apps comes back empty. This is a one-time
    POST per WABA (no body needed) - safe to call even if already
    subscribed."""
    async with httpx.AsyncClient(base_url=GRAPH_BASE, timeout=30) as client:
        response = await client.post(
            f"/{waba_id}/subscribed_apps",
            headers={"Authorization": f"Bearer {business_token}"},
        )
    if response.status_code >= 400:
        logger.error("Meta subscribed_apps error %s: %s", response.status_code, response.text)
        raise HTTPException(
            status_code=502,
            detail="The number was found but the app couldn't subscribe to its "
                   "webhooks, so replies/delivery status won't reach the inbox. "
                   "Try again, or check Meta Platform Settings.",
        )


async def _fetch_single_phone_number(phone_number_id: str, business_token: str) -> list[dict]:
    """Used when the JS SDK's postMessage FINISH event already told us
    exactly which phone_number_id was onboarded - skips listing every
    number on the WABA and just confirms/labels this one."""
    async with httpx.AsyncClient(base_url=GRAPH_BASE, timeout=30) as client:
        response = await client.get(
            f"/{phone_number_id}",
            params={"fields": "id,display_phone_number,verified_name"},
            headers={"Authorization": f"Bearer {business_token}"},
        )
    if response.status_code >= 400:
        logger.error("Meta phone number lookup error %s: %s", response.status_code, response.text)
        raise HTTPException(status_code=502, detail="Failed to look up the new phone number's details")
    data = response.json()
    return [data] if data.get("id") else []


class OnboardingCompleteRequest(BaseModel):
    code: str
    waba_id: str | None = None
    phone_number_id: str | None = None


@router.post("/onboarding/complete")
async def onboarding_complete(
    payload: OnboardingCompleteRequest,
    user: dict = Depends(get_current_web_user),
):
    """fetch()'d from add_number.html once FB.login()'s callback hands
    back an exchangeable `code` (and, from the postMessage FINISH event
    listener, usually a waba_id/phone_number_id too - see module
    docstring). Runs in the same user's browser session that clicked
    "Connect" on the picker page - that's what makes
    consume_latest_onboarding_intent(user['id'], ...) safe to trust
    without any state param from Meta."""
    intent = storage.consume_latest_onboarding_intent(user["id"], max_age_minutes=INTENT_MAX_AGE_MINUTES)
    if not intent:
        raise HTTPException(
            status_code=400,
            detail="No pending onboarding request found for your session (it may have "
                   "expired). Go back to Add Number and try again.",
        )

    settings = _require_meta_settings()
    business_token = await _exchange_code_for_business_token(payload.code, settings)

    if payload.waba_id:
        waba_ids = [payload.waba_id]
    else:
        # The JS SDK's postMessage listener normally supplies waba_id
        # directly (see add_number.html) - debug_token introspection is
        # only a fallback for the rare case that event didn't fire (e.g.
        # popup blocked briefly, or Meta changes the payload shape).
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
        # per flow) - rather than guess which one the user meant,
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
    await _subscribe_app_to_waba(waba_id, business_token)

    if payload.phone_number_id:
        phone_numbers = await _fetch_single_phone_number(payload.phone_number_id, business_token)
    else:
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

    return {"redirect": "/whatsapp-numbers"}


def _parse_signed_request(signed_request: str, app_secret: str) -> dict:
    """Meta's Deauthorize/Data-Deletion callback body format: a
    "<base64url signature>.<base64url JSON payload>" string, HMAC-SHA256
    signed over the payload with the app secret. See Meta's Facebook
    Login "signed_request" docs - the same format is reused here since
    Embedded Signup deauthorization piggybacks on the standard Facebook
    Login callback mechanism."""
    try:
        encoded_sig, encoded_payload = signed_request.split(".", 1)
    except ValueError:
        raise HTTPException(status_code=400, detail="Malformed signed_request")

    def _b64url_decode(data: str) -> bytes:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))

    expected_sig = hmac.new(app_secret.encode(), encoded_payload.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(_b64url_decode(encoded_sig), expected_sig):
        raise HTTPException(status_code=401, detail="Invalid signed_request signature")

    return json.loads(_b64url_decode(encoded_payload))


@router.post("/oauth/meta/deauthorize")
async def oauth_meta_deauthorize_callback(signed_request: str = Form(...)):
    """Meta's Deauthorize Callback URL (App Dashboard > Facebook Login for
    Business settings). Meta POSTs here when a user removes the app's
    access from their Facebook/Meta account settings.

    We can only verify and log this, not auto-revoke a specific
    whatsapp_numbers row: the signed_request payload identifies the
    Facebook/Meta user_id who deauthorized, but this schema never
    records which Facebook user completed a given Embedded Signup
    flow - only the resulting waba_id/phone_number_id (see
    create_whatsapp_number in storage/units.py). A superadmin has to
    match the user up out-of-band (e.g. via Meta Business Manager) and
    deactivate/rotate the affected whatsapp_numbers row manually."""
    settings = _require_meta_settings()
    data = _parse_signed_request(signed_request, settings["app_secret"])
    logger.warning(
        "Meta deauthorize callback received for Facebook user_id=%s - no whatsapp_numbers "
        "row was auto-revoked (see docstring); a superadmin should check Meta Business "
        "Manager and deactivate the matching number under WhatsApp Numbers if needed.",
        data.get("user_id"),
    )
    return {"success": True}

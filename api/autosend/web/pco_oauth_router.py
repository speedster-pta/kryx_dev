"""Planning Center OAuth: "Connect via Planning Center" + its callback.

Replaces manually pasting a PCO Personal Access Token as the primary way
an org connects PCO (PCOOrganizationSettingsAdmin / PcoSettingsView's
existing PAT form stay as a fallback - same "OAuth as the friendlier
default, manual entry kept as an escape hatch" split as
onboarding_router.py did for WhatsApp Embedded Signup). Two routes:

  POST /pco-oauth/start        - writes a state row, redirects to PCO's
                                  own /oauth/authorize
  GET  /oauth/planning-center  - PCO's redirect_uri; exchanges the code,
                                  stores the token pair

Unlike Meta's Embedded Signup callback (see onboarding_router.py), PCO's
authorize endpoint passes `state` straight back to the callback - so
correlating "which org is this for" is a plain state-token lookup
(storage.create_pco_oauth_state/consume_pco_oauth_state), no
session-based correlation trick needed.

redirect_uri is derived per-request from request.base_url (same pattern
as the PCO webhook URL / iCal URL built in admin_org_pages.py), NOT a
hardcoded constant - unlike Meta's Embedded Signup, which has exactly
one production app registration, kryx and kryx-dev are two independent
deployments (separate hostnames, separate DBs, separate PCO OAuth
connections), so the callback has to resolve to whichever one the flow
was started from. This only works because uvicorn is run with
--proxy-headers --forwarded-allow-ips "*" (see Dockerfile CMD), so
request.base_url correctly reports the real https scheme/host set by
nginx's X-Forwarded-Proto rather than the container's own plain-http
view of itself. PCO's OAuth app registration needs every environment's
callback URL added to its allowed list up front, e.g.
https://dev.kryx.co.za/oauth/planning-center and
https://kryx.co.za/oauth/planning-center.
"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from starlette.responses import RedirectResponse

from autosend import storage
from autosend.integrations.planning_center_oauth import (
    build_authorize_url,
    exchange_code_for_tokens,
)
from autosend.utils.logging import get_logger
from autosend.web.auth import get_current_web_user, pco_module_visible

router = APIRouter()
logger = get_logger(__name__)

# Same bound as whatsapp_onboarding_intents' INTENT_MAX_AGE_MINUTES in
# onboarding_router.py, same reasoning: generous enough to survive
# someone getting distracted mid-flow, tight enough that a
# bookmarked/replayed callback days later can't silently attach a token
# to a stale connection attempt.
STATE_MAX_AGE_MINUTES = 30


def _redirect_uri(request: Request) -> str:
    return f"{str(request.base_url).rstrip('/')}/oauth/planning-center"


def _require_platform_settings() -> dict:
    settings = storage.get_pco_platform_settings()
    if not settings or not settings.get("client_secret"):
        raise HTTPException(
            status_code=503,
            detail="PCO Platform Settings aren't configured yet - a superadmin "
                   "needs to fill in the OAuth client ID/secret under PCO Platform "
                   "Settings first.",
        )
    return settings


@router.post("/pco-oauth/start")
async def pco_oauth_start(
    request: Request,
    org_id: int | None = Form(None),
    user: dict = Depends(get_current_web_user),
):
    """org_id is only honoured for a superadmin (picking which org to
    connect, same as PcoSettingsView's own org_id query param) - an org
    admin's own org_id is always taken from their session, never trusted
    from the form, same rule as every other org-scoped write in this
    codebase."""
    if not pco_module_visible(request):
        raise HTTPException(status_code=403, detail="Not authorized")
    if user["is_superadmin"]:
        if not org_id:
            raise HTTPException(status_code=400, detail="org_id is required for a superadmin")
        target_org_id = org_id
    else:
        if not user.get("is_org_admin"):
            raise HTTPException(status_code=403, detail="Not authorized")
        target_org_id = user["org_id"]

    if storage.get_organisation(target_org_id) is None:
        raise HTTPException(status_code=404, detail="Not found")

    settings = _require_platform_settings()
    state = storage.create_pco_oauth_state(org_id=target_org_id, user_id=user["id"])

    return RedirectResponse(
        url=build_authorize_url(settings["client_id"], _redirect_uri(request), state),
        status_code=303,
    )


@router.get("/oauth/planning-center")
async def pco_oauth_callback(request: Request):
    params = request.query_params
    error = params.get("error")
    if error:
        raise HTTPException(status_code=400, detail=f"Planning Center OAuth error: {error}")

    code = params.get("code")
    state = params.get("state")
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code/state from Planning Center")

    consumed = storage.consume_pco_oauth_state(state, max_age_minutes=STATE_MAX_AGE_MINUTES)
    if consumed is None:
        raise HTTPException(
            status_code=400,
            detail="This Planning Center connection attempt has expired or was already used - "
                   "start again from PCO Settings.",
        )
    org_id = consumed["org_id"]

    settings = _require_platform_settings()
    try:
        token_data = await exchange_code_for_tokens(
            settings["client_id"], settings["client_secret"], code, _redirect_uri(request),
        )
    except Exception:
        logger.exception("Planning Center code exchange failed for org %s", org_id)
        raise HTTPException(status_code=502, detail="Failed to exchange code with Planning Center")

    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=token_data["expires_in"])
    ).isoformat()
    storage.save_pco_oauth_tokens(
        org_id, token_data["access_token"], token_data["refresh_token"], expires_at,
    )

    return RedirectResponse(url=f"/pco-settings?org_id={org_id}", status_code=303)

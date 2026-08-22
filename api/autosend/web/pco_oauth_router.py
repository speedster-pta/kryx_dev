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
  POST /pco-oauth/disconnect   - clears Kryx's stored OAuth token pair
                                  for an org (see storage.disconnect_pco_oauth
                                  for exactly what this does and doesn't do)

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
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import RedirectResponse

from autosend import storage
from autosend.admin_models import engine, Unit
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


async def _auto_fill_subdomain(org_id: int) -> None:
    """Best-effort: fetches the connected PCO account's own Church Center
    subdomain (verified live - see
    PlanningCenterClient.get_organization_info) and fills it in if the
    org doesn't already have one set. Failures here are logged and
    swallowed, same reasoning as _auto_create_webhooks below - a lookup
    hiccup shouldn't undo an otherwise successful OAuth connection, and
    the field stays hand-editable on the PCO Settings page either way."""
    from autosend.clients import get_pco_org_client

    try:
        info = await get_pco_org_client(org_id).get_organization_info()
    except Exception:
        logger.exception("Failed to fetch PCO organization info for org %s", org_id)
        return
    subdomain = info.get("church_center_subdomain")
    if subdomain:
        storage.set_pco_subdomain_if_blank(org_id, subdomain)


async def _auto_create_webhooks(org_id: int, base_url: str) -> None:
    """Best-effort: right after a successful OAuth connect, creates a PCO
    webhook subscription (people.v2.events.form_submission.created,
    verified live against a real PCO organisation - see
    PlanningCenterClient.create_form_submission_webhook) for every active
    unit in this org that doesn't already have one pointed at its own
    webhook URL. Failures here are logged and swallowed rather than
    raised - a webhook-creation hiccup shouldn't undo an otherwise
    successful OAuth connection, and the manual PCO Settings page (adding
    a webhook by hand, or via PCO's own dashboard) remains available as a
    fallback either way.

    Skips a unit whose webhook URL already has a subscription pointed at
    it (checked via list_webhook_subscriptions) - safe to call again on a
    repeat OAuth connect without creating duplicates. If a unit already
    has a manually-configured primary secret, the new one is added
    alongside it (see unit_webhook_secrets) rather than overwriting a
    working setup."""
    from autosend.clients import get_pco_org_client
    from autosend.storage.units import ensure_webhook_slug

    with Session(engine) as session:
        units = session.execute(
            select(Unit).where(Unit.org_id == org_id, Unit.active == True)  # noqa: E712
        ).scalars().all()
        unit_data = [(u.id, u.pco_webhook_secret) for u in units]

    if not unit_data:
        return

    try:
        pco_client = get_pco_org_client(org_id)
        existing = await pco_client.list_webhook_subscriptions()
    except Exception:
        logger.exception("Failed to list existing PCO webhook subscriptions for org %s", org_id)
        return
    existing_urls = {s["url"] for s in existing}

    for unit_id, current_secret in unit_data:
        webhook_slug = ensure_webhook_slug(unit_id)
        if not webhook_slug:
            continue
        url = f"{base_url}/webhooks/planning-center/people-form/{webhook_slug}"
        if url in existing_urls:
            continue

        try:
            result = await pco_client.create_form_submission_webhook(url)
        except Exception:
            logger.exception("Failed to auto-create PCO webhook for unit %s", unit_id)
            continue

        if current_secret:
            storage.create_unit_webhook_secret(
                unit_id, result["authenticity_secret"], label="Auto-created via OAuth connect",
            )
        else:
            with Session(engine) as session:
                unit = session.get(Unit, unit_id)
                unit.pco_webhook_secret = result["authenticity_secret"]
                session.commit()


def _resolve_target_org_id(user: dict, org_id: int | None) -> int:
    """Shared by /pco-oauth/start and /pco-oauth/disconnect: org_id from
    the form is only ever honoured for a superadmin (picking which org
    to act on, same as PcoSettingsView's own org_id query param) - an
    org admin's own org_id always comes from their session, never
    trusted from the form, same rule as every other org-scoped write in
    this codebase."""
    if user["is_superadmin"]:
        if not org_id:
            raise HTTPException(status_code=400, detail="org_id is required for a superadmin")
        return org_id
    if not user.get("is_org_admin"):
        raise HTTPException(status_code=403, detail="Not authorized")
    return user["org_id"]


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
    if not pco_module_visible(request):
        raise HTTPException(status_code=403, detail="Not authorized")
    target_org_id = _resolve_target_org_id(user, org_id)

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
    # Clears any cached client/credentials from a prior connection for
    # this org - without this, a reconnect would keep using the previous
    # connection's now-stale token until it happened to expire. See
    # clients.invalidate_pco_org_cache's own docstring.
    from autosend.clients import invalidate_pco_org_cache
    await invalidate_pco_org_cache(org_id)

    await _auto_fill_subdomain(org_id)
    await _auto_create_webhooks(org_id, str(request.base_url).rstrip("/"))

    return RedirectResponse(url=f"/pco-settings?org_id={org_id}", status_code=303)


@router.post("/pco-oauth/disconnect")
async def pco_oauth_disconnect(
    request: Request,
    org_id: int | None = Form(None),
    user: dict = Depends(get_current_web_user),
):
    """Clears Kryx's stored OAuth token pair for an org - see
    storage.disconnect_pco_oauth for exactly what this does (and
    doesn't: it does not revoke the grant with Planning Center itself)."""
    if not pco_module_visible(request):
        raise HTTPException(status_code=403, detail="Not authorized")
    target_org_id = _resolve_target_org_id(user, org_id)

    if storage.get_organisation(target_org_id) is None:
        raise HTTPException(status_code=404, detail="Not found")

    storage.disconnect_pco_oauth(target_org_id)

    from autosend.clients import invalidate_pco_org_cache
    await invalidate_pco_org_cache(target_org_id)

    redirect_url = f"/pco-settings?org_id={target_org_id}" if user["is_superadmin"] else "/pco-settings"
    return RedirectResponse(url=redirect_url, status_code=303)

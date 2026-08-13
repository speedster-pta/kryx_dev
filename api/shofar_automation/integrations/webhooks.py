import hashlib
import hmac

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import PlainTextResponse

from shofar_automation import storage
from shofar_automation.services.people_forms import process_people_form
from shofar_automation.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(
    prefix="/webhooks",
    tags=["webhooks"],
)

def _verify_pco_signature(body: bytes, signature: str | None, webhook_secret: str) -> None:
    """PCO signs each webhook delivery with the subscription's Authenticity
    Secret (HMAC-SHA256 over the raw request body). Reject anything that
    doesn't match instead of trusting whatever is POSTed here - this
    triggers a real WhatsApp send to a real person."""
    if not signature:
        raise HTTPException(status_code=401, detail="Missing webhook signature")

    expected = hmac.new(webhook_secret.encode(), body, hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")


@router.post("/planning-center/people-form/{slug}")
async def people_form_submission(slug: str, request: Request, background_tasks: BackgroundTasks):
    unit = storage.get_unit_by_slug(slug)
    if not unit or not unit["active"]:
        raise HTTPException(status_code=404, detail="Unknown or inactive unit")

    if not unit.get("pco_webhook_secret"):
        raise HTTPException(status_code=404, detail="This unit has no PCO webhook configured")

    body = await request.body()

    _verify_pco_signature(
        body,
        request.headers.get("X-PCO-Webhooks-Authenticity"),
        unit["pco_webhook_secret"],
    )

    envelope = await request.json()

    # Ack immediately - PCO retries on timeout/non-2xx, and the actual
    # work (PCO lookup + WhatsApp send) is too slow to do inline safely.
    background_tasks.add_task(process_people_form, unit, envelope)

    return {"status": "accepted"}

@router.get("/whatsapp", response_class=PlainTextResponse)
async def whatsapp_webhook_verify(request: Request):
    """Meta's handshake when you (re)register the webhook URL in the App
    Dashboard. Purely a one-time-per-registration setup step - unrelated
    to onboarding_router.py's OAuth callback, which is a separate route
    entirely. webhook_verify_token used to be hardcoded here
    (WHATSAPP_WEBHOOK_VERIFY_TOKEN = "***") before meta_platform_settings
    existed - moved there (see admin_views.MetaPlatformSettingsAdmin) so
    it's not a literal secret sitting in source control."""
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode != "subscribe":
        raise HTTPException(status_code=400, detail="Invalid hub.mode")

    settings = storage.get_meta_platform_settings()
    expected_token = settings["webhook_verify_token"] if settings else None
    if not expected_token or not hmac.compare_digest(token or "", expected_token):
        raise HTTPException(status_code=403, detail="Invalid verify token")

    if not challenge:
        raise HTTPException(status_code=400, detail="Missing challenge")

    return challenge


@router.post("/whatsapp")
async def whatsapp_webhook_event(request: Request):
    """Receives every subscribed WhatsApp webhook event - currently only
    account_update is handled (specifically PARTNER_ADDED, fired when a
    unit completes Embedded Signup). This is an audit-trail
    fallback only, not the primary onboarding path: onboarding_router.py's
    /oauth/meta/whatsapp callback does the real work (exchanging the code,
    creating the whatsapp_numbers row) synchronously in the staff member's
    browser session, which is the only place a unit_id can be
    correlated to the new number - this webhook has no equivalent
    correlation available (Meta doesn't echo back any state we control),
    so it only logs for visibility/debugging and never writes to
    whatsapp_numbers itself.

    Other event types aren't subscribed to by this app yet - Meta only
    sends what your webhook subscription is configured for in the App
    Dashboard, so there's nothing else to filter out here."""
    body = await request.body()

    settings = storage.get_meta_platform_settings()
    if not settings or not settings.get("app_secret"):
        logger.warning("Received WhatsApp webhook event but no app_secret is configured - cannot verify signature, dropping")
        raise HTTPException(status_code=503, detail="Meta platform settings not configured")

    signature = request.headers.get("X-Hub-Signature-256", "")
    expected = "sha256=" + hmac.new(settings["app_secret"].encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    envelope = await request.json()
    for entry in envelope.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") != "account_update":
                continue
            value = change.get("value", {})
            if value.get("event") == "PARTNER_ADDED":
                logger.info(
                    "Embedded Signup PARTNER_ADDED: business_id=%s waba_id=%s "
                    "(audit only - number creation happens via the OAuth "
                    "callback, not this webhook)",
                    value.get("business_id"), value.get("waba_id"),
                )

    # Meta expects a fast 2xx regardless of payload content - slow/failing
    # responses here can pause future webhook delivery.
    return {"status": "received"}


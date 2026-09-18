import hashlib
import hmac

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import PlainTextResponse

from autosend import storage
from autosend.billing import engine as billing_engine
from autosend.billing.paystack import verify_webhook_signature as verify_paystack_signature
from autosend.integrations.whatsapp_media import download_and_store_media
from autosend.services.ai_reply import maybe_generate_ai_reply
from autosend.services.audio_transcription import transcribe_inbound_audio
from autosend.services.people_forms import process_people_form
from autosend.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(
    prefix="/webhooks",
    tags=["webhooks"],
)

def _verify_pco_signature(body: bytes, signature: str | None, candidate_secrets: list[str]) -> None:
    """PCO signs each webhook delivery with the subscription's Authenticity
    Secret (HMAC-SHA256 over the raw request body). Reject anything that
    doesn't match instead of trusting whatever is POSTed here - this
    triggers a real WhatsApp send to a real person.

    candidate_secrets is a unit's primary pco_webhook_secret plus every
    additional unit_webhook_secrets row (see that table's schema.py
    docstring) - a unit can have more than one PCO webhook subscription
    pointed at it (e.g. created by different PCO users covering
    different forms' visibility), each with its own Authenticity Secret,
    so a request is accepted if it matches ANY of them, not just the
    first one ever configured."""
    if not signature:
        raise HTTPException(status_code=401, detail="Missing webhook signature")

    for secret in candidate_secrets:
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected, signature):
            return

    raise HTTPException(status_code=401, detail="Invalid webhook signature")


@router.post("/planning-center/people-form/{webhook_slug}")
async def people_form_submission(webhook_slug: str, request: Request, background_tasks: BackgroundTasks):
    # Keyed by the random, globally-unique webhook_slug rather than the
    # human-readable `slug` column - slug is only unique per organisation
    # (every org's default unit is named/slugged "Main"), so two orgs'
    # webhook URLs would otherwise collide on the same path.
    unit = storage.get_unit_by_webhook_slug(webhook_slug)
    if not unit or not unit["active"]:
        raise HTTPException(status_code=404, detail="Unknown or inactive unit")

    candidate_secrets = [
        s for s in [unit.get("pco_webhook_secret")] + storage.get_unit_webhook_secrets_decrypted(unit["id"])
        if s
    ]
    if not candidate_secrets:
        raise HTTPException(status_code=404, detail="This unit has no PCO webhook configured")

    if not storage.is_enabled(unit["org_id"], storage.MODULE_PCO):
        # Safe no-op for a disabled org, same as "misconfigured" above -
        # PCO itself will keep retrying subscriptions regardless of
        # whether this org still has the module enabled, so this must
        # 404 cleanly rather than assume PCO config is still meaningful.
        raise HTTPException(status_code=404, detail="Planning Center integration is not enabled for this organisation")

    body = await request.body()

    _verify_pco_signature(body, request.headers.get("X-PCO-Webhooks-Authenticity"), candidate_secrets)

    if not storage.is_org_active(unit["org_id"]) or not storage.is_org_current(unit["org_id"]):
        # Org has the PCO module enabled but is inactive (e.g. not
        # currently paying) or its subscription isn't currently active
        # (billing lapsed) - ack cleanly (PCO would otherwise keep
        # retrying) but don't actually send a confirmation.
        return {"status": "accepted"}

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


def _extract_message_content(msg: dict) -> tuple[str | None, str | None, str | None]:
    """Returns (body, media_id, media_mime_type) for one entry in a
    webhook's value.messages array, covering the message types an
    inbound WhatsApp Inbox conversation can actually receive. Unhandled/
    future message types fall through with body=None, media_id=None -
    still recorded (message_type is stored as-is) rather than dropped."""
    msg_type = msg.get("type")
    if msg_type == "text":
        return msg.get("text", {}).get("body"), None, None
    if msg_type in ("image", "video", "document", "sticker"):
        media = msg.get(msg_type, {})
        return media.get("caption"), media.get("id"), media.get("mime_type")
    if msg_type == "audio":
        media = msg.get("audio", {})
        return None, media.get("id"), media.get("mime_type")
    if msg_type == "location":
        location = msg.get("location", {})
        name = location.get("name") or f"{location.get('latitude')}, {location.get('longitude')}"
        return f"Location: {name}", None, None
    if msg_type == "button":
        return msg.get("button", {}).get("text"), None, None
    if msg_type == "interactive":
        interactive = msg.get("interactive", {})
        reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
        return reply.get("title"), None, None
    return None, None, None


def _handle_inbound_messages(value: dict, background_tasks: BackgroundTasks) -> None:
    metadata = value.get("metadata", {})
    number = storage.get_whatsapp_number_by_phone_id(metadata.get("phone_number_id"))
    if not number:
        logger.warning(
            "Inbound WhatsApp message for unknown phone_number_id=%s - dropping "
            "(no matching whatsapp_numbers row)", metadata.get("phone_number_id"),
        )
        return

    contacts_by_wa_id = {c["wa_id"]: c for c in value.get("contacts", []) if c.get("wa_id")}

    for msg in value.get("messages", []):
        contact = contacts_by_wa_id.get(msg.get("from"))
        contact_name = (contact or {}).get("profile", {}).get("name")

        conversation = storage.get_or_create_conversation(
            unit_id=number["unit_id"],
            whatsapp_number_id=number["id"],
            contact_wa_id=msg["from"],
            contact_name=contact_name,
        )
        body, media_id, media_mime_type = _extract_message_content(msg)
        message_id = storage.record_inbound_message(
            conversation["id"],
            wamid=msg.get("id"),
            message_type=msg.get("type", "unknown"),
            body=body,
            media_id=media_id,
            media_mime_type=media_mime_type,
            contact_name=contact_name,
        )
        if media_id:
            background_tasks.add_task(download_and_store_media, message_id, media_id, number["access_token"])

        message_type = msg.get("type", "unknown")
        if message_type == "audio":
            # transcribe_inbound_audio re-invokes maybe_generate_ai_reply
            # itself once a transcript exists - scheduling it here too
            # would double up the AI/keyword reply for every voice note.
            # BackgroundTasks run in the order added, each awaited to
            # completion before the next starts, so this always runs
            # after the download above.
            background_tasks.add_task(transcribe_inbound_audio, message_id)
        elif body:
            background_tasks.add_task(maybe_generate_ai_reply, conversation["id"], message_id)


def _handle_delivery_statuses(value: dict) -> None:
    for status in value.get("statuses", []):
        wamid = status.get("id")
        delivery_status = status.get("status")
        if not wamid or not delivery_status:
            continue
        error_message = None
        errors = status.get("errors")
        if errors:
            error_message = errors[0].get("title")
        storage.update_delivery_status(wamid, delivery_status, error_message=error_message)


@router.post("/whatsapp")
async def whatsapp_webhook_event(request: Request, background_tasks: BackgroundTasks):
    """Receives every subscribed WhatsApp webhook event.

    `account_update` (PARTNER_ADDED, fired when a unit completes Embedded
    Signup) is audit-trail only, not the primary onboarding path:
    onboarding_router.py's /oauth/meta/whatsapp callback does the real
    work (exchanging the code, creating the whatsapp_numbers row)
    synchronously in the user's browser session, which is the only place
    a unit_id can be correlated to the new number - this webhook has no
    equivalent correlation available (Meta doesn't echo back any state we
    control), so it only logs for visibility/debugging and never writes
    to whatsapp_numbers itself.

    `messages` covers both inbound WhatsApp Inbox messages
    (value.messages) and delivery/read receipts for messages this app
    sent (value.statuses) - Meta puts both under the same webhook field,
    distinguished by which key is present in `value`, not by `field`
    itself. Media (images/audio/video/documents) is downloaded in the
    background so this handler can still return its fast 2xx immediately.

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
            field = change.get("field")
            value = change.get("value", {})
            if field == "account_update":
                if value.get("event") == "PARTNER_ADDED":
                    logger.info(
                        "Embedded Signup PARTNER_ADDED: business_id=%s waba_id=%s "
                        "(audit only - number creation happens via the OAuth "
                        "callback, not this webhook)",
                        value.get("business_id"), value.get("waba_id"),
                    )
            elif field == "messages":
                if value.get("messages"):
                    _handle_inbound_messages(value, background_tasks)
                if value.get("statuses"):
                    _handle_delivery_statuses(value)

    # Meta expects a fast 2xx regardless of payload content - slow/failing
    # responses here can pause future webhook delivery.
    return {"status": "received"}


@router.post("/paystack")
async def paystack_webhook(request: Request, background_tasks: BackgroundTasks):
    """Paystack's own server-to-server webhook - the authoritative
    confirmation path for a payment, independent of whether the payer's
    browser ever makes it back to /billing/callback (they might close the
    tab right after paying). Verifies X-Paystack-Signature (HMAC-SHA512
    over the raw body, see billing.paystack.verify_webhook_signature) the
    same ack-then-background_tasks shape as the PCO webhook above -
    Paystack retries on a non-2xx/timeout, and confirm_payment does a
    network round-trip (verify_transaction) too slow to do inline safely.

    Reuses billing.engine.confirm_payment(reference) rather than a second
    confirmation path - this handler's only job is to verify the
    signature and extract the reference from the event payload."""
    body = await request.body()

    signature = request.headers.get("x-paystack-signature", "")
    if not verify_paystack_signature(body, signature):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    envelope = await request.json()
    reference = (envelope.get("data") or {}).get("reference")
    if reference:
        background_tasks.add_task(billing_engine.confirm_payment, reference)

    return {"status": "accepted"}

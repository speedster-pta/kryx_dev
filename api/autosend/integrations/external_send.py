"""
integrations/external_send.py

POST /integrations/external-send - lets a separate, sister service running
on this same VPS (currently: the booking service for a practice's
appointment scheduling, its own repo/container, not part of this
codebase) trigger a WhatsApp and/or email send without going through
this app's own browser-session auth. Gated by X-Service-Key
(require_booking_service_key, auth.py) - a distinct shared secret from
admin_api_key on purpose, since this endpoint can cause a real message to
go out, unlike the read-only/diagnostic /ops/* routes that key guards.

Same ack-then-background_tasks shape as integrations/webhooks.py's PCO
handler: the caller gets a 202 immediately, the actual WhatsApp Graph API
call / SMTP send happens after the response so a slow send can't make the
caller's request hang or time out.

idempotency_key is required and is what a retry from the calling service
is expected to reuse - storage.already_sent() checks it per (key, source)
pair before sending, so a retried call after a timeout/ambiguous response
doesn't double-send a leg that already succeeded, while still attempting
a leg that didn't get that far the first time.
"""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from autosend import storage
from autosend.auth import require_booking_service_key
from autosend.clients import get_whatsapp_client_for_number
from autosend.integrations.mailer import MailerNotConfigured, send_email
from autosend.integrations.whatsapp import MessagingLimitExceeded, WhatsAppSendError
from autosend.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(
    prefix="/integrations/external-send",
    tags=["external_send"],
    dependencies=[Depends(require_booking_service_key)],
)

_WHATSAPP_SOURCE = "booking_service_whatsapp"
_EMAIL_SOURCE = "booking_service_email"


class WhatsAppLeg(BaseModel):
    to: str
    template_name: str
    parameters: list[str] = []
    number_id: int


class EmailLeg(BaseModel):
    to: str
    subject: str
    text_body: str
    html_body: str | None = None


class ExternalSendRequest(BaseModel):
    idempotency_key: str
    whatsapp: WhatsAppLeg | None = None
    email: EmailLeg | None = None


@router.post("")
async def send(payload: ExternalSendRequest, background_tasks: BackgroundTasks):
    if payload.whatsapp is None and payload.email is None:
        raise HTTPException(status_code=400, detail="At least one of whatsapp/email is required")

    if payload.whatsapp is not None:
        background_tasks.add_task(_send_whatsapp_leg, payload.idempotency_key, payload.whatsapp)
    if payload.email is not None:
        background_tasks.add_task(_send_email_leg, payload.idempotency_key, payload.email)

    return {"status": "queued"}


async def _send_whatsapp_leg(idempotency_key: str, leg: WhatsAppLeg) -> None:
    if storage.already_sent(idempotency_key, _WHATSAPP_SOURCE):
        logger.info("external_send: whatsapp leg for %s already sent, skipping", idempotency_key)
        return

    number = storage.get_whatsapp_number_by_id(leg.number_id)
    if number is None:
        storage.record_send(
            unit_id=None, source=_WHATSAPP_SOURCE, status="failed",
            whatsapp_number_id=leg.number_id, recipient_phone=leg.to,
            template_name=leg.template_name, error_message=f"No such whatsapp_number id {leg.number_id}",
            reference_id=idempotency_key,
        )
        return
    if not number["active"] or not number["unit_active"]:
        storage.record_send(
            unit_id=number["unit_id"], source=_WHATSAPP_SOURCE, status="failed",
            whatsapp_number_id=leg.number_id, recipient_phone=leg.to,
            template_name=leg.template_name, error_message="WhatsApp number or its unit is inactive",
            reference_id=idempotency_key,
        )
        return

    try:
        client = get_whatsapp_client_for_number(number)
        await client.send_template(leg.to, leg.template_name, *leg.parameters)
    except MessagingLimitExceeded as exc:
        storage.record_send(
            unit_id=number["unit_id"], source=_WHATSAPP_SOURCE, status="deferred",
            whatsapp_number_id=leg.number_id, recipient_phone=leg.to,
            template_name=leg.template_name, error_message=str(exc), reference_id=idempotency_key,
        )
    except Exception as exc:
        code = exc.code if isinstance(exc, WhatsAppSendError) else None
        logger.warning("external_send: whatsapp leg for %s failed: %s", idempotency_key, exc)
        storage.record_send(
            unit_id=number["unit_id"], source=_WHATSAPP_SOURCE, status="failed",
            whatsapp_number_id=leg.number_id, recipient_phone=leg.to,
            template_name=leg.template_name, error_code=code, error_message=str(exc),
            reference_id=idempotency_key,
        )
    else:
        storage.record_send(
            unit_id=number["unit_id"], source=_WHATSAPP_SOURCE, status="sent",
            whatsapp_number_id=leg.number_id, recipient_phone=leg.to,
            template_name=leg.template_name, reference_id=idempotency_key,
        )


async def _send_email_leg(idempotency_key: str, leg: EmailLeg) -> None:
    if storage.already_sent(idempotency_key, _EMAIL_SOURCE):
        logger.info("external_send: email leg for %s already sent, skipping", idempotency_key)
        return

    try:
        send_email(leg.to, leg.subject, leg.text_body, leg.html_body)
    except MailerNotConfigured as exc:
        storage.record_send(
            unit_id=None, source=_EMAIL_SOURCE, status="failed",
            recipient_phone=leg.to, error_message=str(exc), reference_id=idempotency_key,
        )
    except Exception as exc:
        logger.warning("external_send: email leg for %s failed: %s", idempotency_key, exc)
        storage.record_send(
            unit_id=None, source=_EMAIL_SOURCE, status="failed",
            recipient_phone=leg.to, error_message=str(exc), reference_id=idempotency_key,
        )
    else:
        storage.record_send(
            unit_id=None, source=_EMAIL_SOURCE, status="sent",
            recipient_phone=leg.to, reference_id=idempotency_key,
        )

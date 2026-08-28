"""
integrations/kryx_bookings.py

POST /integrations/kryx-bookings/send - lets a customer's own instance of
the standalone Kryx Bookings product (its own repo/container, not part of
this codebase) trigger a WhatsApp send on their org's behalf.

Unlike integrations/external_send.py (one shared X-Service-Key for a
single internal booking service), this is genuinely multi-tenant: any org
with the kryx_bookings module enabled generates its own per-unit API key
(storage/kryx_bookings.py, managed on the Kryx Bookings Settings admin
page) and passes it back as X-API-Key. The key is what identifies both
the caller (auth) and which unit/org/template it should send through -
there is no separate org_id in the request body, so a leaked key can only
ever send through the one unit it was issued for.

Same ack-then-background_tasks shape as external_send.py/webhooks.py: the
caller gets a 202 immediately, the real WhatsApp Graph API call happens
after the response so a slow send can't make the caller's request hang.

idempotency_key is required, same reasoning as external_send.py's -
storage.already_sent() is checked per (idempotency_key:automation_id,
source) before each individual automation's send, so a retried call after
a timeout doesn't double-send - scoped per automation, not just per
request, because one incoming event can now fan out to more than one
automation (see below) and a retry must be able to resume whichever
automation(s) hadn't gotten as far as "sent" yet without re-sending the
one(s) that had.

The five fields below (first_name, type, date_time, status, location) are
the fixed set of variables Kryx Bookings can ever offer - see
storage/kryx_bookings.py's BOOKING_VARIABLES. status doubles as a routing
key: each of the four BOOKING_STATUSES can have one or more configurable
automations (a booking being declined and a booking being confirmed are
different messages, not the same template with a substituted word, and
e.g. a "pending" booking can notify both the requester and a fixed
approver number as two independent automations), configured on the Kryx
Bookings Automations page (admin_pages.AutomationsView's
/automations/kryx-bookings). status is still also offered as a body
variable (mapped to its display label, e.g. "Approved") for a template
that wants to print it as text too.

Each automation's recipient_mode decides who "to" resolves to for that
automation specifically: "requester" (the default) sends to this
payload's own `to` field; "fixed" sends to the phone number configured on
that automation instead (storage/kryx_bookings.py's recipient_phone),
regardless of what `to` was - see _send_booking_whatsapp below.
"""

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from pydantic import BaseModel

from autosend import storage
from autosend.clients import resolve_whatsapp_client
from autosend.integrations.whatsapp import MessagingLimitExceeded, WhatsAppSendError
from autosend.template_variables import resolve_variable_lenient, resolve_variable_strict
from autosend.utils.logging import get_logger
from autosend.utils.phone import normalize_phone_e164

logger = get_logger(__name__)

router = APIRouter(prefix="/integrations/kryx-bookings", tags=["kryx_bookings"])

_SOURCE = "kryx_bookings"


async def _authenticate(x_api_key: str | None = Header(default=None)) -> dict:
    """Resolves the caller's connection from its API key. Deliberately
    only checks the key itself (exists + active) here - org-active/
    module-enabled checks happen in the background task, same as
    services/sme_metrics.py's process_inbound_email, so a temporarily
    disabled module doesn't leak an enumeration signal via a different
    HTTP status than "queued"."""
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing X-API-Key")
    connection = storage.get_kryx_bookings_connection_by_api_key(x_api_key)
    if connection is None or not connection["active"]:
        raise HTTPException(status_code=401, detail="Invalid or inactive X-API-Key")
    return connection


class BookingSendRequest(BaseModel):
    idempotency_key: str
    to: str
    first_name: str
    type: str
    date_time: str
    status: str
    location: str


@router.post("/send")
async def send(
    payload: BookingSendRequest,
    background_tasks: BackgroundTasks,
    connection: dict = Depends(_authenticate),
):
    if payload.status not in storage.BOOKING_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"status must be one of {storage.BOOKING_STATUSES}, got '{payload.status}'",
        )
    background_tasks.add_task(_send_booking_whatsapp, connection, payload)
    return {"status": "queued"}


async def _send_booking_whatsapp(connection: dict, payload: BookingSendRequest) -> None:
    unit = {"id": connection["unit_id"], "slug": connection["unit_slug"]}

    if not storage.is_enabled(connection["org_id"], storage.MODULE_KRYX_BOOKINGS):
        logger.info("[%s] kryx_bookings send but module is disabled for this org - dropping", unit["slug"])
        return
    if not storage.is_org_active(connection["org_id"]):
        logger.info("[%s] kryx_bookings send but org is inactive - dropping", unit["slug"])
        return
    if not storage.is_org_current(connection["org_id"]):
        logger.info("[%s] kryx_bookings send but org subscription is not active - dropping", unit["slug"])
        return

    automations = storage.list_active_booking_automations(connection["unit_id"], payload.status)
    if not automations:
        logger.error("[%s] kryx_bookings has no active '%s' automation configured", unit["slug"], payload.status)
        storage.record_send(
            unit_id=unit["id"], source=_SOURCE, status="failed",
            recipient_phone=payload.to,
            error_message=f"No active Kryx Bookings automation configured for status '{payload.status}'",
            reference_id=payload.idempotency_key,
        )
        return

    for automation in automations:
        await _send_one_automation(unit, connection, payload, automation)

    storage.touch_kryx_bookings_last_used(connection["id"])


async def _send_one_automation(unit: dict, connection: dict, payload: BookingSendRequest, automation: dict) -> None:
    # Scoped per automation, not just per request - see module docstring.
    reference_id = f"{payload.idempotency_key}:{automation['id']}"
    if storage.already_sent(reference_id, _SOURCE):
        logger.info("kryx_bookings: %s already sent, skipping", reference_id)
        return

    recipient = payload.to if automation["recipient_mode"] == "requester" else automation["recipient_phone"]
    phone = normalize_phone_e164(recipient, connection["default_region"])
    if not phone:
        storage.record_send(
            unit_id=unit["id"], source=_SOURCE, status="failed",
            template_name=automation["template_name"], recipient_phone=recipient,
            error_message="Could not parse recipient phone number", reference_id=reference_id,
        )
        return

    try:
        whatsapp_client = resolve_whatsapp_client(unit, automation)
    except ValueError as exc:
        storage.record_send(
            unit_id=unit["id"], source=_SOURCE, status="failed",
            template_name=automation["template_name"], recipient_phone=phone,
            error_message=str(exc), reference_id=reference_id,
        )
        return
    whatsapp_number_id = whatsapp_client.number.get("id") if whatsapp_client.number else None

    fields = {
        "first_name": payload.first_name, "type": payload.type, "date_time": payload.date_time,
        "status": storage.KRYX_BOOKINGS_STATUS_LABELS.get(payload.status, payload.status),
        "location": payload.location,
    }
    button_values = [
        resolve_variable_lenient(key, fields) if key else None
        for key in automation.get("button_variables") or []
    ]
    try:
        ordered_values = [resolve_variable_strict(var, fields) for var in automation["body_variable_order"]]
    except KeyError as missing:
        logger.error("[%s] kryx_bookings automation requires variable %s", unit["slug"], missing)
        storage.record_send(
            unit_id=unit["id"], source=_SOURCE, status="failed",
            template_name=automation["template_name"], whatsapp_number_id=whatsapp_number_id,
            recipient_phone=phone, error_message=f"Missing variable: {missing}",
            reference_id=reference_id,
        )
        return

    try:
        await whatsapp_client.send_template(
            phone, automation["template_name"], *ordered_values,
            header_image_url=automation.get("header_image_url"),
            button_values=button_values,
        )
    except MessagingLimitExceeded as exc:
        storage.record_send(
            unit_id=unit["id"], source=_SOURCE, status="deferred",
            template_name=automation["template_name"], whatsapp_number_id=whatsapp_number_id,
            recipient_phone=phone, error_message=str(exc), reference_id=reference_id,
        )
        return
    except Exception as exc:
        code = exc.code if isinstance(exc, WhatsAppSendError) else None
        logger.warning("[%s] kryx_bookings send for %s failed: %s", unit["slug"], reference_id, exc)
        storage.record_send(
            unit_id=unit["id"], source=_SOURCE, status="failed",
            template_name=automation["template_name"], whatsapp_number_id=whatsapp_number_id,
            recipient_phone=phone, error_code=code, error_message=str(exc),
            reference_id=reference_id,
        )
        return

    storage.record_send(
        unit_id=unit["id"], source=_SOURCE, status="sent",
        template_name=automation["template_name"], whatsapp_number_id=whatsapp_number_id,
        recipient_phone=phone, reference_id=reference_id,
    )

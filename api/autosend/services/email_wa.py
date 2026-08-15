"""
services/email_wa.py

Orchestrates one inbound email through to a WhatsApp send: resolve which
integration a receiving address belongs to, identify+parse the email via
that integration's provider, normalise the phone field, resolve the
target WhatsApp template's body_variable_order/button_variables against
the parsed fields (the same mechanism registration_poller.py/
form_response.py use for PCO automations), and send. Mirrors
services/people_forms.py + services/form_response.py combined.
"""

from autosend import storage
from autosend.clients import resolve_whatsapp_client
from autosend.integrations.email_wa.providers import PROVIDERS, UnparseableEmail
from autosend.integrations.whatsapp import MessagingLimitExceeded, WhatsAppSendError
from autosend.template_variables import resolve_variable_lenient, resolve_variable_strict
from autosend.utils.logging import get_logger
from autosend.utils.phone import normalize_phone_e164

logger = get_logger(__name__)


def _record(unit: dict, status: str, *, phone=None, template_name=None, whatsapp_number_id=None,
            error_code=None, error_message=None, reference_id=None) -> None:
    storage.record_send(
        unit_id=unit["id"], source="email_wa", status=status,
        whatsapp_number_id=whatsapp_number_id, recipient_phone=phone, template_name=template_name,
        error_code=error_code, error_message=error_message, reference_id=reference_id,
    )


async def process_inbound_email(local_part: str, body_text: str, dedup_key: str) -> None:
    """dedup_key is also used as send_log's reference_id, so a send can be
    cross-referenced against processed_inbound_emails the same way
    people_forms.py's submission_id links send_log to
    processed_form_submissions."""
    if storage.is_inbound_email_processed(dedup_key):
        logger.info("Inbound email %s already processed, skipping (likely a SendGrid redelivery)", dedup_key)
        return

    integration = storage.get_email_integration_by_local_part(local_part)
    if not integration or not integration["active"]:
        logger.info("Inbound email to unknown/inactive local_part '%s' - dropping", local_part)
        return

    if not storage.is_enabled(integration["org_id"], storage.MODULE_EMAIL_WA):
        logger.info(
            "[%s] Inbound email for %s/%s but email_wa module is disabled for this org - dropping",
            integration["unit_slug"], integration["provider_key"], integration["email_type"],
        )
        return

    provider = PROVIDERS.get(integration["provider_key"])
    if provider is None:
        logger.error("Inbound email for unknown provider_key '%s' - dropping", integration["provider_key"])
        storage.mark_inbound_email_processed(
            dedup_key, status="failed", detail=f"Unknown provider '{integration['provider_key']}'"
        )
        return

    unit = {"id": integration["unit_id"], "slug": integration["unit_slug"]}

    try:
        identified_type = provider.identify_email_type(body_text)
        if identified_type != integration["email_type"]:
            raise UnparseableEmail(
                f"this address is configured for email_type '{integration['email_type']}', "
                f"but this email looks like '{identified_type}'"
            )
        fields = provider.parse(integration["email_type"], body_text)
    except UnparseableEmail as exc:
        logger.warning(
            "[%s] Could not parse %s/%s email: %s",
            integration["unit_slug"], integration["provider_key"], integration["email_type"], exc,
        )
        storage.mark_inbound_email_processed(dedup_key, status="failed", detail=str(exc))
        return

    spec = provider.EMAIL_TYPES[integration["email_type"]]
    raw_phone = fields.get(spec.phone_field)
    phone = normalize_phone_e164(raw_phone, integration["default_region"]) if raw_phone else None
    if not phone:
        logger.warning(
            "[%s] %s/%s email has no usable phone number (raw=%r) - dropping",
            integration["unit_slug"], integration["provider_key"], integration["email_type"], raw_phone,
        )
        storage.mark_inbound_email_processed(dedup_key, status="failed", detail="No usable phone number")
        return

    template = storage.get_template_by_id(integration["whatsapp_template_id"])
    if not template:
        logger.error(
            "[%s] email_integration %s points at a missing/inactive template id %s",
            integration["unit_slug"], integration["id"], integration["whatsapp_template_id"],
        )
        storage.mark_inbound_email_processed(dedup_key, status="failed", detail="No active template found")
        return

    try:
        whatsapp_client = resolve_whatsapp_client(unit, template)
    except ValueError as exc:
        _record(unit, "failed", phone=phone, template_name=template["template_name"],
                error_message=str(exc), reference_id=dedup_key)
        storage.mark_inbound_email_processed(dedup_key, status="failed", detail=str(exc))
        return
    whatsapp_number_id = whatsapp_client.number.get("id") if whatsapp_client.number else None

    try:
        ordered_values = [resolve_variable_strict(var, fields) for var in template["body_variable_order"]]
    except KeyError as missing:
        logger.error(
            "[%s] Template %s requires variable %s, which this %s/%s email doesn't provide. Available: %s",
            integration["unit_slug"], template["template_name"], missing,
            integration["provider_key"], integration["email_type"], list(fields),
        )
        _record(unit, "failed", phone=phone, template_name=template["template_name"],
                whatsapp_number_id=whatsapp_number_id, error_message=f"Missing variable: {missing}",
                reference_id=dedup_key)
        storage.mark_inbound_email_processed(dedup_key, status="failed", detail=f"Missing variable: {missing}")
        return

    button_values = [
        resolve_variable_lenient(key, fields) if key else None
        for key in template.get("button_variables") or []
    ]

    try:
        await whatsapp_client.send_template(
            phone, template["template_name"], *ordered_values,
            header_image_url=template.get("header_image_url"),
            button_values=button_values,
        )
    except MessagingLimitExceeded as exc:
        # Unlike registration_poller.py's deferred sends (re-checked on its
        # next poll cycle), there is no periodic job that re-attempts a
        # pushed-webhook-triggered email send - this is a known limitation,
        # not a design choice: a 24h-limit rejection here is currently a
        # hard failure, not a genuine defer/retry.
        _record(unit, "deferred", phone=phone, template_name=template["template_name"],
                whatsapp_number_id=whatsapp_number_id, error_message=str(exc), reference_id=dedup_key)
        storage.mark_inbound_email_processed(dedup_key, status="failed", detail=str(exc))
        return
    except Exception as exc:
        code = exc.code if isinstance(exc, WhatsAppSendError) else None
        _record(unit, "failed", phone=phone, template_name=template["template_name"],
                whatsapp_number_id=whatsapp_number_id, error_code=code, error_message=str(exc),
                reference_id=dedup_key)
        storage.mark_inbound_email_processed(dedup_key, status="failed", detail=str(exc))
        return

    _record(unit, "sent", phone=phone, template_name=template["template_name"],
            whatsapp_number_id=whatsapp_number_id, reference_id=dedup_key)
    storage.mark_inbound_email_processed(dedup_key, status="sent")

    logger.info(
        "[%s] Sent %s WhatsApp to %s via email_wa/%s/%s",
        integration["unit_slug"], template["template_name"], phone,
        integration["provider_key"], integration["email_type"],
    )

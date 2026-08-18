from autosend.clients import get_pco_client, resolve_whatsapp_client
from autosend.integrations.whatsapp import MessagingLimitExceeded, WhatsAppSendError
from autosend import storage
from autosend.template_variables import resolve_variable_lenient, resolve_variable_strict
from autosend.utils.logging import get_logger
from autosend.utils.phone import normalize_phone_e164

logger = get_logger(__name__)


class FormConfirmationError(Exception):
    """Raised for the known, non-exceptional reasons a form confirmation
    can't be sent (no phone, no template, missing body/button variable).
    Distinct from WhatsAppSendError/etc. only in that these are detected
    before any send attempt - but callers should treat it the same as
    any other failure: str(exc) is a human-readable reason suitable for
    logging and for storage.mark_form_submission_processed(..., detail=...)."""


def _record(unit, status, *, phone=None, template_name=None, whatsapp_number_id=None,
            error_code=None, error_message=None, reference_id=None):
    storage.record_send(
        unit_id=unit["id"], source="form_webhook", status=status,
        whatsapp_number_id=whatsapp_number_id, recipient_phone=phone, template_name=template_name,
        error_code=error_code, error_message=error_message, reference_id=reference_id,
    )


async def send_form_confirmation(
    unit: dict, person_id: str, whatsapp_template_id: int, reference_id: str | None = None,
) -> None:
    """reference_id (optional): the PCO form submission_id this send is
    for, if the caller has one - purely so send_log rows can be
    cross-referenced against people_forms.py's own dedup marking. Not
    required; existing callers that don't pass it just get a NULL
    reference_id in the history.

    Raises FormConfirmationError if the person has no phone, the
    template is missing, or a required body/button variable can't be
    resolved. Raises MessagingLimitExceeded/WhatsAppSendError (or lets
    them propagate) on actual send failures. In every failure case a
    send_log row is recorded before the exception is raised - callers
    should not treat "no exception" as the only signal of success."""
    pco_client = get_pco_client(unit)

    person = await pco_client.get_person(person_id)
    phone = await pco_client.get_person_phone(person_id)

    if not phone:
        logger.warning(
            "[%s] Cannot send WhatsApp for template_id %s: person %s has no phone number",
            unit["slug"], whatsapp_template_id, person_id,
        )
        _record(unit, "failed", error_message="No phone number on file", reference_id=reference_id)
        raise FormConfirmationError("No phone number on file")

    template = storage.get_template_by_id(whatsapp_template_id)
    if not template:
        logger.error(
            "[%s] No active template found for id %s", unit["slug"], whatsapp_template_id
        )
        _record(unit, "failed", phone=phone, error_message="No active template found", reference_id=reference_id)
        raise FormConfirmationError("No active template found")

    whatsapp_client = resolve_whatsapp_client(unit, template)
    whatsapp_number_id = whatsapp_client.number.get("id") if whatsapp_client.number else None

    # PCO's own e164 field is normally already valid (see utils/phone.py
    # docstring), but re-validate/reformat defensively here in case a
    # person's PCO record has an incomplete/malformed phone entry - treat
    # that the same as no phone on file rather than sending an unvalidated
    # string to WhatsApp.
    default_region = (whatsapp_client.number or {}).get("default_region", "ZA")
    phone = normalize_phone_e164(phone, default_region)
    if not phone:
        logger.warning(
            "[%s] Cannot send WhatsApp for template_id %s: person %s has an invalid phone number",
            unit["slug"], whatsapp_template_id, person_id,
        )
        _record(unit, "failed", template_name=template["template_name"],
                whatsapp_number_id=whatsapp_number_id, error_message="Phone number on file is not a valid phone number",
                reference_id=reference_id)
        raise FormConfirmationError("Phone number on file is not a valid phone number")

    attrs = person["data"]["attributes"]
    available_fields = {
        "first_name": attrs.get("first_name") or attrs.get("name", ""),
        "last_name": attrs.get("last_name", ""),
        "name": attrs.get("name", ""),
    }

    try:
        ordered_values = [resolve_variable_strict(var, available_fields) for var in template["body_variable_order"]]
    except KeyError as missing:
        logger.error(
            "[%s] Template %s requires variable %s, which isn't available from person data - "
            "cannot send. Available: %s",
            unit["slug"], template["template_name"], missing, list(available_fields),
        )
        _record(
            unit, "failed", phone=phone, template_name=template["template_name"],
            whatsapp_number_id=whatsapp_number_id, error_message=f"Missing variable: {missing}",
            reference_id=reference_id,
        )
        raise FormConfirmationError(f"Missing variable: {missing}")

    # button_variables is a JSON array parallel to the template's button
    # list (one entry per button position); blank/falsy entries mean that
    # button has no dynamic URL variable. Unlike the free/paid registration
    # path (which just skips a button whose field isn't available), this
    # aborts the whole send on a missing field - matching the existing
    # strictness of the body-variable resolution above.
    try:
        button_values = [
            resolve_variable_strict(key, available_fields) if key else None
            for key in template.get("button_variables") or []
        ]
    except KeyError as missing:
        logger.error(
            "[%s] Template %s button requires variable %s, which isn't available from person "
            "data - cannot send. Available: %s",
            unit["slug"], template["template_name"], missing, list(available_fields),
        )
        _record(
            unit, "failed", phone=phone, template_name=template["template_name"],
            whatsapp_number_id=whatsapp_number_id, error_message=f"Missing button variable: {missing}",
            reference_id=reference_id,
        )
        raise FormConfirmationError(f"Missing button variable: {missing}")

    try:
        response = await whatsapp_client.send_template(
            phone,
            template["template_name"],
            *ordered_values,
            header_image_url=template.get("header_image_url"),
            button_values=button_values,
        )
    except MessagingLimitExceeded as exc:
        _record(
            unit, "deferred", phone=phone, template_name=template["template_name"],
            whatsapp_number_id=whatsapp_number_id, error_message=str(exc), reference_id=reference_id,
        )
        raise
    except Exception as exc:
        code = exc.code if isinstance(exc, WhatsAppSendError) else None
        _record(
            unit, "failed", phone=phone, template_name=template["template_name"],
            whatsapp_number_id=whatsapp_number_id, error_code=code, error_message=str(exc),
            reference_id=reference_id,
        )
        raise

    _record(
        unit, "sent", phone=phone, template_name=template["template_name"],
        whatsapp_number_id=whatsapp_number_id, reference_id=reference_id,
    )

    logger.info(
        "[%s] Sent %s WhatsApp to %s (%s)",
        unit["slug"], template["template_name"], available_fields["first_name"], phone,
    )

    logger.debug("WhatsApp response: %s", response)
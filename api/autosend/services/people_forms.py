import json

from autosend import storage
from autosend.services.form_response import send_form_confirmation
from autosend.utils.logging import get_logger

logger = get_logger(__name__)


async def process_people_form(unit: dict, envelope: dict) -> None:
    try:
        delivery = envelope["data"][0]
        payload = json.loads(delivery["attributes"]["payload"])
        submission = payload["data"]
        submission_id = submission["id"]
        person_id = submission["relationships"]["person"]["data"]["id"]
        form_id = submission["relationships"]["form"]["data"]["id"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        logger.exception("Malformed people-form webhook payload, dropping: %s", envelope)
        return

    whatsapp_template_id = storage.get_form_whatsapp_template_id(unit["id"], form_id)
    if not whatsapp_template_id:
        logger.info(
            "[%s] Submission %s is for form %s, which has no configured template mapping - ignoring",
            unit["slug"], submission_id, form_id,
        )
        return

    if storage.is_form_submission_processed(submission_id):
        logger.info(
            "Submission %s already processed, skipping (likely a PCO redelivery)",
            submission_id,
        )
        return

    logger.info(
        "[%s] People form webhook: submission=%s person=%s form=%s template_id=%s",
        unit["slug"], submission_id, person_id, form_id, whatsapp_template_id,
    )

    try:
        await send_form_confirmation(unit, person_id, whatsapp_template_id, reference_id=submission_id)
        storage.mark_form_submission_processed(submission_id, person_id, status="sent")
    except Exception as exc:
        logger.exception(
            "Failed to process form submission %s (person %s) - check /ops/failures",
            submission_id, person_id,
        )
        storage.mark_form_submission_processed(
            submission_id, person_id, status="failed", detail=str(exc)
        )

"""Bulk WhatsApp campaign sending, as a page in the shared admin UI.

Adapted from wa-campaign-manager's campaigns_router.py. A "campaign" sends
from a specific WhatsAppNumber (autosend.storage /
admin.WhatsAppNumber) rather than wa-campaign-manager's own
whatsapp_numbers/user_numbers tables. Access is unit-scoped the
same way SQLAdmin already scopes everything else - a user sees every
number under every unit they're assigned to (user["unit_ids"] /
is_superadmin, see web.auth.get_current_web_user) and picks one per
campaign, rather than numbers being individually assigned per-user like
wa-campaign-manager did.

Recipient parsing (CSV/Excel/Google Sheets/OneDrive), the actual send loop,
number/template lookups, and account self-service now live in
recipient_import.py, campaign_runner.py, numbers_router.py, and
account_router.py respectively - this file is just the campaign CRUD API.
"""
import threading
from datetime import datetime

import anyio
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from autosend import storage
from autosend.web import whatsapp_bulk
from autosend.web.auth import get_current_web_user
from autosend.web.campaign_runner import _run_campaign
from autosend.web.numbers_router import _get_number_if_authorized
from autosend.web.recipient_import import _load_recipient_rows

router = APIRouter()


@router.post("/api/campaigns/{campaign_id}/cancel")
def cancel_campaign(campaign_id: int, user: dict = Depends(get_current_web_user)):
    campaign = storage.get_campaign(campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if not user["is_superadmin"] and campaign["unit_id"] not in user["unit_ids"]:
        raise HTTPException(status_code=403, detail="Not your unit's campaign")

    status = campaign["status"]
    if status not in ("running", "scheduled", "throttled"):
        raise HTTPException(status_code=400, detail="Campaign is not running, scheduled, or throttled")

    if status == "throttled":
        # No send loop is polling for 'cancelling' on a paused campaign -
        # it's just sitting in the DB waiting for the periodic
        # throttle-recheck job. Finalize immediately, same reasoning as
        # the 'scheduled' case below.
        storage.clear_campaign_payload(campaign_id)
        storage.finalize_campaign_status(campaign_id, "cancelled")
        return {"status": "cancel_requested"}

    was_scheduled = status == "scheduled"
    ok = storage.request_campaign_cancel(campaign_id)
    if not ok:
        raise HTTPException(status_code=400, detail="Campaign is not running or scheduled")

    if was_scheduled:
        # No send loop is going to notice status='cancelling' for a
        # campaign that hasn't started yet, so cancel outright here: pull
        # the job and finalize immediately rather than leaving it stuck at
        # 'cancelling'.
        from autosend.scheduler import cancel_scheduled_job
        cancel_scheduled_job(campaign_id)
        storage.finalize_campaign_status(campaign_id, "cancelled")

    return {"status": "cancel_requested"}


@router.post("/api/campaigns")
async def create_campaign(
    number_id: int = Form(...),
    template_name: str = Form(...),
    language: str = Form(...),
    phone_column: str = Form("phone"),
    body_vars: str = Form(""),
    button_vars: str = Form(""),
    scheduled_at: str = Form(None),  # ISO 8601, e.g. "2026-08-05T09:00:00+02:00"
    recipients_file: UploadFile = File(None),
    sheet_link: str = Form(None),
    sheet_name: str = Form(None),
    image_file: UploadFile = File(None),
    user: dict = Depends(get_current_web_user),
):
    number = _get_number_if_authorized(user, number_id)
    if not storage.is_org_active(number.get("org_id")):
        raise HTTPException(
            status_code=403,
            detail="Your organisation is inactive - sending is disabled. Contact support to activate your account.",
        )
    if not storage.is_org_current(number.get("org_id")):
        raise HTTPException(
            status_code=403,
            detail="Your organisation's subscription isn't active - sending is disabled. Contact support to update billing.",
        )
    delay_seconds = number.get("send_delay_seconds", 0.0)
    token = number["access_token"]
    phone_number_id = number["phone_number_id"]

    rows = await _load_recipient_rows(recipients_file, sheet_link, sheet_name)
    if not rows:
        raise HTTPException(status_code=400, detail="No data rows found in the uploaded file/sheet")

    body_var_columns = [c.strip() for c in body_vars.split(",") if c.strip()]
    # Unlike body_var_columns, blank entries are kept here rather than
    # filtered out - each entry's position is a button index, and a blank
    # entry means "this button has no dynamic variable", not "skip it".
    button_var_columns = [c.strip() for c in button_vars.split(",")] if button_vars else []

    image_media_id = None
    if image_file is not None and image_file.filename:
        # image_file.file.read() and upload_media() are both synchronous
        # (file I/O, then a blocking requests.post to Meta's Graph API with
        # a 60s timeout) - this route is async def, so calling either
        # inline here would stall the event loop for the full duration,
        # same failure mode as WhatsAppClient._gate() had. Offload both to
        # a worker thread; unlike campaign sends themselves (which run on
        # a background threading.Thread further down), this happens before
        # that point, while the request is still being handled inline.
        image_bytes = await anyio.to_thread.run_sync(image_file.file.read)
        mime_type = image_file.content_type or "image/jpeg"
        image_media_id = await anyio.to_thread.run_sync(
            whatsapp_bulk.upload_media, token, phone_number_id, image_bytes, image_file.filename, mime_type
        )

    if scheduled_at:
        try:
            # JS's Date.toISOString() always ends in "Z", which
            # datetime.fromisoformat() only accepts from Python 3.11+ -
            # normalize it to an explicit UTC offset so this works on
            # older interpreters too.
            normalized = scheduled_at[:-1] + "+00:00" if scheduled_at.endswith("Z") else scheduled_at
            run_time = datetime.fromisoformat(normalized)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid scheduled_at timestamp")
        now = datetime.now(run_time.tzinfo) if run_time.tzinfo else datetime.now()
        if run_time <= now:
            raise HTTPException(status_code=400, detail="Scheduled time must be in the future")

        # Persisted so the send can be reconstructed by the scheduler after
        # a container restart - see storage.list_pending_scheduled_campaigns.
        payload = {
            "rows": rows,
            "body_var_columns": body_var_columns,
            "button_var_columns": button_var_columns,
            "phone_column": phone_column,
            "image_media_id": image_media_id,
        }
        campaign_id = storage.create_campaign(
            user["id"], number["unit_id"], number_id, template_name, language,
            len(rows), scheduled_at=normalized, payload=payload,
        )

        from autosend.scheduler import schedule_campaign
        schedule_campaign(campaign_id, normalized)

        return {"campaign_id": campaign_id, "total": len(rows), "status": "scheduled"}

    campaign_id = storage.create_campaign(
        user["id"], number["unit_id"], number_id, template_name, language, len(rows)
    )

    thread = threading.Thread(
        target=_run_campaign,
        args=(campaign_id, number,
              template_name, language, image_media_id, rows, body_var_columns,
              phone_column, delay_seconds, button_var_columns),
        daemon=True,
    )
    thread.start()

    return {"campaign_id": campaign_id, "total": len(rows), "status": "running"}


@router.get("/api/campaigns")
def list_campaigns(user: dict = Depends(get_current_web_user)):
    unit_ids = None if user["is_superadmin"] else user["unit_ids"]
    # paginated client-side in dashboard.html (10 per page) - this just
    # controls how far back that pagination can go
    return storage.list_campaigns(unit_ids, limit=200)


@router.get("/api/campaigns/{campaign_id}")
def campaign_detail(campaign_id: int, user: dict = Depends(get_current_web_user)):
    campaign = storage.get_campaign(campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if not user["is_superadmin"] and campaign["unit_id"] not in user["unit_ids"]:
        raise HTTPException(status_code=403, detail="Not your unit's campaign")
    recipients = campaign.pop("recipients")
    return {"campaign": campaign, "recipients": recipients}

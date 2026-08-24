"""API endpoints backing the single-page Automations UI (Free Registrations,
Paid Registrations, Form Responses). Unit/number/template dropdowns
and the message preview reuse the same scoping and /api/numbers, /api/templates
endpoints as the bulk-campaigns page (web/numbers_router.py) - this module
only adds what's specific to saving/listing the three sections' own records.
"""
import logging
import mimetypes
import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from pydantic import BaseModel

from autosend import storage
from autosend.storage.header_images import HEADER_IMAGES_DIR
from autosend.web.auth import get_current_web_user, pco_module_visible
from autosend.web.numbers_router import (
    _accessible_numbers,
    _accessible_unit_ids,
    _accessible_units,
    _check_number_access,
    _check_unit_access,
)

logger = logging.getLogger(__name__)


def _require_pco_module(request: Request, user: dict = Depends(get_current_web_user)) -> dict:
    """Router-level gate: every endpoint here is PCO-driven (registration/
    form/serving-reminder automations), so a disabled org must 403 even on
    a direct API call, not just have the page/nav link hidden - see
    web.auth.pco_module_visible for the shared check."""
    if not pco_module_visible(request):
        raise HTTPException(status_code=403, detail="Planning Center integration is not enabled for this organisation")
    return user


router = APIRouter(dependencies=[Depends(_require_pco_module)])

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # WhatsApp's own header-image limit is 5MB


@router.get("/api/automations/units")
def api_automations_units(user: dict = Depends(get_current_web_user)):
    # stitch_active: lets the Paid Registration section's variable picker
    # (automations.html) only offer "Stitch Suffix" for a unit that has
    # Stitch configured and switched on AND whose org has actually
    # enabled the Stitch module (storage.MODULE_STITCH) - a unit-level
    # "Active" checkbox alone isn't enough once Stitch is a billable,
    # opt-in integration rather than something every org gets for free.
    return [
        {
            "id": c["id"],
            "name": c["name"],
            "stitch_active": storage.is_enabled(c["org_id"], storage.MODULE_STITCH) and storage.is_stitch_active(c["id"]),
        }
        for c in _accessible_units(user)
    ]


@router.post("/api/automations/header-image")
async def api_upload_header_image(request: Request, file: UploadFile, user: dict = Depends(get_current_web_user)):
    """Saves the uploaded image under the persistent header_images dir and
    returns an absolute URL to it - WhatsApp's Graph API fetches header
    images by URL at send time (see integrations/whatsapp.py), so unlike
    the bulk-campaigns page's one-off per-send media upload, this has to
    be a stable link that still resolves whenever the automation actually
    fires, potentially months later."""
    content_type = file.content_type or mimetypes.guess_type(file.filename or "")[0]
    if content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=400, detail="Only JPEG, PNG, or WEBP images are allowed")

    data = await file.read()
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=400, detail="Image must be under 5MB")

    ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[content_type]
    filename = f"{secrets.token_hex(16)}{ext}"
    HEADER_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    (HEADER_IMAGES_DIR / filename).write_bytes(data)

    url = f"{str(request.base_url).rstrip('/')}/media/header-images/{filename}"
    return {"url": url}


class RegistrationTemplateIn(BaseModel):
    unit_id: int
    template_type: str  # "free_acknowledgment" | "payment_reminder"
    template_name: str
    body_variable_order: list[str] = []
    whatsapp_number_id: int
    # JSON array parallel to the template's button list (one entry per
    # button position); blank entries mean that button has no dynamic URL
    # variable. Replaces the old button_url_pattern (a static literal
    # string) field.
    button_variables: list[str] = []
    header_image_url: str | None = None
    active: bool = True


@router.get("/api/automations/registration-templates")
def api_list_registration_templates(template_type: str, user: dict = Depends(get_current_web_user)):
    if template_type not in storage.REGISTRATION_TEMPLATE_TYPES:
        raise HTTPException(status_code=400, detail=f"template_type must be one of {storage.REGISTRATION_TEMPLATE_TYPES}")
    return storage.list_registration_templates(_accessible_unit_ids(user), template_type)


@router.post("/api/automations/registration-templates")
def api_save_registration_template(payload: RegistrationTemplateIn, user: dict = Depends(get_current_web_user)):
    _check_unit_access(user, payload.unit_id)
    _check_number_access(user, payload.whatsapp_number_id)
    if payload.template_type not in storage.REGISTRATION_TEMPLATE_TYPES:
        raise HTTPException(status_code=400, detail=f"template_type must be one of {storage.REGISTRATION_TEMPLATE_TYPES}")
    template_id = storage.upsert_registration_template(
        unit_id=payload.unit_id,
        template_type=payload.template_type,
        template_name=payload.template_name,
        body_variable_order=payload.body_variable_order,
        whatsapp_number_id=payload.whatsapp_number_id,
        button_variables=payload.button_variables,
        header_image_url=payload.header_image_url,
        active=payload.active,
    )
    return {"id": template_id}


class FormMappingIn(BaseModel):
    id: int | None = None
    unit_id: int
    pco_form_id: str
    template_name: str
    body_variable_order: list[str] = []
    whatsapp_number_id: int
    button_variables: list[str] = []
    header_image_url: str | None = None
    active: bool = True


@router.get("/api/automations/form-mappings")
def api_list_form_mappings(user: dict = Depends(get_current_web_user)):
    return storage.list_form_mappings(_accessible_unit_ids(user))


@router.post("/api/automations/form-mappings")
def api_save_form_mapping(payload: FormMappingIn, user: dict = Depends(get_current_web_user)):
    _check_unit_access(user, payload.unit_id)
    _check_number_access(user, payload.whatsapp_number_id)
    if not payload.pco_form_id.strip():
        raise HTTPException(status_code=400, detail="PCO form ID is required")
    mapping_id = storage.upsert_form_mapping(
        mapping_id=payload.id,
        unit_id=payload.unit_id,
        pco_form_id=payload.pco_form_id.strip(),
        template_name=payload.template_name,
        body_variable_order=payload.body_variable_order,
        whatsapp_number_id=payload.whatsapp_number_id,
        button_variables=payload.button_variables,
        header_image_url=payload.header_image_url,
        active=payload.active,
    )
    return {"id": mapping_id}


@router.delete("/api/automations/form-mappings/{mapping_id}")
def api_delete_form_mapping(mapping_id: int, user: dict = Depends(get_current_web_user)):
    existing = {m["id"]: m for m in storage.list_form_mappings(_accessible_unit_ids(user))}
    if mapping_id not in existing:
        raise HTTPException(status_code=404, detail="Form mapping not found")
    storage.delete_form_mapping(mapping_id)
    return {"deleted": mapping_id}


# ---- Serving Reminders ----

def _unit_or_404(user: dict, unit_id: int) -> dict:
    _check_unit_access(user, unit_id)
    for c in _accessible_units(user):
        if c["id"] == unit_id:
            return c
    raise HTTPException(status_code=404, detail="Unit not found")


@router.get("/api/automations/service-types")
async def api_service_types(unit_id: int, user: dict = Depends(get_current_web_user)):
    """PCO Service Types for the rule editor's dropdown, scoped to this
    unit's campus via Services v2 folders and cached for the rest
    of the day - unlike get_service_types() (unscoped, deliberately never
    cached, same reasoning as /api/templates hitting Meta live), the
    folder-walk here is several paginated calls, so re-running it on
    every dropdown open isn't worth it. First selection of a unit
    each day pays for the poll; every later selection that day reads the
    cache."""
    from datetime import datetime, timezone
    from autosend.clients import get_pco_client

    unit = _unit_or_404(user, unit_id)
    if not unit.get("pco_campus_id"):
        raise HTTPException(status_code=400, detail="This unit has no PCO campus configured yet")

    today = datetime.now(timezone.utc).date().isoformat()
    cached = storage.get_cached_service_types(unit_id, today)
    if cached is not None:
        return cached

    try:
        pco_client = get_pco_client(unit)
        service_types = await pco_client.get_service_types_for_campus(unit["pco_campus_id"])
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to fetch PCO service types for unit %s", unit_id)
        raise HTTPException(status_code=502, detail=f"Failed to fetch service types from Planning Center: {exc}")

    storage.set_cached_service_types(unit_id, service_types, today)
    return service_types


@router.get("/api/automations/teams")
async def api_teams(unit_id: int, pco_service_type_id: str, user: dict = Depends(get_current_web_user)):
    """PCO Teams for the rule editor's team-filter checklist, scoped to
    the selected service type and cached for the rest of the day - same
    reasoning and shape as /api/automations/service-types above."""
    from datetime import datetime, timezone
    from autosend.clients import get_pco_client

    unit = _unit_or_404(user, unit_id)

    today = datetime.now(timezone.utc).date().isoformat()
    cached = storage.get_cached_teams(unit_id, pco_service_type_id, today)
    if cached is not None:
        return cached

    try:
        pco_client = get_pco_client(unit)
        teams = await pco_client.get_teams_for_service_type(pco_service_type_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to fetch PCO teams for unit %s / service type %s", unit_id, pco_service_type_id)
        raise HTTPException(status_code=502, detail=f"Failed to fetch teams from Planning Center: {exc}")

    storage.set_cached_teams(unit_id, pco_service_type_id, teams, today)
    return teams


class ServingRuleIn(BaseModel):
    id: int | None = None
    unit_id: int
    pco_service_type_id: str
    pco_service_type_name: str
    schedule_type: str = "weekly"  # "weekly" | "monthly" | "immediate"
    send_day_of_week: str | None = None  # 'mon'..'sun', required when schedule_type='weekly'
    send_day_of_month: int | None = None  # 1..31, required when schedule_type='monthly'
    send_time: str | None = None  # "HH:MM", required unless schedule_type='immediate'
    timezone: str = "Africa/Johannesburg"
    status_filter: str = "confirmed_only"  # "confirmed_only" | "all_scheduled"
    template_name: str
    body_variable_order: list[str] = []
    whatsapp_number_id: int
    button_variables: list[str] = []
    header_image_url: str | None = None
    active: bool = True
    plan_selection_mode: str = "next_event"  # "next_event" | "days_ahead"
    days_ahead: int | None = None
    pco_team_ids: list[str] = []
    pco_team_names: list[str] = []


@router.get("/api/automations/serving-rules")
def api_list_serving_rules(user: dict = Depends(get_current_web_user)):
    return storage.list_serving_rules(_accessible_unit_ids(user))


@router.post("/api/automations/serving-rules")
async def api_save_serving_rule(payload: ServingRuleIn, user: dict = Depends(get_current_web_user)):
    from autosend.scheduler import schedule_serving_rule, cancel_serving_rule_job
    from autosend.services.serving_reminder import run_serving_reminder_rule

    _check_unit_access(user, payload.unit_id)
    _check_number_access(user, payload.whatsapp_number_id)
    if payload.status_filter not in storage.SERVING_STATUS_FILTERS:
        raise HTTPException(status_code=400, detail=f"status_filter must be one of {storage.SERVING_STATUS_FILTERS}")
    if payload.plan_selection_mode not in storage.SERVING_PLAN_SELECTION_MODES:
        raise HTTPException(status_code=400, detail=f"plan_selection_mode must be one of {storage.SERVING_PLAN_SELECTION_MODES}")
    if payload.plan_selection_mode == "days_ahead" and (not payload.days_ahead or payload.days_ahead < 1):
        raise HTTPException(status_code=400, detail="days_ahead must be a positive integer when plan_selection_mode is 'days_ahead'")

    if payload.schedule_type not in storage.SERVING_SCHEDULE_TYPES:
        raise HTTPException(status_code=400, detail=f"schedule_type must be one of {storage.SERVING_SCHEDULE_TYPES}")
    send_day_of_week = None
    if payload.schedule_type == "weekly":
        if not payload.send_day_of_week or payload.send_day_of_week.lower() not in {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}:
            raise HTTPException(status_code=400, detail="send_day_of_week must be one of mon/tue/wed/thu/fri/sat/sun")
        if not payload.send_time:
            raise HTTPException(status_code=400, detail="send_time is required when schedule_type is 'weekly'")
        send_day_of_week = payload.send_day_of_week.lower()
    elif payload.schedule_type == "monthly":
        if not payload.send_day_of_month or not (1 <= payload.send_day_of_month <= 31):
            raise HTTPException(status_code=400, detail="send_day_of_month must be between 1 and 31 when schedule_type is 'monthly'")
        if not payload.send_time:
            raise HTTPException(status_code=400, detail="send_time is required when schedule_type is 'monthly'")

    rule_id = storage.upsert_serving_rule(
        rule_id=payload.id,
        unit_id=payload.unit_id,
        pco_service_type_id=payload.pco_service_type_id,
        pco_service_type_name=payload.pco_service_type_name,
        send_day_of_week=send_day_of_week,
        send_time=payload.send_time,
        timezone_name=payload.timezone,
        status_filter=payload.status_filter,
        template_name=payload.template_name,
        body_variable_order=payload.body_variable_order,
        whatsapp_number_id=payload.whatsapp_number_id,
        button_variables=payload.button_variables,
        header_image_url=payload.header_image_url,
        active=payload.active,
        plan_selection_mode=payload.plan_selection_mode,
        days_ahead=payload.days_ahead,
        schedule_type=payload.schedule_type,
        send_day_of_month=payload.send_day_of_month,
        pco_team_ids=payload.pco_team_ids,
        pco_team_names=payload.pco_team_names,
    )

    if payload.schedule_type == "immediate":
        # No recurring job to register - saving an immediate-schedule rule
        # IS the send trigger. Always cancel any leftover job first: a
        # rule previously saved as weekly/monthly and just switched to
        # immediate must not keep firing on its old recurring schedule.
        cancel_serving_rule_job(rule_id)
        send_result = await run_serving_reminder_rule(rule_id)
        return {"id": rule_id, "send_result": send_result}

    if payload.active:
        schedule_serving_rule(storage.get_serving_rule_by_id(rule_id))
    else:
        cancel_serving_rule_job(rule_id)

    return {"id": rule_id}


@router.delete("/api/automations/serving-rules/{rule_id}")
def api_delete_serving_rule(rule_id: int, user: dict = Depends(get_current_web_user)):
    from autosend.scheduler import cancel_serving_rule_job

    existing = {r["id"]: r for r in storage.list_serving_rules(_accessible_unit_ids(user))}
    if rule_id not in existing:
        raise HTTPException(status_code=404, detail="Serving reminder rule not found")
    cancel_serving_rule_job(rule_id)
    storage.delete_serving_rule(rule_id)
    return {"deleted": rule_id}


@router.post("/api/automations/serving-rules/{rule_id}/send-now")
async def api_send_serving_rule_now(rule_id: int, user: dict = Depends(get_current_web_user)):
    """Manual trigger: runs the rule immediately against whatever the
    next upcoming plan currently is, regardless of the rule's active flag
    or its scheduled day/time. Idempotent the same way the scheduled job
    is - anyone already reminded for that plan under this rule is skipped,
    not re-sent."""
    from autosend.services.serving_reminder import run_serving_reminder_rule

    existing = {r["id"]: r for r in storage.list_serving_rules(_accessible_unit_ids(user))}
    if rule_id not in existing:
        raise HTTPException(status_code=404, detail="Serving reminder rule not found")

    result = await run_serving_reminder_rule(rule_id)
    if "error" in result:
        raise HTTPException(status_code=502, detail=result["error"])
    return result


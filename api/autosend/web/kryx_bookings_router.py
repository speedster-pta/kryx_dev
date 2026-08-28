"""API endpoints backing the Kryx Bookings automations page's per-status
tabs (admin_pages.AutomationsView's /automations/kryx-bookings,
kryx_bookings_automations.html). Mirrors automations_router.py's
registration-templates endpoints closely, just keyed by Kryx Bookings
status instead of a PCO registration template_type, and - unlike that
one-row-per-(unit, template_type) model - allows more than one automation
per (unit, status), each independently addressed by its own id (see
storage/kryx_bookings.py's module docstring for why). Unit/number/template
dropdowns and the message preview reuse the same /api/automations/units,
/api/numbers, /api/templates, and /api/automations/header-image endpoints
those PCO sections use.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from autosend import storage
from autosend.web.auth import get_current_web_user, kryx_bookings_module_visible
from autosend.web.numbers_router import _accessible_unit_ids, _check_number_access, _check_unit_access


def _require_kryx_bookings_module(request: Request, user: dict = Depends(get_current_web_user)) -> dict:
    """Router-level gate, same reasoning as automations_router.py's
    _require_pco_module - a disabled module must 403 even on a direct API
    call, not just have the page/nav link hidden."""
    if not kryx_bookings_module_visible(request):
        raise HTTPException(status_code=403, detail="The Kryx Bookings module is not enabled for this organisation")
    return user


router = APIRouter(dependencies=[Depends(_require_kryx_bookings_module)])


class KryxBookingsAutomationIn(BaseModel):
    id: int | None = None
    unit_id: int
    status: str
    template_name: str
    body_variable_order: list[str] = []
    whatsapp_number_id: int
    button_variables: list[str] = []
    header_image_url: str | None = None
    active: bool = True
    recipient_mode: str = "requester"
    recipient_phone: str | None = None


def _check_automation_access(user: dict, automation_id: int) -> dict:
    """404s if the automation itself doesn't exist; 403s (via
    _check_unit_access, same status code every other unit-scoped check in
    this router already uses) if it exists but belongs to a unit outside
    the caller's scope."""
    automation = storage.get_booking_automation(automation_id)
    if automation is None:
        raise HTTPException(status_code=404, detail="Not found")
    _check_unit_access(user, automation["unit_id"])
    return automation


@router.get("/api/kryx-bookings/templates")
def api_list_kryx_bookings_templates(status: str, user: dict = Depends(get_current_web_user)):
    if status not in storage.BOOKING_STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of {storage.BOOKING_STATUSES}")
    return storage.list_booking_automations(_accessible_unit_ids(user), status)


@router.post("/api/kryx-bookings/templates")
def api_save_kryx_bookings_template(payload: KryxBookingsAutomationIn, user: dict = Depends(get_current_web_user)):
    if payload.id is not None:
        _check_automation_access(user, payload.id)
    _check_unit_access(user, payload.unit_id)
    _check_number_access(user, payload.whatsapp_number_id)
    if payload.status not in storage.BOOKING_STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of {storage.BOOKING_STATUSES}")
    try:
        automation_id = storage.upsert_booking_automation(
            automation_id=payload.id,
            unit_id=payload.unit_id,
            status=payload.status,
            template_name=payload.template_name,
            body_variable_order=payload.body_variable_order,
            whatsapp_number_id=payload.whatsapp_number_id,
            button_variables=payload.button_variables,
            header_image_url=payload.header_image_url,
            active=payload.active,
            recipient_mode=payload.recipient_mode,
            recipient_phone=payload.recipient_phone,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"id": automation_id}


@router.delete("/api/kryx-bookings/templates/{automation_id}")
def api_delete_kryx_bookings_template(automation_id: int, user: dict = Depends(get_current_web_user)):
    _check_automation_access(user, automation_id)
    storage.delete_booking_automation(automation_id)
    return {"status": "deleted"}

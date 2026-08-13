"""WhatsApp number and template lookups used by the campaign UI.

Split out of campaigns_router.py - _accessible_numbers/_get_number_if_authorized
and the /api/numbers*, /api/templates endpoints aren't campaign-specific
(unit-scoped number access is a general concept), so they live
here rather than under "campaigns". campaigns_router.py imports
_get_number_if_authorized from this module for create_campaign.
"""
from fastapi import APIRouter, Depends, HTTPException

from autosend import storage, whatsapp_limits
from autosend.web import whatsapp_bulk
from autosend.web.auth import get_current_web_user

router = APIRouter()


def _accessible_numbers(user: dict) -> list[dict]:
    unit_ids = None if user["is_superadmin"] else user["unit_ids"]
    return storage.get_whatsapp_numbers(unit_ids)


def _get_number_if_authorized(user: dict, number_id: int) -> dict:
    for n in _accessible_numbers(user):
        if n["id"] == number_id:
            return n
    raise HTTPException(status_code=403, detail="You do not have access to this WhatsApp number")


@router.get("/api/numbers")
def api_numbers(unit_id: int | None = None, user: dict = Depends(get_current_web_user)):
    numbers = _accessible_numbers(user)
    if unit_id is not None:
        numbers = [n for n in numbers if n["unit_id"] == unit_id]
    return [
        {"id": n["id"], "unit_id": n["unit_id"], "unit_name": n["unit_name"], "label": f"{n['unit_name']} — {n['label']}"}
        for n in numbers
    ]


@router.get("/api/numbers/{number_id}/usage")
def api_number_usage(number_id: int, user: dict = Depends(get_current_web_user)):
    number = _get_number_if_authorized(user, number_id)
    return whatsapp_limits.usage_summary(number)


@router.get("/api/numbers/{number_id}/quality")
def api_number_quality(number_id: int, user: dict = Depends(get_current_web_user)):
    number = _get_number_if_authorized(user, number_id)
    return whatsapp_limits.quality_summary(number)


@router.get("/api/templates")
def api_templates(number_id: int, user: dict = Depends(get_current_web_user)):
    number = _get_number_if_authorized(user, number_id)
    waba_id = number.get("waba_id")
    if not waba_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "This WhatsApp number has no WhatsApp Business Account ID on file "
                "(waba_id). Add it in SQLAdmin under WhatsApp Numbers to list "
                "templates for bulk sending."
            ),
        )
    try:
        tmpls = whatsapp_bulk.fetch_templates(number["access_token"], waba_id)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return [
        {
            "id": t.get("id"),  # Meta's numeric template ID - required for edits
                                 # (POST /{template_id}), unlike create/delete which
                                 # key off waba_id+name instead.
            "name": t.get("name"),
            "language": t.get("language"),
            "status": t.get("status"),
            "category": t.get("category"),
            "components": t.get("components", []),
        }
        for t in tmpls
    ]

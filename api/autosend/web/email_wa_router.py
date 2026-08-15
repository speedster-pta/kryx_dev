"""API endpoints backing the Automations page's Email-to-WhatsApp section.

Mirrors automations_router.py's form-mapping endpoints (list/save/delete
over a per-unit JSON API, unit/number access re-checked per request) but
gated on the email_wa module rather than PCO - see
web.auth.email_wa_module_visible.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from starlette.requests import Request

from autosend import storage
from autosend.integrations.email_wa.providers import PROVIDERS, build_email_type_tabs
from autosend.web.auth import email_wa_module_visible, get_current_web_user
from autosend.web.numbers_router import _accessible_numbers

logger = logging.getLogger(__name__)


def _require_email_wa_module(request: Request, user: dict = Depends(get_current_web_user)) -> dict:
    if not email_wa_module_visible(request):
        raise HTTPException(status_code=403, detail="Email-to-WhatsApp integration is not enabled for this organisation")
    return user


router = APIRouter(dependencies=[Depends(_require_email_wa_module)])


def _accessible_unit_ids(user: dict) -> list[int] | None:
    return None if user["is_superadmin"] else user["unit_ids"]


def _accessible_units(user: dict) -> list[dict]:
    ids = _accessible_unit_ids(user)
    all_units = storage.get_active_units()
    if ids is None:
        return all_units
    id_set = set(ids)
    return [u for u in all_units if u["id"] in id_set]


def _check_unit_access(user: dict, unit_id: int) -> None:
    accessible_ids = {u["id"] for u in _accessible_units(user)}
    if unit_id not in accessible_ids:
        raise HTTPException(status_code=403, detail="You do not have access to this unit")


def _check_number_access(user: dict, whatsapp_number_id: int) -> None:
    accessible_ids = {n["id"] for n in _accessible_numbers(user)}
    if whatsapp_number_id not in accessible_ids:
        raise HTTPException(status_code=403, detail="You do not have access to this WhatsApp number")


@router.get("/api/email-wa/providers")
def api_email_wa_providers():
    """Provider/email_type registry as JSON - code, not DB data (see
    integrations/email_wa/providers/__init__.py), so this just exposes
    what's already registered rather than reading a table. `fields` is
    the ordered list of variable keys the Automations page's variable-order
    editor offers for that provider/email_type, mirroring how
    REGISTRATION_VARIABLES/FORM_VARIABLES/SERVING_VARIABLES feed the same
    editor for the PCO-driven sections."""
    return [
        {
            "key": provider.PROVIDER_KEY,
            "label": provider.LABEL,
            "email_types": build_email_type_tabs(provider),
        }
        for provider in PROVIDERS.values()
    ]


@router.get("/api/email-wa/integrations")
def api_list_email_integrations(user: dict = Depends(get_current_web_user)):
    return storage.list_email_integrations(_accessible_unit_ids(user))


class EmailIntegrationIn(BaseModel):
    unit_id: int
    provider_key: str
    email_type: str
    template_name: str
    body_variable_order: list[str] = []
    whatsapp_number_id: int
    button_variables: list[str] = []
    header_image_url: str | None = None
    active: bool = True


@router.post("/api/email-wa/integrations")
def api_save_email_integration(payload: EmailIntegrationIn, user: dict = Depends(get_current_web_user)):
    _check_unit_access(user, payload.unit_id)
    _check_number_access(user, payload.whatsapp_number_id)

    provider = PROVIDERS.get(payload.provider_key)
    if provider is None:
        raise HTTPException(status_code=400, detail=f"Unknown provider '{payload.provider_key}'")
    if payload.email_type not in provider.EMAIL_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"'{payload.email_type}' is not a supported email type for provider '{payload.provider_key}'",
        )

    result = storage.upsert_email_integration(
        unit_id=payload.unit_id,
        provider_key=payload.provider_key,
        email_type=payload.email_type,
        template_name=payload.template_name,
        body_variable_order=payload.body_variable_order,
        whatsapp_number_id=payload.whatsapp_number_id,
        button_variables=payload.button_variables,
        header_image_url=payload.header_image_url,
        active=payload.active,
    )
    return {"id": result["id"], "local_part": result["local_part"], "domain": _domain()}


@router.delete("/api/email-wa/integrations/{integration_id}")
def api_delete_email_integration(integration_id: int, user: dict = Depends(get_current_web_user)):
    existing = {row["id"]: row for row in storage.list_email_integrations(_accessible_unit_ids(user))}
    if integration_id not in existing:
        raise HTTPException(status_code=404, detail="Email integration not found")
    storage.delete_email_integration(integration_id)
    return {"deleted": integration_id}


def _domain() -> str:
    from autosend.config import settings

    return settings.email_wa_inbound_domain

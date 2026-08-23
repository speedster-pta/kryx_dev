"""API endpoints backing the Automations page's SME Metrics section.

Mirrors automations_router.py's form-mapping endpoints (list/save/delete
over a per-unit JSON API, unit/number access re-checked per request) but
gated on the sme_metrics module rather than PCO - see
web.auth.sme_metrics_module_visible.

Route paths (/api/sme-metrics/*) and the request/response shapes are new
(renamed from /api/email-wa/*) - unlike the webhook route/secret, these
are only ever called by this app's own JS (automations.html), never by an
external service, so renaming them carries none of the "breaks already-
configured infra" risk integrations/sme_metrics/webhook.py's docstring
describes.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from starlette.requests import Request

from autosend import storage
from autosend.integrations.sme_metrics.providers import PROVIDERS, build_email_type_tabs
from autosend.web.auth import get_current_web_user, sme_metrics_module_visible
from autosend.web.numbers_router import (
    _accessible_numbers,
    _accessible_unit_ids,
    _accessible_units,
    _check_number_access,
    _check_unit_access,
)

logger = logging.getLogger(__name__)


def _require_sme_metrics_module(request: Request, user: dict = Depends(get_current_web_user)) -> dict:
    if not sme_metrics_module_visible(request):
        raise HTTPException(status_code=403, detail="SME Metrics integration is not enabled for this organisation")
    return user


router = APIRouter(dependencies=[Depends(_require_sme_metrics_module)])


@router.get("/api/sme-metrics/providers")
def api_sme_metrics_providers():
    """Provider/email_type registry as JSON - code, not DB data (see
    integrations/sme_metrics/providers/__init__.py), so this just exposes
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


@router.get("/api/sme-metrics/integrations")
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


@router.post("/api/sme-metrics/integrations")
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


@router.delete("/api/sme-metrics/integrations/{integration_id}")
def api_delete_email_integration(integration_id: int, user: dict = Depends(get_current_web_user)):
    existing = {row["id"]: row for row in storage.list_email_integrations(_accessible_unit_ids(user))}
    if integration_id not in existing:
        raise HTTPException(status_code=404, detail="Email integration not found")
    storage.delete_email_integration(integration_id)
    return {"deleted": integration_id}


def _domain() -> str:
    from autosend.config import settings

    return settings.email_wa_inbound_domain

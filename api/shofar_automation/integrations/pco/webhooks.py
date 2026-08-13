"""
integrations/pco/webhooks.py

Webhook routes for PCO callbacks. Mounted unconditionally at the FastAPI
level (the URL always resolves — that's what lets Meta/PCO retries
behave sanely and lets us return clean 2xx/404s instead of connection
errors). Inside the handler, organisation/unit is resolved from the
URL/slug *before* any PCO logic runs, and the module-enabled check comes
before signature verification.

This generalises the "active=1 but no webhook secret => must fail
safely" pattern from the parent project (context seed §6) into the
broader "module disabled => safe no-op" rule: both cases now flow
through the same early-exit path rather than the old one being a
special-cased 404.
"""

from __future__ import annotations

import hashlib
import hmac
import logging

from fastapi import APIRouter, Header, HTTPException, Request

from shofar_automation.storage.units import get_unit
from shofar_automation.storage.modules import is_enabled
from shofar_automation.integrations.pco.storage import find_unit_id_by_webhook_secret, get_unit_settings
from shofar_automation.core.automation_engine import fire

logger = logging.getLogger("kryx.integrations.pco.webhooks")

router = APIRouter(prefix="/webhooks/planning-center", tags=["pco-webhooks"])


@router.post("/{org_slug}/{unit_id}")
async def pco_webhook(org_slug: str, unit_id: int, request: Request,
                       x_pco_signature: str | None = Header(default=None)):
    from shofar_automation.storage.organisations import get_organisation_by_slug

    # 1. Resolve organisation from the URL. Unknown slug => 404, same as
    #    any other not-found resource; nothing PCO-specific yet.
    org = get_organisation_by_slug(org_slug)
    if org is None:
        raise HTTPException(status_code=404)

    # 2. Module-enabled check comes BEFORE touching unit settings or
    #    verifying any signature. An org with the module off gets a
    #    clean no-op response — same shape whether it's "never set up
    #    PCO" or "turned it off yesterday."
    if not is_enabled(org.id, "pco"):
        logger.info("PCO webhook received for org_id=%s but module disabled — no-op", org.id)
        return {"status": "ignored", "reason": "module_disabled"}

    # 3. Resolve unit within that org. Cross-tenant guardrail: unit
    #    must belong to this org, not just exist.
    unit = get_unit(org.id, unit_id)
    if unit is None:
        raise HTTPException(status_code=404)

    settings = get_unit_settings(unit_id)
    if settings is None or not settings.pco_webhook_secret:
        # Module enabled at the org level but this unit was never
        # given PCO settings — safe no-op, not a 404, per the
        # generalised pattern from the context seed.
        logger.info("PCO webhook for unit_id=%s has no webhook secret configured — no-op", unit_id)
        return {"status": "ignored", "reason": "unit_not_configured"}

    body = await request.body()
    if not _valid_signature(body, settings.pco_webhook_secret, x_pco_signature):
        raise HTTPException(status_code=401, detail="invalid signature")

    payload = await request.json()
    trigger_key = _trigger_key_for_payload(payload)
    if trigger_key:
        fire(trigger_key, org.id, {"unit_id": unit_id, **payload})

    return {"status": "accepted"}


def _valid_signature(body: bytes, secret: str, signature_header: str | None) -> bool:
    if not signature_header:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def _trigger_key_for_payload(payload: dict) -> str | None:
    event_type = payload.get("type")
    mapping = {
        "registration.completed": "event.registration.confirmed",
        "form_submission.created": "pco.form.response.submitted",
    }
    return mapping.get(event_type)

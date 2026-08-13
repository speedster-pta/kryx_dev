"""
integrations/pco/automations.py

The three PCO-driven automations from the parent project, now
registered into core.automation_engine instead of being called directly
by core routers/webhooks. Core never imports send_registration_confirmation
etc. by name — it fires a generic trigger key and the engine dispatches
here, gated by organisation_modules("pco").

Actual WhatsApp sending still goes through the core transactional send
path (async WhatsAppClient) — that two-send-path separation (sync bulk
vs async transactional) is preserved per the inherited principles; PCO
automations are transactional-path callers, same as before.
"""

from __future__ import annotations

import logging
from typing import Any

from shofar_automation.core.automation_engine import register

logger = logging.getLogger("kryx.integrations.pco.automations")

MODULE_KEY = "pco"


def register_automations() -> None:
    register(MODULE_KEY, "event.registration.confirmed", handle_registration_confirmation)
    register(MODULE_KEY, "pco.form.response.submitted", handle_form_response)
    register(MODULE_KEY, "pco.serving.reminder.due", handle_serving_reminder)


def handle_registration_confirmation(org_id: int, payload: dict[str, Any]) -> None:
    """
    Free/paid event registration confirmations. payload is expected to
    carry recipient/event/unit details already resolved by whatever
    core code fired the trigger (webhook handler or scheduler tick) —
    this function's job is PCO-specific formatting + handoff to the
    transactional send path, not re-deriving org/unit context.
    """
    logger.info("PCO registration confirmation: org_id=%s payload=%s", org_id, payload)
    # from core.transactional_send import send_transactional_message
    # send_transactional_message(org_id, ...)


def handle_form_response(org_id: int, payload: dict[str, Any]) -> None:
    logger.info("PCO form response confirmation: org_id=%s payload=%s", org_id, payload)


def handle_serving_reminder(org_id: int, payload: dict[str, Any]) -> None:
    """
    Serving reminders (PCO Services). Deferred/retry handling for these
    lives in scheduler.py's recheck_deferred_serving_reminders job, which
    re-fires this same trigger — this handler stays idempotent-safe to
    call more than once for the same reminder.
    """
    logger.info("PCO serving reminder: org_id=%s payload=%s", org_id, payload)

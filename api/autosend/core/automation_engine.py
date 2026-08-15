"""
core/automation_engine.py

The extension point named in the target architecture doc (Core includes
"Automation engine" as its own box, separate from Integrations). This is
what lets integrations/pco/automations.py — and later, other
integrations — hook into trigger points without core routers or the
campaign sender ever importing PCO-specific functions directly.

Pattern: integrations call register() at startup with a module_key and
a trigger function. Core code that wants to fire automations calls
fire(trigger_key, ...) and the engine dispatches to every registered
handler for that trigger, but ONLY for organisations that have the
owning module enabled — checked here, once, so individual integrations
don't each need to re-implement the enablement check.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from autosend.storage.modules import is_enabled

logger = logging.getLogger("kryx.automation_engine")


class TriggerHandler(Protocol):
    def __call__(self, org_id: int, payload: dict[str, Any]) -> None: ...


@dataclass(frozen=True)
class _Registration:
    module_key: str
    handler: TriggerHandler


_registry: dict[str, list[_Registration]] = defaultdict(list)


def register(module_key: str, trigger_key: str, handler: TriggerHandler) -> None:
    """
    Called by an integration package at import/startup time, e.g.:

        automation_engine.register(
            "pco", "event.registration.confirmed", send_registration_confirmation
        )

    module_key gates every fire() dispatch through organisation_modules,
    so a disabled module's handlers are simply never called — no
    per-handler enablement checks needed inside integrations/pco.
    """
    _registry[trigger_key].append(_Registration(module_key=module_key, handler=handler))
    logger.info("Registered automation handler: %s -> module=%s", trigger_key, module_key)


def fire(trigger_key: str, org_id: int, payload: dict[str, Any]) -> None:
    """
    Core code calls this at trigger points (e.g. after a PCO webhook is
    parsed into a generic event, or after a campaign send completes) —
    it never needs to know which integration, if any, cares about this
    trigger for this org.
    """
    for reg in _registry.get(trigger_key, []):
        if not is_enabled(org_id, reg.module_key):
            continue
        try:
            reg.handler(org_id, payload)
        except Exception:
            logger.exception(
                "Automation handler failed: trigger=%s module=%s org_id=%s",
                trigger_key, reg.module_key, org_id,
            )

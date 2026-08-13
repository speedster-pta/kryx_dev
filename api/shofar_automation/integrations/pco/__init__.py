"""
integrations/pco/__init__.py

Explicit named re-exports, same rule as storage/__init__.py. This module
also IS the IntegrationModule this package exposes to integrations/
registry — init_schema / register_automations / register_scheduler_jobs
/ get_router / get_admin_views are the exact protocol methods
integrations/__init__.py's IntegrationModule Protocol expects, so
`import shofar_automation.integrations.pco as pco` gives the registry
everything it needs via module-level functions rather than a class
instance.
"""

from __future__ import annotations

import sqlite3

from shofar_automation.integrations.pco.schema import init_pco_schema
from shofar_automation.integrations.pco.automations import register_automations
from shofar_automation.integrations.pco.scheduler import register_scheduler_jobs
from shofar_automation.integrations.pco.webhooks import router
from shofar_automation.integrations.pco.admin import get_admin_views
from shofar_automation.integrations.pco.client import PcoClient
from shofar_automation.integrations.pco.storage import (
    get_org_settings,
    upsert_org_settings,
    get_unit_settings,
    upsert_unit_settings,
)

MODULE_KEY = "pco"


def init_schema(conn: sqlite3.Connection) -> None:
    init_pco_schema(conn)


def get_router():
    return router


__all__ = [
    "MODULE_KEY",
    "init_schema",
    "register_automations",
    "register_scheduler_jobs",
    "get_router",
    "get_admin_views",
    "PcoClient",
    "get_org_settings",
    "upsert_org_settings",
    "get_unit_settings",
    "upsert_unit_settings",
]

"""
integrations/__init__.py

Registry of installed integration packages. This is the ONE file in
integrations/ that core is allowed to import from (via core/migrations.py
and core/bootstrap.py) — it exposes metadata and setup hooks, never
integration internals. Adding a future non-PCO integration means adding
one entry here, not touching core files.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol
import sqlite3


class IntegrationModule(Protocol):
    def init_schema(self, conn: sqlite3.Connection) -> None: ...
    def register_automations(self) -> None: ...
    def register_scheduler_jobs(self, scheduler) -> None: ...
    def get_router(self):  # returns a FastAPI APIRouter, mounted unconditionally
        ...
    def get_admin_views(self) -> list:
        ...


@dataclass(frozen=True)
class IntegrationDescriptor:
    module_key: str
    display_name: str
    loader: Callable[[], IntegrationModule]


def _load_pco() -> IntegrationModule:
    import integrations.pco as pco
    return pco


INSTALLED_INTEGRATIONS: list[IntegrationDescriptor] = [
    IntegrationDescriptor(module_key="pco", display_name="Planning Center Online", loader=_load_pco),
    # Future integrations register here, e.g.:
    # IntegrationDescriptor(module_key="ccb", display_name="Church Community Builder", loader=_load_ccb),
]


def all_integrations() -> list[IntegrationModule]:
    return [d.loader() for d in INSTALLED_INTEGRATIONS]

"""
storage/scope.py

Generalises the parent project's `current_scope` ContextVar / row-level
scoping pattern to add an outer organisation_id above the existing
unit (ex-congregation) scope. Staff are always scoped to exactly one
organisation; unit scoping within it is optional and additive.

This is core-only. integrations/pco reads Scope.org_id when it needs to
resolve which organisation a webhook or scheduler tick belongs to, but
it never defines or mutates scope itself.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Scope:
    org_id: int
    unit_ids: Optional[tuple[int, ...]] = None  # None => all units in org
    is_super_admin: bool = False

    def unit_filter_sql(self, column: str = "unit_id") -> tuple[str, tuple]:
        """
        Returns (sql_fragment, params) to AND onto a WHERE clause.
        Super admins and org-wide staff (unit_ids is None) get no
        unit restriction, only the org boundary (applied separately).
        """
        if self.unit_ids is None:
            return "1=1", ()
        placeholders = ",".join("?" * len(self.unit_ids))
        return f"{column} IN ({placeholders})", self.unit_ids


_current_scope: ContextVar[Optional[Scope]] = ContextVar("current_scope", default=None)


def set_scope(scope: Scope) -> None:
    _current_scope.set(scope)


def get_scope() -> Scope:
    scope = _current_scope.get()
    if scope is None:
        raise RuntimeError(
            "No scope set on this context — every request handler must call "
            "set_scope() before touching organisation-scoped storage."
        )
    return scope


def require_org_id() -> int:
    """Convenience accessor — the one predicate every storage query needs."""
    return get_scope().org_id

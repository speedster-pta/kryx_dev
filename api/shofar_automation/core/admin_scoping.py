"""
core/admin_scoping.py

Generalises the parent project's ScopedModelView (SQLAdmin base class
for row-level scoping) to filter on organisation_id first, then
optionally unit_id within it. Integration admin views (e.g.
integrations/pco/admin.py) subclass this rather than sqladmin.ModelView
directly, so scoping logic lives in exactly one place.
"""

from __future__ import annotations

from sqlalchemy import select
from sqladmin import ModelView

from shofar_automation.storage.scope import get_scope


class ScopedModelView(ModelView):
    """
    Subclasses must set `org_column` (default "org_id") to the name of
    the column on their model that holds the organisation FK, and
    optionally `unit_column` if the model also needs unit-level
    filtering (e.g. campaigns, pco_unit_settings via a join).

    Super admins (scope.is_super_admin) bypass the org filter entirely
    so platform support/onboarding staff can see across tenants.
    """

    org_column: str = "org_id"
    unit_column: str | None = None

    def list_query(self, request):
        stmt = super().list_query(request)
        return self._apply_scope(stmt)

    def count_query(self, request):
        stmt = super().count_query(request)
        return self._apply_scope(stmt)

    def _apply_scope(self, stmt: select):
        scope = get_scope()
        if scope.is_super_admin:
            return stmt

        model = self.model
        if hasattr(model, self.org_column):
            stmt = stmt.where(getattr(model, self.org_column) == scope.org_id)

        if self.unit_column and scope.unit_ids is not None and hasattr(model, self.unit_column):
            stmt = stmt.where(getattr(model, self.unit_column).in_(scope.unit_ids))

        return stmt

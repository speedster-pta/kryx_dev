"""
Row-level scoping for SQLAdmin CRUD views: superadmins see every unit's
rows, unit-scoped staff see only rows belonging to their own unit(s) -
request.session["unit_ids"], set at login by
admin_auth.authenticate_staff_user(). Same scoping rule as
admin_views.UnitWebhookAdmin.accessible_units().

Subclasses set `unit_field` to the column name that identifies which
unit a row belongs to - a FK for most models (default "unit_id"), or a
model's own PK for Unit itself (UnitAdmin/UnitWebhookAdmin use
unit_field="id").

NOTE: admin_auth.py's `current_scope` docstring also describes this class
filtering relationship dropdowns (e.g. the unit picker) via
scaffold_form() - that override was not present in the last known-good
version of this file and has not been reconstructed here (no reference
implementation to recover it from). Until it's added, create/edit forms
on scoped views show every unit in dropdown fields regardless of the
current user's scope, even though list/count views below are correctly
row-scoped.
"""
from sqladmin import ModelView
from starlette.requests import Request


class ScopedModelView(ModelView):
    unit_field: str = "unit_id"

    def _scope(self, request: Request) -> tuple[bool, list[int]]:
        return (
            request.session.get("is_superadmin", False),
            request.session.get("unit_ids", []),
        )

    def _apply_scope(self, stmt, request: Request):
        is_superadmin, unit_ids = self._scope(request)
        if is_superadmin:
            return stmt
        column = getattr(self.model, self.unit_field)
        return stmt.where(column.in_(unit_ids))

    def list_query(self, request: Request):
        return self._apply_scope(super().list_query(request), request)

    def count_query(self, request: Request):
        return self._apply_scope(super().count_query(request), request)

"""
Row-level scoping for SQLAdmin CRUD views: superadmins see every unit's
rows, unit-scoped staff (plain staff AND org admins) see only rows
belonging to their own unit(s). For plain staff that's
request.session["unit_ids"], set at login by
admin_auth.authenticate_user(); for an org admin it's every unit in
their org, resolved live via web.auth.resolve_unit_ids() rather than a
static session value (so a unit created mid-session is visible without
re-login) - see that function's docstring. Same scoping rule as
admin_views.UnitWebhookAdmin.accessible_units().

Subclasses set `unit_field` to the column name that identifies which
unit a row belongs to - a FK for most models (default "unit_id"), or a
model's own PK for Unit itself (UnitAdmin/UnitWebhookAdmin use
unit_field="id").
"""
from typing import Any

from fastapi import HTTPException
from sqladmin import ModelView
from sqlalchemy.orm import Session
from starlette.requests import Request

from autosend.admin_models import engine
from autosend.web.auth import resolve_unit_ids


class VisibleIfAccessible:
    """Mix in (before ModelView/ScopedModelView/BaseView) for any admin
    view whose nav visibility should exactly mirror its own
    is_accessible - covers every view in this app where is_visible
    either delegated to is_accessible already, or duplicated its body
    verbatim instead of delegating."""

    def is_visible(self, request: Request) -> bool:
        return self.is_accessible(request)


class OrgScopedModelView(ModelView):
    """Row-level scoping for the handful of tables scoped directly by an
    org_id column rather than through a unit_id join (see ScopedModelView
    below for that case) - PCOOrganizationSettings and User. Superadmins
    see every org's rows; everyone else is restricted to their own
    session org_id, both via query filtering (list/count/edit-form/
    details) and, since sqladmin re-fetches a row by raw pk for
    update/delete instead of going through those queries, an explicit
    re-check before either.

    org_field is the column name that identifies which org a row
    belongs to (default "org_id")."""

    org_field: str = "org_id"

    def _apply_org_scope(self, stmt, request: Request):
        if request.session.get("is_superadmin", False):
            return stmt
        column = getattr(self.model, self.org_field)
        return stmt.where(column == request.session.get("org_id"))

    def list_query(self, request: Request):
        return self._apply_org_scope(super().list_query(request), request)

    def count_query(self, request: Request):
        return self._apply_org_scope(super().count_query(request), request)

    def form_edit_query(self, request: Request):
        # sqladmin fetches the edit-page object by pk alone (not via
        # list_query) - without this, an org admin who guesses another
        # org's row id could still reach its edit page.
        return self._apply_org_scope(super().form_edit_query(request), request)

    def details_query(self, request: Request):
        return self._apply_org_scope(super().details_query(request), request)

    def _check_row_scope(self, request: Request, pk: str) -> None:
        """Real boundary, not just the query-scoping above: sqladmin's
        own update()/delete() re-fetch the row by raw pk, bypassing
        form_edit_query/list_query/count_query entirely - so a crafted
        POST/DELETE naming another org's row id must be caught here."""
        if request.session.get("is_superadmin", False):
            return
        with Session(engine) as session:
            existing = session.get(self.model, int(pk))
        if existing is None or getattr(existing, self.org_field) != request.session.get("org_id"):
            raise HTTPException(status_code=404, detail="Not found")


class ScopedModelView(ModelView):
    unit_field: str = "unit_id"

    def _scope(self, request: Request) -> tuple[bool, list[int]]:
        return (
            request.session.get("is_superadmin", False),
            resolve_unit_ids(request.session),
        )

    def _apply_scope(self, stmt, request: Request):
        is_superadmin, unit_ids = self._scope(request)
        if is_superadmin:
            return stmt
        column = getattr(self.model, self.unit_field)
        return stmt.where(column.in_(unit_ids))

    def _row_in_scope(self, request: Request, pk: str) -> bool:
        """True if `pk` names a row the current user is allowed to reach.
        Needed because sqladmin fetches the edit/details page object, and
        re-fetches the row inside update_model/delete_model, by raw pk
        alone (ModelView.form_edit_query/form_details_query,
        Query.update()/Query.delete() in sqladmin/_queries.py) - none of
        that goes through list_query/count_query, so unit_field scoping
        applied only there (as above) is cosmetic, not a boundary. See
        form_edit_query/details_query/update_model/delete_model below,
        the actual call sites this guards."""
        is_superadmin, unit_ids = self._scope(request)
        if is_superadmin:
            return True
        with Session(engine) as session:
            obj = session.get(self.model, int(pk))
        if obj is None:
            return False
        return getattr(obj, self.unit_field) in unit_ids

    def list_query(self, request: Request):
        return self._apply_scope(super().list_query(request), request)

    def count_query(self, request: Request):
        return self._apply_scope(super().count_query(request), request)

    def form_edit_query(self, request: Request):
        # sqladmin fetches the edit-page object by pk alone (not via
        # list_query) - without this, a scoped staff member who guesses
        # another unit's row id could still reach its edit page.
        return self._apply_scope(super().form_edit_query(request), request)

    def details_query(self, request: Request):
        return self._apply_scope(super().details_query(request), request)

    async def update_model(self, request: Request, pk: str, data: dict) -> Any:
        # Real boundary, not just the query-scoping above: sqladmin's own
        # Query.update() re-fetches the row by pk alone, bypassing
        # form_edit_query/list_query entirely - so a crafted POST to
        # another unit's row id must be caught here explicitly.
        if not self._row_in_scope(request, pk):
            raise HTTPException(status_code=404, detail="Not found")
        return await super().update_model(request, pk, data)

    async def delete_model(self, request: Request, pk: Any) -> None:
        # Same reasoning as update_model above - Query.delete() also
        # re-fetches by raw pk, bypassing list_query/count_query.
        if not self._row_in_scope(request, pk):
            raise HTTPException(status_code=404, detail="Not found")
        await super().delete_model(request, pk)

    async def scaffold_form(self, rules: list[str] | None = None):
        """Filters the Organisation/Unit picker on create/edit forms down
        to the current user's scope, using the current_scope contextvar
        (admin_auth.py) since this method gets no `request` param.
        Non-superadmins never see another org's units, or another
        organisation at all, in a dropdown - closes the gap this class
        used to flag as a known, not-yet-fixed issue. This is UX/defense
        in depth, not the actual security boundary: insert_model/
        update_model on UnitAdmin/UserAdmin still force the real
        org_id server-side regardless of what a form submits."""
        from sqlalchemy.orm import Session

        from autosend.admin_auth import current_scope
        from autosend.admin_models import Organisation, Unit, engine

        form_cls = await super().scaffold_form(rules)

        scope = current_scope.get()
        if scope is None:
            return form_cls
        is_superadmin, _is_org_admin, org_id, _unit_ids = scope
        if is_superadmin:
            return form_cls

        unit_ids = resolve_unit_ids(
            {"is_org_admin": _is_org_admin, "org_id": org_id, "unit_ids": _unit_ids}
        )

        # sqladmin's own QuerySelectField (sqladmin/fields.py) takes a
        # pre-materialized `data` kwarg - a list of (str(pk), str(obj))
        # pairs - not a wtforms-sqlalchemy-style `query_factory` callable
        # (that field class doesn't accept one at all). This mirrors
        # exactly what sqladmin's own _prepare_select_options() builds
        # normally, just pre-filtered to the current scope.
        if hasattr(form_cls, "organisation") and org_id is not None:
            with Session(engine) as session:
                orgs = session.query(Organisation).filter(Organisation.id == org_id).all()
                form_cls.organisation.kwargs["data"] = [(str(o.id), str(o)) for o in orgs]

        if hasattr(form_cls, "unit") and unit_ids:
            with Session(engine) as session:
                units = session.query(Unit).filter(Unit.id.in_(unit_ids)).all()
                form_cls.unit.kwargs["data"] = [(str(u.id), str(u)) for u in units]

        return form_cls

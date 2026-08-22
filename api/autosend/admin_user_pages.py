"""Bespoke Users pages - /users (list) and /users/{user_id} (detail/edit),
the friendlier counterpart to UserAdmin's generic CRUD screen (still
registered and fully functional at /users/list, /users/create, etc. -
same "new bespoke page as the primary surface, old CRUD kept as an
escape hatch" split OrganisationsView/UnitsView already established).

Every real permission boundary (org scoping, last-admin protection, unit
assignment restricted to the target org, seat-limit entitlement checks,
password strength) is re-run here exactly as UserAdmin enforces it - the
bespoke page is a different surface over the same rules, not a relaxed
one.
"""
from datetime import datetime, timezone

import bcrypt
from fastapi import HTTPException
from sqladmin import BaseView, expose
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload
from starlette.requests import Request
from starlette.responses import RedirectResponse

from autosend.admin_models import engine, Organisation, Unit, User
from autosend.billing import entitlements
from autosend.password_policy import validate_password_strength
from autosend.utils.logging import get_logger

logger = get_logger(__name__)


class UsersView(BaseView):
    name = "Users"
    icon = "fa-solid fa-user-shield"
    identity = "users-page"

    def is_accessible(self, request: Request) -> bool:
        return request.session.get("is_superadmin", False) or request.session.get("is_org_admin", False)

    def is_visible(self, request: Request) -> bool:
        return self.is_accessible(request)

    def _scope(self, request: Request) -> tuple[bool, int | None]:
        is_superadmin = request.session.get("is_superadmin", False)
        org_id = None if is_superadmin else request.session.get("org_id")
        return is_superadmin, org_id

    @expose("/users", methods=["GET"], identity="users-list-page")
    async def list_page(self, request: Request):
        if not self.is_accessible(request):
            raise HTTPException(status_code=403, detail="Not permitted")
        from autosend.web.auth import get_current_web_user

        is_superadmin, org_id = self._scope(request)
        with Session(engine) as session:
            query = select(User).order_by(User.username)
            if org_id is not None:
                query = query.where(User.org_id == org_id)
            users = session.execute(query).scalars().all()
            rows = [
                {
                    "u": u,
                    "org_name": u.organisation.name if u.organisation else "",
                    "org_link": _org_link(u.organisation, is_superadmin) if u.organisation else None,
                    "unit_names": ", ".join(sorted(unit.name for unit in u.units)),
                }
                for u in users
            ]
        return await self.templates.TemplateResponse(
            request, "users_list.html",
            {"user": get_current_web_user(request), "rows": rows, "is_superadmin": is_superadmin},
        )

    def _org_units_for_form(self, is_superadmin: bool, org_id: int | None):
        """Returns (units, units_org_map). units_org_map is only populated
        for a superadmin (every unit id -> its org id, for the create/edit
        template's JS to cascade-filter the checkbox list by whichever
        org is selected) - non-superadmins get their own org's units
        pre-filtered server-side instead, same split as
        UserAdmin.scaffold_form/create_context/edit_context."""
        with Session(engine) as session:
            if is_superadmin:
                # joinedload: the template reads u.organisation.name for
                # every row, and the session is closed by the time it
                # renders (Jinja doesn't run inside this `with` block) -
                # without eager loading that's a DetachedInstanceError,
                # not a lazy load, the moment the template touches it.
                units = session.execute(
                    select(Unit).options(joinedload(Unit.organisation)).order_by(Unit.name)
                ).scalars().all()
                units_org_map = {str(u.id): str(u.org_id) for u in units}
            else:
                units = (
                    session.execute(select(Unit).where(Unit.org_id == org_id).order_by(Unit.name)).scalars().all()
                    if org_id else []
                )
                units_org_map = None
            session.expunge_all()
        return units, units_org_map

    @expose("/users/new", methods=["GET"], identity="user-new-page")
    async def new_page(self, request: Request):
        if not self.is_accessible(request):
            raise HTTPException(status_code=403, detail="Not permitted")
        from autosend.web.auth import get_current_web_user
        from autosend import storage

        is_superadmin, org_id = self._scope(request)
        orgs = storage.list_organisations(active_only=False) if is_superadmin else []
        units, units_org_map = self._org_units_for_form(is_superadmin, org_id)
        return await self.templates.TemplateResponse(
            request, "user_new.html",
            {
                "user": get_current_web_user(request), "orgs": orgs, "units": units,
                "units_org_map": units_org_map, "is_superadmin": is_superadmin, "error": None,
            },
        )

    @expose("/users", methods=["POST"], identity="user-create")
    async def create(self, request: Request):
        if not self.is_accessible(request):
            raise HTTPException(status_code=403, detail="Not permitted")
        from autosend.web.auth import get_current_web_user
        from autosend import storage

        is_superadmin, session_org_id = self._scope(request)
        form = await request.form()
        username = (form.get("username") or "").strip()
        password = form.get("password") or ""
        is_org_admin = "is_org_admin" in form
        is_target_superadmin = is_superadmin and "is_superadmin" in form
        unit_ids = {int(v) for v in form.getlist("units") if v}

        if is_superadmin:
            org_id_raw = form.get("org_id")
            org_id = int(org_id_raw) if org_id_raw else None
        else:
            org_id = session_org_id

        def _redisplay(error: str):
            orgs = storage.list_organisations(active_only=False) if is_superadmin else []
            units, units_org_map = self._org_units_for_form(is_superadmin, org_id)
            return self.templates.TemplateResponse(
                request, "user_new.html",
                {
                    "user": get_current_web_user(request), "orgs": orgs, "units": units,
                    "units_org_map": units_org_map, "is_superadmin": is_superadmin, "error": error,
                },
                status_code=400,
            )

        if not username:
            return await _redisplay("Username is required.")
        if not password:
            return await _redisplay("Password is required.")
        try:
            validate_password_strength(password)
        except ValueError as exc:
            return await _redisplay(str(exc))
        if not is_target_superadmin and not org_id:
            return await _redisplay("Organisation is required.")
        if not is_target_superadmin:
            try:
                entitlements.check_can_add_user(org_id)
            except entitlements.LimitExceeded as exc:
                return await _redisplay(str(exc))

        # Never trust which units were posted beyond the target org's own
        # set - same boundary as UserAdmin._restrict_units_to_org, not
        # just the checkbox list's own filtering.
        allowed_unit_ids = set(storage.get_unit_ids_for_org(org_id)) if org_id else set()
        unit_ids &= allowed_unit_ids

        with Session(engine) as session:
            if org_id is not None and session.get(Organisation, org_id) is None:
                raise HTTPException(status_code=404, detail="Organisation not found")
            user = User(
                org_id=org_id,
                username=username,
                password_hash=bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode(),
                is_superadmin=is_target_superadmin,
                is_org_admin=is_org_admin,
                active=True,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            if unit_ids:
                user.units = session.execute(select(Unit).where(Unit.id.in_(unit_ids))).scalars().all()
            session.add(user)
            session.commit()
            session.refresh(user)
            user_id = user.id

        return RedirectResponse(url=f"/users/{user_id}", status_code=303)

    async def _detail_context(self, request: Request, user_id: int) -> dict:
        from autosend.web.auth import get_current_web_user

        is_superadmin, session_org_id = self._scope(request)
        with Session(engine) as session:
            target = session.get(User, user_id)
            if target is None:
                raise HTTPException(status_code=404, detail="Not found")
            if not is_superadmin and target.org_id != session_org_id:
                raise HTTPException(status_code=404, detail="Not found")

            org = target.organisation
            assigned_unit_ids = {u.id for u in target.units}
            # Unlike the create page, there's no org picker here to
            # cascade against - a user's org is fixed once created (this
            # page doesn't offer moving a user to a different org), so
            # the checkbox list is just that target's own org's units,
            # for either role.
            units = (
                session.execute(select(Unit).where(Unit.org_id == target.org_id).order_by(Unit.name)).scalars().all()
                if target.org_id else []
            )

        return {
            "user": get_current_web_user(request),
            "target": target,
            "org": org,
            "org_link": _org_link(org, is_superadmin) if org else None,
            "units": units,
            "assigned_unit_ids": assigned_unit_ids,
            "is_superadmin": is_superadmin,
        }

    @expose("/users/{user_id:int}", methods=["GET"], identity="user-detail-page")
    async def detail_page(self, request: Request):
        if not self.is_accessible(request):
            raise HTTPException(status_code=403, detail="Not permitted")
        user_id = request.path_params["user_id"]
        context = await self._detail_context(request, user_id)
        return await self.templates.TemplateResponse(request, "user_detail.html", context)

    @expose("/users/{user_id:int}/update", methods=["POST"], identity="user-update")
    async def update_identity(self, request: Request):
        if not self.is_accessible(request):
            raise HTTPException(status_code=403, detail="Not permitted")
        from autosend import storage

        is_superadmin, session_org_id = self._scope(request)
        user_id = request.path_params["user_id"]
        form = await request.form()

        with Session(engine) as session:
            target = session.get(User, user_id)
            if target is None:
                raise HTTPException(status_code=404, detail="Not found")
            if not is_superadmin and target.org_id != session_org_id:
                raise HTTPException(status_code=404, detail="Not found")

            new_is_org_admin = "is_org_admin" in form
            if (
                target.org_id is not None
                and target.is_org_admin
                and not new_is_org_admin
                and storage.count_active_org_admins(target.org_id) <= 1
            ):
                raise HTTPException(
                    status_code=400,
                    detail="Cannot remove the last admin for this organisation. Promote another user to org admin first.",
                )

            target.active = "active" in form
            target.is_org_admin = new_is_org_admin
            if is_superadmin:
                target.is_superadmin = "is_superadmin" in form

            unit_ids = {int(v) for v in form.getlist("units") if v}
            allowed_unit_ids = set(storage.get_unit_ids_for_org(target.org_id)) if target.org_id else set()
            unit_ids &= allowed_unit_ids
            target.units = session.execute(select(Unit).where(Unit.id.in_(unit_ids))).scalars().all() if unit_ids else []

            session.commit()

        return RedirectResponse(url=f"/users/{user_id}", status_code=303)

    @expose("/users/{user_id:int}/password", methods=["POST"], identity="user-password-reset")
    async def reset_password(self, request: Request):
        if not self.is_accessible(request):
            raise HTTPException(status_code=403, detail="Not permitted")
        is_superadmin, session_org_id = self._scope(request)
        user_id = request.path_params["user_id"]
        form = await request.form()
        password = form.get("password") or ""

        with Session(engine) as session:
            target = session.get(User, user_id)
            if target is None:
                raise HTTPException(status_code=404, detail="Not found")
            if not is_superadmin and target.org_id != session_org_id:
                raise HTTPException(status_code=404, detail="Not found")

            try:
                validate_password_strength(password)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))

            target.password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
            session.commit()

        return RedirectResponse(url=f"/users/{user_id}", status_code=303)


def _org_link(org, is_superadmin: bool) -> str:
    if is_superadmin:
        return f"/organisations/{org.id}"
    return "/organisation"

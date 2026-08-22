"""Bespoke Units pages - /units (list) and /units/{unit_id} (detail/edit),
the friendlier counterpart to UnitAdmin's generic CRUD screen (still
registered and fully functional at /unit/*, same "new bespoke page as
the primary surface, old CRUD kept as an escape hatch" split
OrganisationsView already established for OrganisationAdmin - see
admin_org_pages.py's own module docstring). Webhook secret management
stays entirely on /pco-settings (PcoSettingsView) - this page only
surfaces the PCO Campus picker and a link out to PCO Settings, not the
webhook config itself.
"""
from datetime import datetime, timezone

from fastapi import HTTPException
from sqladmin import BaseView, expose
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import RedirectResponse

from autosend.admin_models import engine, Organisation, Unit
from autosend.admin_views import _slugify
from autosend.utils.logging import get_logger

logger = get_logger(__name__)


def _org_link(org, is_superadmin: bool) -> str:
    """Same role-based org link split as admin_views._organisation_link -
    superadmins get the per-org page, org admins get their own-org page
    (which needs no org_id - they can only ever land here via their own
    org's units anyway)."""
    if is_superadmin:
        return f"/organisations/{org.id}"
    return "/organisation"


class UnitsView(BaseView):
    name = "Units"
    icon = "fa-solid fa-people-group"
    identity = "units-page"

    def is_accessible(self, request: Request) -> bool:
        return request.session.get("is_superadmin", False) or request.session.get("is_org_admin", False)

    def is_visible(self, request: Request) -> bool:
        return self.is_accessible(request)

    def _scope(self, request: Request) -> tuple[bool, int | None]:
        is_superadmin = request.session.get("is_superadmin", False)
        org_id = None if is_superadmin else request.session.get("org_id")
        return is_superadmin, org_id

    @expose("/units", methods=["GET"], identity="units-list-page")
    async def list_page(self, request: Request):
        if not self.is_accessible(request):
            raise HTTPException(status_code=403, detail="Not permitted")
        from autosend.web.auth import get_current_web_user

        is_superadmin, org_id = self._scope(request)
        with Session(engine) as session:
            query = select(Unit).order_by(Unit.name)
            if org_id is not None:
                query = query.where(Unit.org_id == org_id)
            units = session.execute(query).scalars().all()
            rows = [
                {
                    "unit": u,
                    "org_name": u.organisation.name if u.organisation else "",
                    "org_link": _org_link(u.organisation, is_superadmin) if u.organisation else None,
                    "number_count": len(u.whatsapp_numbers),
                    "number_labels": [n.label for n in u.whatsapp_numbers],
                }
                for u in units
            ]
        return await self.templates.TemplateResponse(
            request, "units_list.html",
            {"user": get_current_web_user(request), "rows": rows, "is_superadmin": is_superadmin},
        )

    @expose("/units/new", methods=["GET"], identity="unit-new-page")
    async def new_page(self, request: Request):
        if not self.is_accessible(request):
            raise HTTPException(status_code=403, detail="Not permitted")
        from autosend.web.auth import get_current_web_user
        from autosend import storage

        is_superadmin, _org_id = self._scope(request)
        orgs = storage.list_organisations(active_only=False) if is_superadmin else []
        return await self.templates.TemplateResponse(
            request, "unit_new.html",
            {"user": get_current_web_user(request), "orgs": orgs, "is_superadmin": is_superadmin, "error": None},
        )

    @expose("/units", methods=["POST"], identity="unit-create")
    async def create(self, request: Request):
        if not self.is_accessible(request):
            raise HTTPException(status_code=403, detail="Not permitted")
        from autosend.web.auth import get_current_web_user
        from autosend import storage

        is_superadmin, session_org_id = self._scope(request)
        form = await request.form()
        name = (form.get("name") or "").strip()

        # org_id from the form is only ever honoured for a superadmin -
        # an org admin's own org_id always comes from their session,
        # never trusted from the form, same rule as every other
        # org-scoped write in this codebase.
        if is_superadmin:
            org_id_raw = form.get("org_id")
            org_id = int(org_id_raw) if org_id_raw else None
        else:
            org_id = session_org_id

        if not name or not org_id:
            orgs = storage.list_organisations(active_only=False) if is_superadmin else []
            return await self.templates.TemplateResponse(
                request, "unit_new.html",
                {
                    "user": get_current_web_user(request), "orgs": orgs, "is_superadmin": is_superadmin,
                    "error": "Name and organisation are required.",
                },
                status_code=400,
            )

        with Session(engine) as session:
            if session.get(Organisation, org_id) is None:
                raise HTTPException(status_code=404, detail="Organisation not found")
            unit = Unit(
                org_id=org_id, name=name, slug=_slugify(name), active=True,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            session.add(unit)
            session.commit()
            session.refresh(unit)
            unit_id = unit.id

        return RedirectResponse(url=f"/units/{unit_id}", status_code=303)

    async def _detail_context(self, request: Request, unit_id: int) -> dict:
        from autosend.web.auth import get_current_web_user
        from autosend import storage

        is_superadmin, session_org_id = self._scope(request)
        with Session(engine) as session:
            unit = session.get(Unit, unit_id)
            if unit is None:
                raise HTTPException(status_code=404, detail="Not found")
            if not is_superadmin and unit.org_id != session_org_id:
                raise HTTPException(status_code=404, detail="Not found")

            org = unit.organisation
            numbers = list(unit.whatsapp_numbers)
            automation_count = len(unit.templates)
            org_id = unit.org_id

        pco_enabled = storage.is_enabled(org_id, storage.MODULE_PCO)
        campuses = []
        if pco_enabled:
            try:
                from autosend.clients import get_pco_org_client

                campuses = await get_pco_org_client(org_id).get_campuses()
            except Exception:
                logger.exception("Failed to fetch PCO campuses for org %s", org_id)
                campuses = []

        return {
            "user": get_current_web_user(request),
            "unit": unit,
            "org": org,
            "org_link": _org_link(org, is_superadmin) if org else None,
            "numbers": numbers,
            "automation_count": automation_count,
            "pco_enabled": pco_enabled,
            "campuses": campuses,
            "is_superadmin": is_superadmin,
        }

    @expose("/units/{unit_id:int}", methods=["GET"], identity="unit-detail-page")
    async def detail_page(self, request: Request):
        if not self.is_accessible(request):
            raise HTTPException(status_code=403, detail="Not permitted")
        unit_id = request.path_params["unit_id"]
        context = await self._detail_context(request, unit_id)
        return await self.templates.TemplateResponse(request, "unit_detail.html", context)

    @expose("/units/{unit_id:int}/update", methods=["POST"], identity="unit-update")
    async def update_identity(self, request: Request):
        if not self.is_accessible(request):
            raise HTTPException(status_code=403, detail="Not permitted")
        is_superadmin, session_org_id = self._scope(request)
        unit_id = request.path_params["unit_id"]
        form = await request.form()
        name = (form.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="Name is required")

        with Session(engine) as session:
            unit = session.get(Unit, unit_id)
            if unit is None:
                raise HTTPException(status_code=404, detail="Not found")
            if not is_superadmin and unit.org_id != session_org_id:
                raise HTTPException(status_code=404, detail="Not found")
            # slug re-derived on every edit (not just creation) so a later
            # name fix/typo correction can't leave a stale slug behind -
            # same reasoning as UnitAdmin.update_model.
            unit.name = name
            unit.slug = _slugify(name)
            # Active toggle is allowed for both roles here, same
            # permission UnitAdmin already grants an org admin over their
            # own unit (unlike an org's own active flag, which an org
            # admin can't touch on the Organisation identity card).
            unit.active = "active" in form
            session.commit()

        return RedirectResponse(url=f"/units/{unit_id}", status_code=303)

    @expose("/units/{unit_id:int}/campus", methods=["POST"], identity="unit-campus-update")
    async def update_campus(self, request: Request):
        if not self.is_accessible(request):
            raise HTTPException(status_code=403, detail="Not permitted")
        is_superadmin, session_org_id = self._scope(request)
        unit_id = request.path_params["unit_id"]
        form = await request.form()
        campus_id = (form.get("pco_campus_id") or "").strip() or None

        with Session(engine) as session:
            unit = session.get(Unit, unit_id)
            if unit is None:
                raise HTTPException(status_code=404, detail="Not found")
            if not is_superadmin and unit.org_id != session_org_id:
                raise HTTPException(status_code=404, detail="Not found")
            unit.pco_campus_id = campus_id
            session.commit()

        return RedirectResponse(url=f"/units/{unit_id}", status_code=303)

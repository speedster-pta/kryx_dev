"""Bespoke WhatsApp Numbers pages - /whatsapp-numbers (list) and
/whatsapp-numbers/{number_id} (detail/edit), the friendlier counterpart
to WhatsAppNumberAdmin's generic CRUD screen (still registered and fully
functional at /whatsapp-numbers/list, /whatsapp-numbers/create, etc. -
same "new bespoke page as the primary surface, old CRUD kept as an
escape hatch" split OrganisationsView/UnitsView/UsersView already
established).

No bespoke "create" page here - Embedded Signup (/add-number,
onboarding_router.py) is already the primary way a number gets connected
(see sqladmin/list.html's own "+ New" override for whatsapp-numbers),
with WhatsAppNumberAdmin's manual create form as the documented fallback
from that page. This page's "+ New Number" button points at /add-number
too, for the same reason.

Unlike Units/Users, plain unit-scoped staff CAN reach this page (not
just superadmin/org admin) - WhatsAppNumberAdmin has no is_accessible
override for the same reason (a unit-scoped WhatsApp number is exactly
what unit-scoped staff manage day to day), so scoping here goes through
web.auth.resolve_unit_ids() rather than the org-level scoping
Units/Users use.
"""
from datetime import datetime, timezone

from fastapi import HTTPException
from sqladmin import BaseView, expose
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload
from starlette.requests import Request
from starlette.responses import RedirectResponse

from autosend.admin_models import engine, Unit, WhatsAppNumber
from autosend.admin_views import COUNTRY_REGION_CHOICES
from autosend.utils.logging import get_logger
from autosend.web.auth import resolve_unit_ids

logger = get_logger(__name__)


def _org_link(org, is_superadmin: bool) -> str:
    if is_superadmin:
        return f"/organisations/{org.id}"
    return "/organisation"


class WhatsAppNumbersView(BaseView):
    name = "WhatsApp Numbers"
    icon = "fa-solid fa-phone"
    identity = "whatsapp-numbers-page"

    # No is_accessible/is_visible override, matching WhatsAppNumberAdmin -
    # plain unit-scoped staff can reach this page for their own unit(s),
    # not just superadmin/org admin. Every route below still re-checks
    # scope itself since a BaseView's @expose routes aren't auto-guarded.

    def _scoped_unit_ids(self, request: Request) -> list[int] | None:
        """None means "no filter" (superadmin) - otherwise the list of
        unit ids this session may see, same resolve_unit_ids() choke
        point ScopedModelView itself is built on."""
        if request.session.get("is_superadmin", False):
            return None
        return resolve_unit_ids(request.session)

    @expose("/whatsapp-numbers", methods=["GET"], identity="whatsapp-numbers-list-page")
    async def list_page(self, request: Request):
        from autosend.web.auth import get_current_web_user

        is_superadmin = request.session.get("is_superadmin", False)
        unit_ids = self._scoped_unit_ids(request)
        with Session(engine) as session:
            query = select(WhatsAppNumber).options(
                joinedload(WhatsAppNumber.unit).joinedload(Unit.organisation)
            ).order_by(WhatsAppNumber.label)
            if unit_ids is not None:
                # in_([]) correctly matches nothing (a plain-staff session
                # with no unit assignment at all) rather than needing a
                # separate always-false branch.
                query = query.where(WhatsAppNumber.unit_id.in_(unit_ids))
            numbers = session.execute(query).unique().scalars().all()
            rows = [
                {
                    "n": n,
                    "unit_name": n.unit.name if n.unit else "",
                    "org_name": n.unit.organisation.name if n.unit and n.unit.organisation else "",
                    "org_link": (
                        _org_link(n.unit.organisation, is_superadmin)
                        if n.unit and n.unit.organisation else None
                    ),
                }
                for n in numbers
            ]
        return await self.templates.TemplateResponse(
            request, "whatsapp_numbers_list.html",
            {"user": get_current_web_user(request), "rows": rows, "is_superadmin": is_superadmin},
        )

    async def _detail_context(self, request: Request, number_id: int) -> dict:
        from autosend.web.auth import get_current_web_user

        is_superadmin = request.session.get("is_superadmin", False)
        unit_ids = self._scoped_unit_ids(request)
        with Session(engine) as session:
            number = session.execute(
                select(WhatsAppNumber)
                .options(joinedload(WhatsAppNumber.unit).joinedload(Unit.organisation))
                .where(WhatsAppNumber.id == number_id)
            ).unique().scalar_one_or_none()
            if number is None:
                raise HTTPException(status_code=404, detail="Not found")
            if unit_ids is not None and number.unit_id not in unit_ids:
                raise HTTPException(status_code=404, detail="Not found")
            unit = number.unit
            org = unit.organisation if unit else None

        return {
            "user": get_current_web_user(request),
            "number": number,
            "unit": unit,
            "org": org,
            "org_link": _org_link(org, is_superadmin) if org else None,
            "region_choices": COUNTRY_REGION_CHOICES,
            "is_superadmin": is_superadmin,
        }

    @expose("/whatsapp-numbers/{number_id:int}", methods=["GET"], identity="whatsapp-number-detail-page")
    async def detail_page(self, request: Request):
        number_id = request.path_params["number_id"]
        context = await self._detail_context(request, number_id)
        return await self.templates.TemplateResponse(request, "whatsapp_number_detail.html", context)

    def _number_in_scope_or_404(self, session: Session, request: Request, number_id: int) -> WhatsAppNumber:
        number = session.get(WhatsAppNumber, number_id)
        if number is None:
            raise HTTPException(status_code=404, detail="Not found")
        unit_ids = self._scoped_unit_ids(request)
        if unit_ids is not None and number.unit_id not in unit_ids:
            raise HTTPException(status_code=404, detail="Not found")
        return number

    @expose("/whatsapp-numbers/{number_id:int}/update", methods=["POST"], identity="whatsapp-number-update")
    async def update_config(self, request: Request):
        number_id = request.path_params["number_id"]
        form = await request.form()
        label = (form.get("label") or "").strip()
        if not label:
            raise HTTPException(status_code=400, detail="Label is required")

        try:
            send_delay_seconds = float(form.get("send_delay_seconds") or 0)
            send_concurrency = int(form.get("send_concurrency") or 20)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid delay/concurrency value")
        if not (0 <= send_delay_seconds <= 10):
            raise HTTPException(status_code=400, detail="Delay must be between 0 and 10 seconds")
        if not (1 <= send_concurrency <= 40):
            raise HTTPException(status_code=400, detail="Concurrency must be between 1 and 40")

        reserve_raw = (form.get("campaign_reserve_percent") or "").strip()
        campaign_reserve_percent = None
        if reserve_raw:
            try:
                campaign_reserve_percent = int(reserve_raw)
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid reserve percent")
            if not (0 <= campaign_reserve_percent <= 100):
                raise HTTPException(status_code=400, detail="Reserve percent must be between 0 and 100")

        with Session(engine) as session:
            number = self._number_in_scope_or_404(session, request, number_id)
            number.label = label
            number.active = "active" in form
            number.send_delay_seconds = send_delay_seconds
            number.send_concurrency = send_concurrency
            number.campaign_reserve_percent = campaign_reserve_percent
            number.default_region = (form.get("default_region") or number.default_region).strip()
            session.commit()

        return RedirectResponse(url=f"/whatsapp-numbers/{number_id}", status_code=303)

    @expose("/whatsapp-numbers/{number_id:int}/credentials", methods=["POST"], identity="whatsapp-number-credentials-update")
    async def update_credentials(self, request: Request):
        """Separate from update_config above, same "credential edit is its
        own action" split as PCO's webhook secret form - also re-syncs
        display_phone_number from Meta whenever the access token or
        phone_number_id actually changes, same as
        WhatsAppNumberAdmin.update_model."""
        from autosend.whatsapp_limits import sync_display_number_from_meta

        number_id = request.path_params["number_id"]
        form = await request.form()
        phone_number_id = (form.get("phone_number_id") or "").strip()
        access_token = form.get("access_token") or ""
        waba_id = (form.get("waba_id") or "").strip() or None
        meta_app_id = (form.get("meta_app_id") or "").strip() or None

        with Session(engine) as session:
            number = self._number_in_scope_or_404(session, request, number_id)
            if phone_number_id:
                number.phone_number_id = phone_number_id
            if access_token:
                number.access_token = access_token
            number.waba_id = waba_id
            number.meta_app_id = meta_app_id

            effective_token = access_token or number.access_token
            effective_phone_id = phone_number_id or number.phone_number_id
            if effective_token and effective_phone_id:
                display_number = sync_display_number_from_meta(effective_token, effective_phone_id)
                if display_number:
                    number.display_phone_number = display_number

            session.commit()

        return RedirectResponse(url=f"/whatsapp-numbers/{number_id}", status_code=303)

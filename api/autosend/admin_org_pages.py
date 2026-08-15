"""Consolidated organisation-facing admin pages.

Two pages live here:

  OrganisationsView - superadmin's org list (/organisations) and per-org
  detail page (/organisations/{org_id}, plus /organisation for an
  org-admin's own org) - organisation identity + the module
  grant/enable checkboxes that used to be their own standalone
  /modules page (admin_pages.ModulesView). That page's POST routes
  (/modules/toggle, /modules/grant-toggle) are reused as-is, just
  redirected back here - see _safe_redirect_target in admin_pages.py.

  PcoSettingsView - the org-level PCO Personal Access Token
  (previously only reachable via the generic PCOOrganizationSettingsAdmin
  CRUD screen) and every one of the org's units' PCO webhook config
  (previously the separate UnitWebhookAdmin page), together on one page.
  Both of those ModelViews stay registered and fully functional (see
  admin.py) - existing links/tests/plain-staff access to
  /pco-webhook/* are untouched; this is an additional, friendlier
  surface for whoever is configuring PCO for a whole org (superadmin or
  that org's own org-admin).
"""
from datetime import datetime, timezone

from fastapi import HTTPException
from sqladmin import BaseView, expose
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import RedirectResponse

from autosend.admin_models import engine, PCOOrganizationSettings, Unit


def _module_rows_for_org(org_id: int) -> list[dict]:
    from autosend import storage

    granted = set(storage.granted_modules_for_org(org_id))
    enabled = set(storage.enabled_modules_for_org(org_id))
    return [
        {"key": key, "label": label, "granted": key in granted, "enabled": key in enabled}
        for key, label in storage.AVAILABLE_MODULES
    ]


class OrganisationsView(BaseView):
    name = "Organisations"
    icon = "fa-solid fa-building"
    identity = "organisations-page"

    def is_accessible(self, request: Request) -> bool:
        return request.session.get("is_superadmin", False)

    def is_visible(self, request: Request) -> bool:
        return self.is_accessible(request)

    @expose("/organisations", methods=["GET"], identity="organisations-list-page")
    async def list_page(self, request: Request):
        from autosend.web.auth import get_current_web_user
        from autosend import storage

        if not request.session.get("is_superadmin", False):
            raise HTTPException(status_code=403, detail="Superadmin only")

        user = get_current_web_user(request)
        orgs = storage.list_organisations(active_only=False)
        rows = [
            {"org": org, "enabled_count": len(storage.enabled_modules_for_org(org.id))}
            for org in orgs
        ]
        return await self.templates.TemplateResponse(
            request, "organisations_list.html", {"user": user, "rows": rows},
        )

    @expose("/organisations/new", methods=["GET"], identity="organisation-new-page")
    async def new_page(self, request: Request):
        if not request.session.get("is_superadmin", False):
            raise HTTPException(status_code=403, detail="Superadmin only")
        from autosend.web.auth import get_current_web_user
        from autosend import storage

        return await self.templates.TemplateResponse(
            request,
            "organisation_new.html",
            {
                "user": get_current_web_user(request),
                "modules": storage.AVAILABLE_MODULES,
                "error": None,
            },
        )

    @expose("/organisations", methods=["POST"], identity="organisation-create")
    async def create(self, request: Request):
        if not request.session.get("is_superadmin", False):
            raise HTTPException(status_code=403, detail="Superadmin only")
        from autosend.web.auth import get_current_web_user
        from autosend import storage

        form = await request.form()
        name = (form.get("name") or "").strip()
        if not name:
            return await self.templates.TemplateResponse(
                request,
                "organisation_new.html",
                {
                    "user": get_current_web_user(request),
                    "modules": storage.AVAILABLE_MODULES,
                    "error": "Organisation name is required.",
                },
                status_code=400,
            )
        # create_organisation() provisions the default "Main" unit in the
        # same transaction - every organisation must have at least one
        # unit (see UnitAdmin.delete_model's matching last-unit guard), so
        # this can't be left as a bare organisations-table insert the way
        # OrganisationAdmin's old generic create form used to.
        org = storage.create_organisation(name, storage.generate_unique_slug(name))
        # Provisioning a module at creation time means both granting it
        # (the superadmin-only entitlement) and enabling it (the org-facing
        # toggle) in one step - enable() alone would raise since a
        # brand-new org has no grants yet, and grant-without-enable would
        # leave the checkbox looking like a no-op.
        for module_key, _label in storage.AVAILABLE_MODULES:
            if form.get(f"module_{module_key}"):
                storage.grant(org.id, module_key)
                storage.enable(org.id, module_key)
        return RedirectResponse(url=f"/organisations/{org.id}", status_code=303)

    async def _detail_context(self, request: Request, org_id: int, editable_identity: bool) -> dict:
        from autosend.web.auth import get_current_web_user
        from autosend import storage

        org = storage.get_organisation(org_id)
        if org is None:
            raise HTTPException(status_code=404, detail="Not found")

        return {
            "user": get_current_web_user(request),
            "org": org,
            "modules": _module_rows_for_org(org_id),
            "editable_identity": editable_identity,
            "is_superadmin": request.session.get("is_superadmin", False),
            "is_org_admin": request.session.get("is_org_admin", False),
            "back_url": "/organisations" if editable_identity else None,
        }

    @expose("/organisations/{org_id:int}", methods=["GET"], identity="organisation-detail-page")
    async def detail_page(self, request: Request):
        if not request.session.get("is_superadmin", False):
            raise HTTPException(status_code=403, detail="Superadmin only")
        org_id = request.path_params["org_id"]
        context = await self._detail_context(request, org_id, editable_identity=True)
        return await self.templates.TemplateResponse(request, "organisation_detail.html", context)

    @expose("/organisation", methods=["GET"], identity="own-organisation-page")
    async def own_page(self, request: Request):
        is_superadmin = request.session.get("is_superadmin", False)
        if not is_superadmin and not request.session.get("is_org_admin", False):
            raise HTTPException(status_code=403, detail="Not permitted")
        org_id = request.session.get("org_id")
        if org_id is None:
            # Superadmins have no org of their own - send them to the list
            # they actually manage instead of a dead 404.
            return RedirectResponse(url="/organisations", status_code=303)
        context = await self._detail_context(request, org_id, editable_identity=False)
        return await self.templates.TemplateResponse(request, "organisation_detail.html", context)

    @expose("/organisations/{org_id:int}/update", methods=["POST"], identity="organisation-update")
    async def update_identity(self, request: Request):
        if not request.session.get("is_superadmin", False):
            raise HTTPException(status_code=403, detail="Superadmin only")
        org_id = request.path_params["org_id"]
        form = await request.form()
        name = (form.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="Name is required")
        with Session(engine) as session:
            from autosend.admin_models import Organisation as OrganisationRow

            org = session.get(OrganisationRow, org_id)
            if org is None:
                raise HTTPException(status_code=404, detail="Not found")
            # Deliberately not touching slug here - same as the old
            # OrganisationAdmin edit form (form_columns=[name, active]),
            # so a rename can't invalidate an org's existing login/PCO
            # webhook URLs that may already reference the current slug.
            org.name = name
            org.active = "active" in form
            session.commit()
        return RedirectResponse(url=f"/organisations/{org_id}", status_code=303)

    @expose("/organisation/update", methods=["POST"], identity="own-organisation-update")
    async def update_own_identity(self, request: Request):
        # Org-admin-facing counterpart to update_identity above: name only
        # (no active toggle - deactivating your own org isn't an org
        # admin's call to make), and scoped to the session's own org_id
        # rather than a path param, same "never trust a posted org_id"
        # rule as save_token/save_unit_webhook below.
        is_superadmin = request.session.get("is_superadmin", False)
        if not is_superadmin and not request.session.get("is_org_admin", False):
            raise HTTPException(status_code=403, detail="Not permitted")
        org_id = request.session.get("org_id")
        if org_id is None:
            raise HTTPException(status_code=403, detail="Not permitted")

        from autosend import storage

        if storage.get_organisation(org_id) is None:
            raise HTTPException(status_code=404, detail="Not found")

        form = await request.form()
        name = (form.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="Name is required")
        storage.update_organisation_name(org_id, name)
        return RedirectResponse(url="/organisation", status_code=303)


class PcoSettingsView(BaseView):
    name = "PCO Settings"
    icon = "fa-solid fa-key"
    identity = "pco-config-page"

    def is_accessible(self, request: Request) -> bool:
        from autosend.web.auth import pco_module_visible

        is_superadmin = request.session.get("is_superadmin", False)
        is_org_admin = request.session.get("is_org_admin", False)
        # pco_module_visible always returns True for a superadmin
        # (spans every org, same bypass as elsewhere) - for an org admin
        # it additionally requires their own org to have the PCO module
        # enabled, so this page isn't reachable by an org admin whose org
        # isn't provisioned for PCO.
        return (is_superadmin or is_org_admin) and pco_module_visible(request)

    def is_visible(self, request: Request) -> bool:
        return self.is_accessible(request)

    def _resolve_org_id(self, request: Request) -> int | None:
        if request.session.get("is_superadmin", False):
            org_id_param = request.query_params.get("org_id")
            return int(org_id_param) if org_id_param else None
        return request.session.get("org_id")

    @expose("/pco-settings", methods=["GET"], identity="pco-config-page")
    async def page(self, request: Request):
        from autosend.web.auth import get_current_web_user
        from autosend import storage

        # sqladmin doesn't enforce is_accessible/is_visible on a BaseView's
        # own @expose routes - only on its auto-generated nav/CRUD routes -
        # so this hand-rolled route needs its own explicit check, same
        # gap already fixed for AutomationsView.
        if not self.is_accessible(request):
            raise HTTPException(status_code=403, detail="Not permitted")
        is_superadmin = request.session.get("is_superadmin", False)

        org_id = self._resolve_org_id(request)
        if org_id is None:
            # Superadmin with no org picked yet - send them to choose one.
            return RedirectResponse(url="/organisations", status_code=303)

        org = storage.get_organisation(org_id)
        if org is None:
            raise HTTPException(status_code=404, detail="Not found")

        with Session(engine) as session:
            pco_settings = session.execute(
                select(PCOOrganizationSettings).where(PCOOrganizationSettings.org_id == org_id)
            ).scalar_one_or_none()
            units = session.execute(
                select(Unit).where(Unit.org_id == org_id).order_by(Unit.name)
            ).scalars().all()
            unit_rows = [
                {
                    "id": u.id,
                    "name": u.name,
                    "pco_webhook_user_name": u.pco_webhook_user_name or "",
                    "pco_campus_id": u.pco_campus_id or "",
                    "has_secret": bool(u.pco_webhook_secret),
                }
                for u in units
            ]

        return await self.templates.TemplateResponse(
            request,
            "pco_settings.html",
            {
                "user": get_current_web_user(request),
                "org": org,
                "pco_settings": pco_settings,
                "units": unit_rows,
                "is_superadmin": is_superadmin,
            },
        )

    @expose("/pco-settings/token", methods=["POST"], identity="pco-config-token-save")
    async def save_token(self, request: Request):
        # Same "BaseView routes aren't auto-guarded" gap as page() above.
        if not self.is_accessible(request):
            raise HTTPException(status_code=403, detail="Not permitted")
        is_superadmin = request.session.get("is_superadmin", False)

        form = await request.form()
        if is_superadmin:
            org_id = int(form["org_id"])
        else:
            # Never trust a posted org_id for a non-superadmin - same rule
            # as every other org-scoped write in admin_views.py.
            org_id = request.session.get("org_id")

        from autosend import storage

        if storage.get_organisation(org_id) is None:
            raise HTTPException(status_code=404, detail="Not found")

        token_id = (form.get("pco_token_id") or "").strip()
        token_secret = form.get("pco_token_secret") or ""
        if not token_id:
            raise HTTPException(status_code=400, detail="PCO Token ID is required")

        with Session(engine) as session:
            existing = session.execute(
                select(PCOOrganizationSettings).where(PCOOrganizationSettings.org_id == org_id)
            ).scalar_one_or_none()
            if existing is None:
                if not token_secret:
                    raise HTTPException(status_code=400, detail="PCO Token Secret is required")
                session.add(PCOOrganizationSettings(
                    org_id=org_id, pco_token_id=token_id, pco_token_secret=token_secret,
                    created_at=datetime.now(timezone.utc).isoformat(),
                ))
            else:
                existing.pco_token_id = token_id
                if token_secret:  # blank on edit = keep existing, same convention as elsewhere
                    existing.pco_token_secret = token_secret
            session.commit()

        redirect_url = f"/pco-settings?org_id={org_id}" if is_superadmin else "/pco-settings"
        return RedirectResponse(url=redirect_url, status_code=303)

    @expose("/pco-settings/unit/{unit_id:int}", methods=["POST"], identity="pco-config-unit-save")
    async def save_unit_webhook(self, request: Request):
        # Same "BaseView routes aren't auto-guarded" gap as page() above.
        if not self.is_accessible(request):
            raise HTTPException(status_code=403, detail="Not permitted")
        is_superadmin = request.session.get("is_superadmin", False)

        unit_id = request.path_params["unit_id"]
        form = await request.form()

        with Session(engine) as session:
            unit = session.get(Unit, unit_id)
            if unit is None:
                raise HTTPException(status_code=404, detail="Not found")
            if not is_superadmin and unit.org_id != request.session.get("org_id"):
                raise HTTPException(status_code=404, detail="Not found")

            org_id = unit.org_id
            secret = form.get("pco_webhook_secret") or ""
            if secret:
                unit.pco_webhook_secret = secret
            unit.pco_webhook_user_name = (form.get("pco_webhook_user_name") or "").strip() or None
            unit.pco_campus_id = (form.get("pco_campus_id") or "").strip() or None
            session.commit()

        redirect_url = f"/pco-settings?org_id={org_id}" if is_superadmin else "/pco-settings"
        return RedirectResponse(url=redirect_url, status_code=303)


class EmailWaSettingsView(BaseView):
    """Reference/management page for the email-to-WhatsApp module -
    creating and editing an integration's provider/template/variable
    mapping happens on the Automations page (see admin_pages.AutomationsView
    and automations.html's Email-to-WhatsApp section, same split as PCO's
    "configure on Automations, manage settings here" pattern implies for
    PcoSettingsView above); this page is where staff go to see every
    configured integration's generated receiving address (masked, with a
    reveal-in-place toggle - it's a plaintext value someone needs to copy
    into a third-party platform, not a secret, so PasswordField-style
    hiding would be the wrong tradeoff) and delete one if it's no longer
    needed."""
    name = "Email-to-WhatsApp Settings"
    icon = "fa-solid fa-envelope"
    identity = "email-wa-config-page"

    def is_accessible(self, request: Request) -> bool:
        from autosend.web.auth import email_wa_module_visible

        is_superadmin = request.session.get("is_superadmin", False)
        is_org_admin = request.session.get("is_org_admin", False)
        return (is_superadmin or is_org_admin) and email_wa_module_visible(request)

    def is_visible(self, request: Request) -> bool:
        return self.is_accessible(request)

    def _resolve_org_id(self, request: Request) -> int | None:
        if request.session.get("is_superadmin", False):
            org_id_param = request.query_params.get("org_id")
            return int(org_id_param) if org_id_param else None
        return request.session.get("org_id")

    @expose("/email-wa-settings", methods=["GET"], identity="email-wa-config-page")
    async def page(self, request: Request):
        from autosend.config import settings
        from autosend.web.auth import get_current_web_user, resolve_unit_ids

        # Same "BaseView routes aren't auto-guarded" gap as PcoSettingsView
        # above - this hand-rolled route needs its own explicit check.
        if not self.is_accessible(request):
            raise HTTPException(status_code=403, detail="Not permitted")
        is_superadmin = request.session.get("is_superadmin", False)

        org_id = self._resolve_org_id(request)
        if org_id is None:
            return RedirectResponse(url="/organisations", status_code=303)

        from autosend import storage

        org = storage.get_organisation(org_id)
        if org is None:
            raise HTTPException(status_code=404, detail="Not found")

        if is_superadmin:
            unit_ids = storage.get_unit_ids_for_org(org_id)
        else:
            unit_ids = resolve_unit_ids(request.session)
        integrations = storage.list_email_integrations(unit_ids)

        return await self.templates.TemplateResponse(
            request,
            "email_wa_settings.html",
            {
                "user": get_current_web_user(request),
                "org": org,
                "integrations": integrations,
                "domain": settings.email_wa_inbound_domain,
                "is_superadmin": is_superadmin,
            },
        )

    @expose("/email-wa-settings/{integration_id:int}/delete", methods=["POST"], identity="email-wa-config-delete")
    async def delete_integration(self, request: Request):
        if not self.is_accessible(request):
            raise HTTPException(status_code=403, detail="Not permitted")
        is_superadmin = request.session.get("is_superadmin", False)
        integration_id = request.path_params["integration_id"]

        from autosend.web.auth import resolve_unit_ids
        from autosend import storage

        existing = storage.get_email_integration_by_id(integration_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="Not found")
        if not is_superadmin:
            allowed_unit_ids = set(resolve_unit_ids(request.session))
            if existing["unit_id"] not in allowed_unit_ids:
                raise HTTPException(status_code=404, detail="Not found")

        storage.delete_email_integration(integration_id)

        org_id_param = request.query_params.get("org_id")
        redirect_url = f"/email-wa-settings?org_id={org_id_param}" if is_superadmin and org_id_param else "/email-wa-settings"
        return RedirectResponse(url=redirect_url, status_code=303)

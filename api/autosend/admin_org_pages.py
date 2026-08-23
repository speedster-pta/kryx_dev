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
  admin.py) - existing links/tests/plain-users access to
  /pco-webhook/* are untouched; this is an additional, friendlier
  surface for whoever is configuring PCO for a whole org (superadmin or
  that org's own org-admin).

  StitchSettingsView - same "one page for every unit's config" pattern
  as PcoSettingsView, for each unit's Stitch Express client_id/secret
  (previously only reachable via the generic StitchCredentialsAdmin CRUD
  screen, which stays registered and fully functional as a fallback).
"""
from datetime import datetime, timezone

from fastapi import HTTPException
from sqladmin import BaseView, expose
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from autosend.admin_models import engine, PCOOrganizationSettings, StitchCredentials, Unit
from autosend.utils.logging import get_logger

logger = get_logger(__name__)


def _visible_unit_ids_within_org(request: Request) -> set[int] | None:
    """None means "no filter" - superadmin/org-admin see every unit in
    the org this page has already resolved via _resolve_org_id; otherwise
    the specific unit ids a plain user's session may see, same
    resolve_unit_ids() choke point ScopedModelView itself is built on.

    Only safe to call from a page that ALSO filters its query by
    Unit.org_id == org_id (PcoSettingsView, StitchSettingsView do) -
    unlike admin_pages.py's "_scoped_unit_ids", this one deliberately
    unfilters for org-admins too, which would leak every other org's
    units if used standalone without that accompanying org_id filter."""
    if request.session.get("is_superadmin", False) or request.session.get("is_org_admin", False):
        return None
    from autosend.web.auth import resolve_unit_ids

    return set(resolve_unit_ids(request.session))


def _resolve_org_id(request: Request) -> int | None:
    """Which org a page/route should act on: a superadmin can switch
    between orgs via ?org_id=, since they aren't tied to one; everyone
    else is pinned to their own session org_id regardless of any
    query param (never trust a client-supplied org_id past this point)."""
    if request.session.get("is_superadmin", False):
        org_id_param = request.query_params.get("org_id")
        return int(org_id_param) if org_id_param else None
    return request.session.get("org_id")


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
            was_active = org.active
            org.name = name
            org.active = "active" in form
            session.commit()
            now_active = org.active

        if was_active != now_active:
            # Immediate effect on any live recurring serving-reminder
            # jobs, not just at next restart - same reasoning/pattern as
            # ModulesView.toggle()'s PCO enable/disable handling
            # (admin_pages.py). One-shot sends (campaigns, registration
            # confirmations, form confirmations, email-to-WhatsApp) are
            # re-checked at fire time instead (storage.is_org_active), so
            # they don't need an equivalent wake/cancel here.
            from autosend.scheduler import cancel_org_serving_rule_jobs, reschedule_org_serving_rules

            if now_active:
                reschedule_org_serving_rules(org_id)
            else:
                cancel_org_serving_rule_jobs(org_id)

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
    """Org-level PCO connection (superadmin/org-admin manage this: OAuth
    connect/disconnect, Personal Access Token) plus every accessible
    unit's Campus Configuration and webhook secrets - same "one page for
    every unit's config" pattern as StitchSettingsView.

    Reachable by plain unit-scoped users too, not just superadmin/org
    admin - registering their own unit's PCO webhook subscription secret
    (what used to be the standalone /pco-webhook/list page) is exactly
    what unit-scoped users do day to day, same policy as
    StitchSettingsView. Plain users get a read-only view of the org's
    connection state (no connect/disconnect/token controls - that stays
    superadmin/org-admin only, enforced again in save_token below) and
    Campus Configuration filtered to their own resolve_unit_ids()
    unit(s), same "None means no filter" pattern as
    the shared _visible_unit_ids_within_org helper."""
    name = "PCO Settings"
    icon = "fa-solid fa-key"
    identity = "pco-config-page"

    def is_accessible(self, request: Request) -> bool:
        from autosend.web.auth import pco_module_visible

        # pco_module_visible always returns True for a superadmin (spans
        # every org) - for everyone else it additionally requires their
        # own org to have the PCO module enabled, so this page isn't
        # reachable by anyone whose org isn't provisioned for PCO.
        return pco_module_visible(request)

    def is_visible(self, request: Request) -> bool:
        return self.is_accessible(request)

    def _can_manage_connection(self, request: Request) -> bool:
        """Only superadmin/org-admin may connect/disconnect/change the
        org's PCO credentials - plain users get a read-only view of
        connection state (see page()/pco_settings.html)."""
        return request.session.get("is_superadmin", False) or request.session.get("is_org_admin", False)

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
        can_manage_connection = self._can_manage_connection(request)

        org_id = _resolve_org_id(request)
        if org_id is None:
            # Superadmin with no org picked yet - send them to choose one.
            return RedirectResponse(url="/organisations", status_code=303)

        org = storage.get_organisation(org_id)
        if org is None:
            raise HTTPException(status_code=404, detail="Not found")

        visible_unit_ids = _visible_unit_ids_within_org(request)

        with Session(engine) as session:
            pco_settings = session.execute(
                select(PCOOrganizationSettings).where(PCOOrganizationSettings.org_id == org_id)
            ).scalar_one_or_none()
            query = select(Unit).where(Unit.org_id == org_id).order_by(Unit.name)
            if visible_unit_ids is not None:
                query = query.where(Unit.id.in_(visible_unit_ids))
            units = session.execute(query).scalars().all()
            from autosend.storage.units import ensure_webhook_slug

            base_url = str(request.base_url).rstrip("/")
            unit_rows = []
            for u in units:
                # Keyed off webhook_slug (random, globally unique), not
                # the human-readable `slug` column - see
                # integrations/webhooks.py for why that distinction
                # matters (two orgs' default units are both slugged
                # "main"). ensure_webhook_slug only mints one once the
                # org has actually been granted the PCO module - a
                # superadmin can reach this page before that's true
                # (pco_module_visible bypasses the grant check for them),
                # in which case there's nothing to show yet.
                webhook_slug = ensure_webhook_slug(u.id)
                unit_rows.append({
                    "id": u.id,
                    "name": u.name,
                    "pco_webhook_user_name": u.pco_webhook_user_name or "",
                    "pco_campus_id": u.pco_campus_id or "",
                    "has_secret": bool(u.pco_webhook_secret),
                    "webhook_url": (
                        f"{base_url}/webhooks/planning-center/people-form/{webhook_slug}"
                        if webhook_slug else None
                    ),
                    # Additional webhooks beyond the primary one above -
                    # e.g. a second PCO webhook subscription created by a
                    # different PCO user, for forms only they can see.
                    # See unit_webhook_secrets' schema.py docstring.
                    "extra_secrets": storage.list_unit_webhook_secrets(u.id),
                })

        oauth_platform_configured = bool(
            (storage.get_pco_platform_settings() or {}).get("client_secret")
        )

        return await self.templates.TemplateResponse(
            request,
            "pco_settings.html",
            {
                "user": get_current_web_user(request),
                "org": org,
                "pco_settings": pco_settings,
                "units": unit_rows,
                "is_superadmin": is_superadmin,
                "can_manage_connection": can_manage_connection,
                "oauth_platform_configured": oauth_platform_configured,
            },
        )

    @expose("/pco-settings/campuses", methods=["GET"], identity="pco-config-campuses")
    async def campuses(self, request: Request):
        """JSON list of this org's PCO campuses ({"id", "name"}), for the
        campus-picker dropdown in pco_settings.html - fetched client-side
        rather than server-rendered into page() so a slow/failed PCO call
        doesn't block the whole settings page from loading, and so it can
        be re-fetched on demand (e.g. right after connecting via OAuth)
        without a full page reload. Works for either PAT or OAuth-based
        orgs - both give get_pco_org_client a usable token; it's the org's
        general PCO connection, not anything OAuth-specific."""
        if not self.is_accessible(request):
            raise HTTPException(status_code=403, detail="Not permitted")

        from autosend import storage
        from autosend.clients import get_pco_org_client

        org_id = _resolve_org_id(request)
        if org_id is None or storage.get_organisation(org_id) is None:
            raise HTTPException(status_code=404, detail="Not found")

        if storage.get_pco_org_settings(org_id) is None:
            raise HTTPException(
                status_code=503,
                detail="Connect via Planning Center or set a Personal Access Token first.",
            )

        try:
            pco_client = get_pco_org_client(org_id)
            campuses = await pco_client.get_campuses()
        except Exception as exc:
            logger.exception("Failed to fetch PCO campuses for org %s", org_id)
            raise HTTPException(
                status_code=502, detail=f"Failed to fetch campuses from Planning Center: {exc}"
            )
        # sqladmin's BaseView.@expose routes need a real Response back -
        # unlike a plain FastAPI @router.get, returning a bare list here
        # gets ASGI-called directly by sqladmin's routing and blows up
        # with "'list' object is not callable" (found live via a real
        # 500 on this exact route).
        return JSONResponse(campuses)

    @expose("/pco-settings/token", methods=["POST"], identity="pco-config-token-save")
    async def save_token(self, request: Request):
        # Same "BaseView routes aren't auto-guarded" gap as page() above.
        if not self.is_accessible(request):
            raise HTTPException(status_code=403, detail="Not permitted")
        # The org's PCO connection/token is superadmin/org-admin only -
        # plain users get no form for this (see pco_settings.html), but
        # that's UI-only, so re-check server-side too against a crafted
        # POST, same defense-in-depth rule as every other scope-widening
        # field in this codebase.
        if not self._can_manage_connection(request):
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
                else:
                    token_secret = existing.pco_token_secret
            session.commit()

        # Best-effort, same reasoning as pco_oauth_router.py's own
        # subdomain sync - a lookup hiccup shouldn't fail this save. No
        # manual entry field for this any more, so this is the only way
        # a PAT-connected org's Church Center subdomain gets set.
        from autosend.clients import invalidate_pco_org_cache
        await invalidate_pco_org_cache(org_id)
        try:
            from autosend.clients import get_pco_org_client

            info = await get_pco_org_client(org_id).get_organization_info()
            subdomain = info.get("church_center_subdomain")
            if subdomain:
                storage.sync_pco_subdomain(org_id, subdomain)
        except Exception:
            logger.exception("Failed to sync PCO Church Center subdomain for org %s", org_id)

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
        visible_unit_ids = _visible_unit_ids_within_org(request)

        with Session(engine) as session:
            unit = session.get(Unit, unit_id)
            if unit is None:
                raise HTTPException(status_code=404, detail="Not found")
            if not is_superadmin and unit.org_id != request.session.get("org_id"):
                raise HTTPException(status_code=404, detail="Not found")
            if visible_unit_ids is not None and unit_id not in visible_unit_ids:
                raise HTTPException(status_code=404, detail="Not found")

            org_id = unit.org_id
            # pco_webhook_secret/pco_webhook_user_name are no longer
            # fields on this form (webhook secrets are managed entirely
            # via /pco-settings/unit/{id}/webhook-secrets now, see
            # pco_settings.html's "Webhooks" card) - only ever update
            # them here if a caller genuinely posts them (e.g. a stale
            # bookmarked form), never blank them out just because this
            # form no longer sends them.
            secret = form.get("pco_webhook_secret") or ""
            if secret:
                unit.pco_webhook_secret = secret
            if "pco_webhook_user_name" in form:
                unit.pco_webhook_user_name = (form.get("pco_webhook_user_name") or "").strip() or None
            unit.pco_campus_id = (form.get("pco_campus_id") or "").strip() or None
            session.commit()

        redirect_url = f"/pco-settings?org_id={org_id}" if is_superadmin else "/pco-settings"
        return RedirectResponse(url=redirect_url, status_code=303)

    def _unit_in_scope_or_404(self, request: Request, unit_id: int) -> "Unit":
        """Shared by the extra-webhook-secret routes below - same
        is_superadmin-bypass / org_id-match / visible_unit_ids check as
        save_unit_webhook above, factored out since both new routes need
        it."""
        is_superadmin = request.session.get("is_superadmin", False)
        visible_unit_ids = _visible_unit_ids_within_org(request)
        with Session(engine) as session:
            unit = session.get(Unit, unit_id)
            if unit is None:
                raise HTTPException(status_code=404, detail="Not found")
            if not is_superadmin and unit.org_id != request.session.get("org_id"):
                raise HTTPException(status_code=404, detail="Not found")
            if visible_unit_ids is not None and unit_id not in visible_unit_ids:
                raise HTTPException(status_code=404, detail="Not found")
            return unit

    @expose(
        "/pco-settings/unit/{unit_id:int}/webhook-secrets",
        methods=["POST"], identity="pco-config-unit-webhook-secret-add",
    )
    async def add_unit_webhook_secret(self, request: Request):
        """Adds an additional PCO webhook Authenticity Secret for this
        unit, on top of the primary one saved by save_unit_webhook above -
        lets a second (or third...) PCO webhook subscription, created by
        a different PCO user, deliver to the same unit URL. See
        unit_webhook_secrets' schema.py docstring for why this exists."""
        if not self.is_accessible(request):
            raise HTTPException(status_code=403, detail="Not permitted")
        is_superadmin = request.session.get("is_superadmin", False)
        from autosend import storage

        unit_id = request.path_params["unit_id"]
        unit = self._unit_in_scope_or_404(request, unit_id)
        org_id = unit.org_id

        form = await request.form()
        secret = (form.get("secret") or "").strip()
        label = (form.get("label") or "").strip() or None
        if not secret:
            raise HTTPException(status_code=400, detail="Secret is required")

        storage.create_unit_webhook_secret(unit_id, secret, label=label)

        redirect_url = f"/pco-settings?org_id={org_id}" if is_superadmin else "/pco-settings"
        return RedirectResponse(url=redirect_url, status_code=303)

    @expose(
        "/pco-settings/unit/{unit_id:int}/webhook-secrets/{secret_id:int}/delete",
        methods=["POST"], identity="pco-config-unit-webhook-secret-delete",
    )
    async def delete_unit_webhook_secret(self, request: Request):
        if not self.is_accessible(request):
            raise HTTPException(status_code=403, detail="Not permitted")
        is_superadmin = request.session.get("is_superadmin", False)
        from autosend import storage

        unit_id = request.path_params["unit_id"]
        secret_id = request.path_params["secret_id"]
        unit = self._unit_in_scope_or_404(request, unit_id)
        org_id = unit.org_id

        # Scoped by unit_id inside the storage call too, not just the
        # unit-ownership check above - belt and braces against a guessed
        # secret_id that happens to belong to some other unit.
        storage.delete_unit_webhook_secret(unit_id, secret_id)

        redirect_url = f"/pco-settings?org_id={org_id}" if is_superadmin else "/pco-settings"
        return RedirectResponse(url=redirect_url, status_code=303)


class StitchSettingsView(BaseView):
    """Every accessible unit's Stitch Express credentials (previously
    only reachable via the generic StitchCredentialsAdmin CRUD screen,
    which stays registered and fully functional as a fallback) - same
    "one page for every unit's config" pattern as PcoSettingsView above.

    Reachable by plain unit-scoped users too, not just superadmin/org
    admin - StitchCredentialsAdmin itself has no role-based
    is_accessible override for the same reason (managing your own
    unit's payment-link credentials is exactly what unit-scoped users do
    day to day, same policy as WhatsAppNumberAdmin/WhatsAppNumbersView).
    Scope within an org is enforced by which units page()/save_unit_stitch
    actually show/accept - superadmin/org admin see every unit in the
    org, plain users see only their own resolve_unit_ids() unit(s). But
    the page itself is only reachable at all when the org's Stitch module
    is provisioned/enabled (stitch_module_visible), same gate as
    PcoSettingsView applies for PCO - an org that hasn't bought/enabled
    Stitch gets no nav link and no page."""
    name = "Stitch"
    icon = "fa-solid fa-money-check-dollar"
    identity = "stitch-config-page"

    def is_accessible(self, request: Request) -> bool:
        from autosend.web.auth import stitch_module_visible

        return stitch_module_visible(request)

    def is_visible(self, request: Request) -> bool:
        return self.is_accessible(request)

    @expose("/stitch-settings", methods=["GET"], identity="stitch-config-page")
    async def page(self, request: Request):
        from autosend.integrations.stitch import STITCH_BASE_URL
        from autosend.web.auth import get_current_web_user
        from autosend import storage

        # sqladmin doesn't enforce is_accessible/is_visible on a BaseView's
        # own @expose routes - only on its auto-generated nav/CRUD routes -
        # so this hand-rolled route needs its own explicit check, same
        # gap already fixed for PcoSettingsView/AutomationsView.
        if not self.is_accessible(request):
            raise HTTPException(status_code=403, detail="Not permitted")

        is_superadmin = request.session.get("is_superadmin", False)

        org_id = _resolve_org_id(request)
        if org_id is None:
            return RedirectResponse(url="/organisations", status_code=303)

        org = storage.get_organisation(org_id)
        if org is None:
            raise HTTPException(status_code=404, detail="Not found")

        visible_unit_ids = _visible_unit_ids_within_org(request)

        with Session(engine) as session:
            query = select(Unit).where(Unit.org_id == org_id).order_by(Unit.name)
            if visible_unit_ids is not None:
                query = query.where(Unit.id.in_(visible_unit_ids))
            units = session.execute(query).scalars().all()
            credentials_by_unit = {
                c.unit_id: c
                for c in session.execute(
                    select(StitchCredentials).where(
                        StitchCredentials.unit_id.in_([u.id for u in units])
                    )
                ).scalars().all()
            }
            unit_rows = [
                {
                    "id": u.id,
                    "name": u.name,
                    "client_id": credentials_by_unit[u.id].client_id if u.id in credentials_by_unit else "",
                    "has_secret": bool(credentials_by_unit[u.id].client_secret) if u.id in credentials_by_unit else False,
                    "active": credentials_by_unit[u.id].active if u.id in credentials_by_unit else False,
                }
                for u in units
            ]

        return await self.templates.TemplateResponse(
            request,
            "stitch_settings.html",
            {
                "user": get_current_web_user(request),
                "org": org,
                "units": unit_rows,
                "is_superadmin": is_superadmin,
                "stitch_base_url": STITCH_BASE_URL,
            },
        )

    @expose("/stitch-settings/unit/{unit_id:int}", methods=["POST"], identity="stitch-config-unit-save")
    async def save_unit_stitch(self, request: Request):
        if not self.is_accessible(request):
            raise HTTPException(status_code=403, detail="Not permitted")

        is_superadmin = request.session.get("is_superadmin", False)
        unit_id = request.path_params["unit_id"]
        form = await request.form()

        visible_unit_ids = _visible_unit_ids_within_org(request)

        with Session(engine) as session:
            unit = session.get(Unit, unit_id)
            if unit is None:
                raise HTTPException(status_code=404, detail="Not found")
            if not is_superadmin and unit.org_id != request.session.get("org_id"):
                raise HTTPException(status_code=404, detail="Not found")
            if visible_unit_ids is not None and unit_id not in visible_unit_ids:
                raise HTTPException(status_code=404, detail="Not found")

            org_id = unit.org_id
            client_id = (form.get("client_id") or "").strip()
            client_secret = form.get("client_secret") or ""
            active = "active" in form

            existing = session.execute(
                select(StitchCredentials).where(StitchCredentials.unit_id == unit_id)
            ).scalar_one_or_none()
            if existing is None:
                if not client_id or not client_secret:
                    raise HTTPException(
                        status_code=400, detail="Client ID and Client Secret are required"
                    )
                session.add(StitchCredentials(
                    unit_id=unit_id, client_id=client_id, client_secret=client_secret,
                    active=active, created_at=datetime.now(timezone.utc).isoformat(),
                ))
            else:
                if client_id:
                    existing.client_id = client_id
                if client_secret:  # blank on edit = keep existing, same convention as elsewhere
                    existing.client_secret = client_secret
                existing.active = active
            session.commit()

        redirect_url = f"/stitch-settings?org_id={org_id}" if is_superadmin else "/stitch-settings"
        return RedirectResponse(url=redirect_url, status_code=303)


class SmeMetricsSettingsView(BaseView):
    """Reference/management page for the SME Metrics module - creating and
    editing an integration's provider/template/variable mapping happens
    on the Automations page (see admin_pages.AutomationsView and
    automations.html's SME Metrics section, same split as PCO's "configure
    on Automations, manage settings here" pattern implies for
    PcoSettingsView above); this page is where users go to see every
    configured integration's generated receiving address (masked, with a
    reveal-in-place toggle - it's a plaintext value someone needs to copy
    into a third-party platform, not a secret, so PasswordField-style
    hiding would be the wrong tradeoff) and delete one if it's no longer
    needed.

    Was EmailWaSettingsView at /email-wa-settings before SME Metrics
    became its own module - that name/route now belongs to the class
    below, for the new, unrelated, genuinely generic Email-to-WhatsApp
    module. Shares email_integration_settings.html with that class,
    parametrised by page_title/automations_url/delete_url_base."""
    name = "SME Metrics Settings"
    icon = "fa-solid fa-envelope"
    identity = "sme-metrics-config-page"

    def is_accessible(self, request: Request) -> bool:
        from autosend.web.auth import sme_metrics_module_visible

        is_superadmin = request.session.get("is_superadmin", False)
        is_org_admin = request.session.get("is_org_admin", False)
        return (is_superadmin or is_org_admin) and sme_metrics_module_visible(request)

    def is_visible(self, request: Request) -> bool:
        return self.is_accessible(request)

    @expose("/sme-metrics-settings", methods=["GET"], identity="sme-metrics-config-page")
    async def page(self, request: Request):
        from autosend.config import settings
        from autosend.web.auth import get_current_web_user, resolve_unit_ids

        # Same "BaseView routes aren't auto-guarded" gap as PcoSettingsView
        # above - this hand-rolled route needs its own explicit check.
        if not self.is_accessible(request):
            raise HTTPException(status_code=403, detail="Not permitted")
        is_superadmin = request.session.get("is_superadmin", False)

        org_id = _resolve_org_id(request)
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
            "email_integration_settings.html",
            {
                "user": get_current_web_user(request),
                "org": org,
                "integrations": integrations,
                "domain": settings.email_wa_inbound_domain,
                "is_superadmin": is_superadmin,
                "page_title": "SME Metrics Settings",
                "automations_url": "/automations/sme-metrics",
                "automations_label": "SME Metrics Automations",
                "delete_url_base": "/sme-metrics-settings",
            },
        )

    @expose("/sme-metrics-settings/{integration_id:int}/delete", methods=["POST"], identity="sme-metrics-config-delete")
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
        redirect_url = (
            f"/sme-metrics-settings?org_id={org_id_param}" if is_superadmin and org_id_param
            else "/sme-metrics-settings"
        )
        return RedirectResponse(url=redirect_url, status_code=303)


class EmailWaSettingsView(BaseView):
    """Reference/management page for the new, genuinely generic
    Email-to-WhatsApp module - identical shape to SmeMetricsSettingsView
    above (see that class's own docstring for the full mechanism), just
    pointed at storage/email_wa.py's own tables and gated on
    web.auth.email_wa_module_visible instead. Shares
    email_integration_settings.html with that class, parametrised
    differently."""
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

    @expose("/email-wa-settings", methods=["GET"], identity="email-wa-config-page")
    async def page(self, request: Request):
        from autosend.config import settings
        from autosend.web.auth import get_current_web_user, resolve_unit_ids

        if not self.is_accessible(request):
            raise HTTPException(status_code=403, detail="Not permitted")
        is_superadmin = request.session.get("is_superadmin", False)

        org_id = _resolve_org_id(request)
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
        integrations = storage.list_email_wa_integrations(unit_ids)

        return await self.templates.TemplateResponse(
            request,
            "email_integration_settings.html",
            {
                "user": get_current_web_user(request),
                "org": org,
                "integrations": integrations,
                "domain": settings.generic_email_wa_inbound_domain,
                "is_superadmin": is_superadmin,
                "page_title": "Email-to-WhatsApp Settings",
                "automations_url": "/automations/email-wa",
                "automations_label": "Email-to-WhatsApp Automations",
                "delete_url_base": "/email-wa-settings",
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

        existing = storage.get_email_wa_integration_by_id(integration_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="Not found")
        if not is_superadmin:
            allowed_unit_ids = set(resolve_unit_ids(request.session))
            if existing["unit_id"] not in allowed_unit_ids:
                raise HTTPException(status_code=404, detail="Not found")

        storage.delete_email_wa_integration(integration_id)

        org_id_param = request.query_params.get("org_id")
        redirect_url = (
            f"/email-wa-settings?org_id={org_id_param}" if is_superadmin and org_id_param
            else "/email-wa-settings"
        )
        return RedirectResponse(url=redirect_url, status_code=303)


class BillingDashboardView(BaseView):
    """Superadmin-only page listing every org's subscription status,
    with a "comp this org" action - mirrors PcoSettingsView's exact
    shape above (inline is_accessible re-check on every @expose route,
    since sqladmin never auto-guards a BaseView's own hand-rolled
    routes)."""
    name = "Billing"
    icon = "fa-solid fa-file-invoice-dollar"
    identity = "billing-dashboard-page"

    def is_accessible(self, request: Request) -> bool:
        return request.session.get("is_superadmin", False)

    def is_visible(self, request: Request) -> bool:
        return self.is_accessible(request)

    @expose("/billing-dashboard", methods=["GET"], identity="billing-dashboard-page")
    async def page(self, request: Request):
        from autosend.web.auth import get_current_web_user
        from autosend import storage

        if not self.is_accessible(request):
            raise HTTPException(status_code=403, detail="Superadmin only")

        orgs = storage.list_organisations(active_only=False)
        rows = []
        for org in orgs:
            subscription = storage.get_subscription(org.id)
            rows.append({
                "org": org,
                "status": subscription.status if subscription else "no_subscription",
                "current_period_end": subscription.current_period_end if subscription else None,
            })

        return await self.templates.TemplateResponse(
            request, "billing_dashboard.html",
            {"user": get_current_web_user(request), "rows": rows},
        )

    @expose("/billing-dashboard/{org_id:int}/comp", methods=["POST"], identity="billing-dashboard-comp")
    async def comp(self, request: Request):
        if not self.is_accessible(request):
            raise HTTPException(status_code=403, detail="Superadmin only")

        from autosend import storage
        from autosend.billing import engine

        org_id = request.path_params["org_id"]
        if storage.get_organisation(org_id) is None:
            raise HTTPException(status_code=404, detail="Not found")

        form = await request.form()
        note = (form.get("note") or "").strip()
        engine.comp_org(org_id, note=note)

        return RedirectResponse(url="/billing-dashboard", status_code=303)


class BillingCatalogueView(BaseView):
    """Superadmin hub page for the platform's pricing catalogue - Plans,
    Add-ons, Coupons. This app's nav (sqladmin_theme/sqladmin/layout.html)
    is a hand-coded dropdown, not sqladmin's auto-generated sidebar
    (admin.add_view() alone makes a view's routes reachable, but creates
    no visible link anywhere in this custom theme) - so BillingPlanAdmin/
    BillingAddonAdmin/CouponAdmin (admin_views.py) were only ever reachable
    by typing their URL directly. Rather than adding three separate raw
    ModelView links to the nav dropdown, this consolidates them onto one
    page (mirroring BillingDashboardView's shape above), with each
    section linking through to that model's already-working sqladmin
    CRUD screens (edit/create/delete) - not reimplementing those forms
    here, just making them discoverable."""
    name = "Billing Catalogue"
    icon = "fa-solid fa-tags"
    identity = "billing-catalogue-page"

    def is_accessible(self, request: Request) -> bool:
        return request.session.get("is_superadmin", False)

    def is_visible(self, request: Request) -> bool:
        return self.is_accessible(request)

    @expose("/billing-catalogue", methods=["GET"], identity="billing-catalogue-page")
    async def page(self, request: Request):
        from autosend.web.auth import get_current_web_user
        from autosend import storage

        if not self.is_accessible(request):
            raise HTTPException(status_code=403, detail="Superadmin only")

        all_addons = storage.list_addons(active_only=False)
        return await self.templates.TemplateResponse(
            request, "billing_catalogue.html",
            {
                "user": get_current_web_user(request),
                "plans": storage.list_plans(active_only=False),
                # Split into two sections per product framing: "add-ons"
                # expand core capacity and can be bought in multiples
                # (kind='capacity'), "integrations" are a plain on/off
                # module toggle (kind='integration') - see
                # billing/entitlements.py for how capacity_key is
                # actually consumed.
                "addons": [a for a in all_addons if a["kind"] == "capacity"],
                # Alphabetical by name, not the underlying list_addons()
                # price ordering - an org admin scanning for "PCO" or
                # "Stitch" shouldn't have to hunt through a price-sorted
                # list.
                "integrations": sorted((a for a in all_addons if a["kind"] != "capacity"), key=lambda a: a["name"].lower()),
                "coupons": storage.list_coupons(),
            },
        )

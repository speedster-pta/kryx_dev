"""BaseView page shells mounted into the sqladmin sidebar. Each of these
just renders a template; the actual data operations for
Automations/Templates go through their own JSON routers
(automations_router.py, templates_router.py), and Campaigns/Account are
likewise thin wrappers - see setup_admin() in admin.py for how these get
registered."""
from fastapi import HTTPException
from sqladmin import BaseView, expose
from starlette.requests import Request

from autosend.admin_scoping import VisibleIfAccessible


def _scoped_unit_ids(request: Request) -> list[int] | None:
    if request.session.get("is_superadmin", False):
        return None
    from autosend.web.auth import resolve_unit_ids

    return resolve_unit_ids(request.session)


def _resolve_number_labels(rows: list[dict]) -> list[dict]:
    """Attaches number_label to each row for display - shared by
    HistoryView and AutomationsView so both stay in sync instead of
    drifting apart."""
    from autosend import storage

    number_labels: dict[int, str] = {}
    for number_id in {r["whatsapp_number_id"] for r in rows if r["whatsapp_number_id"]}:
        number = storage.get_whatsapp_number_by_id(number_id)
        if number:
            number_labels[number_id] = number["label"]
    for row in rows:
        row["number_label"] = number_labels.get(row["whatsapp_number_id"]) or row["whatsapp_number_id"] or "—"
    return rows


def _available_numbers(unit_ids: list[int] | None) -> list[dict]:
    """Every distinct WhatsApp number that has ever sent something in
    scope, for the Number filter dropdown - not limited to whatever's on
    the current page/window, so the dropdown's options stay stable across
    pagination."""
    from autosend import storage

    numbers = []
    for number_id in storage.get_distinct_number_ids(unit_ids=unit_ids):
        number = storage.get_whatsapp_number_by_id(number_id)
        if number:
            numbers.append({"id": number_id, "label": number["label"]})
    numbers.sort(key=lambda n: n["label"])
    return numbers


def _safe_redirect_target(form, default: str) -> str:
    """Lets a caller (e.g. the organisation detail page's module
    checkboxes) send the user back to wherever they came from instead of
    always bouncing to the standalone /modules page - restricted to an
    in-app absolute path so this can never become an open redirect."""
    next_url = form.get("next")
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return default


def _pagination_window(page: int, total_pages: int, radius: int = 2) -> list[int | None]:
    """Builds a compact page-number list for /history's pagination bar -
    first, last, and a small window around the current page, with None
    standing in for an ellipsis - so this doesn't render a link per page
    once history grows into the hundreds of pages."""
    if total_pages <= 1:
        return [1]
    pages = {1, total_pages}
    for p in range(max(1, page - radius), min(total_pages, page + radius) + 1):
        pages.add(p)
    ordered = sorted(pages)
    result: list[int | None] = []
    prev = None
    for p in ordered:
        if prev is not None and p - prev > 1:
            result.append(None)
        result.append(p)
        prev = p
    return result


class AutomationsView(VisibleIfAccessible, BaseView):
    """One page per automation-driving integration module - PCO
    (/automations/pco: Free/Paid Registrations, Form Responses, Serving
    Reminders), SME Metrics (/automations/sme-metrics), and
    Email-to-WhatsApp (/automations/email-wa) - replacing what used to be
    a single combined /automations page with every integration's sub-tabs
    crammed into one nav bar. That got unworkable as more integrations
    shipped - a superadmin in particular always sees every module (see
    web.auth.pco_module_visible/sme_metrics_module_visible/
    email_wa_module_visible), so their tab bar only ever grows. Splitting
    means each integration now gets its own headerbar item (see
    layout.html's use of web.auth.visible_automation_modules: hidden if
    no module is visible to this session, a direct link if exactly one
    is, a dropdown once two or more are). /automations itself now just
    redirects to whichever per-module page(s) apply, for old
    links/bookmarks.

    All three routes render the same template shell (automations.html)
    via _render() below. SME Metrics and Email-to-WhatsApp are both
    "provider registry" modules (see _PROVIDER_MODULES) - structurally
    identical (one tab/section per registered provider/email_type), just
    pointed at two independent provider registries/storage layers/API
    routers, so they share one render path in the template keyed by
    generic `provider_*` context variables rather than each getting
    email_wa-specific ones. PCO has its own fixed set of named sections
    instead of a provider registry, so it stays a separate branch.

    is_accessible/is_visible below are kept for consistency with
    ModulesView/WabaUsageView, but sqladmin never actually calls them for
    a BaseView's own @expose routes (only for its auto-generated menu and
    ModelView's built-in CRUD routes) - this app's hand-rolled layout.html
    nav doesn't call them either. The real enforcement is the explicit
    check at the top of _render() below."""
    name = "Automations"
    icon = "fa-solid fa-robot"
    identity = "automations-page"

    def is_accessible(self, request: Request) -> bool:
        from autosend.web.auth import (
            email_wa_module_visible,
            pco_module_visible,
            sme_metrics_module_visible,
        )

        return (
            pco_module_visible(request)
            or sme_metrics_module_visible(request)
            or email_wa_module_visible(request)
        )

    @expose("/automations", methods=["GET"], identity="automations-page")
    async def redirect_to_module_page(self, request: Request):
        from starlette.responses import RedirectResponse

        from autosend.web.auth import visible_automation_modules

        modules = visible_automation_modules(request)
        if not modules:
            raise HTTPException(status_code=403, detail="No automation module is enabled for this organisation")
        return RedirectResponse(url=modules[0]["url"], status_code=302)

    @expose("/automations/pco", methods=["GET"], identity="automations-pco-page")
    async def pco_page(self, request: Request):
        return await self._render(request, module="pco")

    @expose("/automations/sme-metrics", methods=["GET"], identity="automations-sme-metrics-page")
    async def sme_metrics_page(self, request: Request):
        return await self._render(request, module="sme_metrics")

    @expose("/automations/email-wa", methods=["GET"], identity="automations-email-wa-page")
    async def email_wa_page(self, request: Request):
        return await self._render(request, module="email_wa")

    def _provider_module_config(self, module: str) -> dict:
        """One entry per provider-registry module (see class docstring) -
        the single place that maps a module key to its own provider
        registry/domain setting/API prefix/label, so _render() below
        doesn't need a growing if/elif chain every time another such
        module is added."""
        from autosend.config import settings
        from autosend.web.auth import email_wa_module_visible, sme_metrics_module_visible

        if module == "sme_metrics":
            from autosend.integrations.sme_metrics.providers import PROVIDERS

            return {
                "module_visible": sme_metrics_module_visible,
                "providers": PROVIDERS,
                "domain": settings.email_wa_inbound_domain,
                "api_prefix": "/api/sme-metrics",
                "label": "SME Metrics Automations",
                "not_enabled_detail": "The SME Metrics module is not enabled for this organisation",
            }
        from autosend.integrations.email_wa.providers import PROVIDERS

        return {
            "module_visible": email_wa_module_visible,
            "providers": PROVIDERS,
            "domain": settings.generic_email_wa_inbound_domain,
            "api_prefix": "/api/email-wa",
            "label": "Email-to-WhatsApp Automations",
            "not_enabled_detail": "The Email-to-WhatsApp module is not enabled for this organisation",
        }

    async def _render(self, request: Request, module: str):
        from autosend.integrations.sme_metrics.providers import build_email_type_tabs
        from autosend.web.auth import get_current_web_user, pco_module_visible
        from autosend import storage

        pco_visible = module == "pco"
        provider_config = None if pco_visible else self._provider_module_config(module)

        if pco_visible:
            if not pco_module_visible(request):
                raise HTTPException(status_code=403, detail="The PCO module is not enabled for this organisation")
        elif not provider_config["module_visible"](request):
            raise HTTPException(status_code=403, detail=provider_config["not_enabled_detail"])

        user = get_current_web_user(request)
        unit_ids = _scoped_unit_ids(request)

        pco_subdomain = None
        if pco_visible and user["org_id"] is not None:
            from sqlalchemy import select
            from sqlalchemy.orm import Session

            from autosend.admin_models import PCOOrganizationSettings, engine

            with Session(engine) as session:
                pco_settings = session.execute(
                    select(PCOOrganizationSettings).where(PCOOrganizationSettings.org_id == user["org_id"])
                ).scalar_one_or_none()
            pco_subdomain = pco_settings.pco_subdomain if pco_settings else None

        # Last 50 - a recent-activity snapshot, not the full history (see
        # /history for that). Paginated client-side in groups of 10 in
        # automations.html, since 50 rows is small enough that a second
        # DB round-trip per page would be unnecessary overhead. Shown on
        # every per-module page unfiltered by module, same as it always
        # was on the old combined page - it's cross-integration activity,
        # not something to split per integration.
        automation_history = _resolve_number_labels(
            storage.get_recent_sends(limit=50, unit_ids=unit_ids)
        )
        available_numbers = _available_numbers(unit_ids)

        # Provider/email_type registry is code, not DB data (see
        # integrations/sme_metrics/providers/__init__.py and
        # integrations/email_wa/providers/__init__.py) - passed here so
        # automations.html can render one sub-tab per registered
        # email_type server-side (Jinja) and give its JS the per-type
        # variable vocabulary inline, the same way REGISTRATION_VARIABLES/
        # FORM_VARIABLES/SERVING_VARIABLES are baked-in JS constants for
        # the PCO-driven sections - fetching this same data over the
        # module's own /api/.../providers endpoint as well would just be
        # a redundant round trip for content that never changes without a
        # deploy.
        provider_module_providers = [
            {
                "key": provider.PROVIDER_KEY,
                "label": provider.LABEL,
                "email_types": build_email_type_tabs(provider),
            }
            for provider in provider_config["providers"].values()
        ] if provider_config else []

        return await self.templates.TemplateResponse(
            request,
            "automations.html",
            {
                "user": user,
                "automation_history": automation_history,
                "available_numbers": available_numbers,
                # Named *_section_visible, not pco_visible/provider_visible -
                # "pco_visible" is already taken by a Jinja global of the
                # same name (see admin.py's setup_admin) that layout.html
                # calls as a function (pco_visible(request)) for its own
                # nav links; a same-named context variable here would
                # shadow it for this template's entire render (Jinja
                # globals are just default context, easily overridden),
                # breaking layout.html with "'bool' object is not callable".
                "pco_section_visible": pco_visible,
                "provider_section_visible": provider_config is not None,
                "provider_module_providers": provider_module_providers,
                "provider_module_domain": provider_config["domain"] if provider_config else None,
                "provider_api_prefix": provider_config["api_prefix"] if provider_config else None,
                "pco_subdomain": pco_subdomain,
                "page_title": "Planning Center Automations" if pco_visible else provider_config["label"],
            },
        )


class TemplatesView(BaseView):
    """WhatsApp Templates builder page. Same shell pattern as
    AutomationsView/CampaignsView - all the actual create/list/delete
    logic goes through web/templates_router.py's JSON endpoints, which
    talk to Meta directly and keep no local record (Meta-only, no DB
    table for this one, unlike Automations)."""
    name = "Templates"
    icon = "fa-solid fa-file-lines"
    identity = "templates-page"

    @expose("/templates", methods=["GET"], identity="templates-page")
    async def page(self, request: Request):
        from autosend.web.auth import get_current_web_user, ical_module_visible, stitch_module_visible
        from autosend.integrations.stitch import STITCH_BASE_URL

        user = get_current_web_user(request)
        # Presets for the button builder's "quick fill" dropdown - each
        # entry only appears when the org actually has the matching
        # automation provisioned (both iCal and Stitch are real per-org
        # module toggles - see storage.MODULE_ICAL/MODULE_STITCH). Base
        # URL is computed from the current request rather than hardcoded,
        # so dev.kryx.co.za vs kryx.co.za resolves correctly without a
        # config setting.
        button_presets = []
        if stitch_module_visible(request):
            button_presets.append({
                "key": "stitch",
                "label": "Stitch payment link",
                "base_url": STITCH_BASE_URL,
                # Opaque suffix from a real StitchClient.create_payment_link()
                # call (integrations/stitch.py) - no longer the old locally-
                # built "rands/reference" shape.
                "example": "pay_3f8e2a1c9b7d",
            })
        if ical_module_visible(request):
            button_presets.append({
                "key": "ical",
                "label": "Calendar invite (iCal)",
                "base_url": f"{str(request.base_url).rstrip('/')}/ical/",
                "example": "3f8e2a1c-9b7d-4e6a-9c3f-1a2b3c4d5e6f.ics",
            })
        return await self.templates.TemplateResponse(
            request, "templates.html", {"user": user, "button_presets": button_presets},
        )


class WabaUsageView(VisibleIfAccessible, BaseView):
    """Read-only usage report: template sends per day per WABA pool, so
    you can see which units/numbers are actually using the
    platform and spot unusual volume. Pulls straight from message_log -
    the same table whatsapp_limits.py already writes to for 24h-limit
    gating - via storage.daily_message_counts()/waba_label_map(). No new
    writes, no change to the limiter's behaviour. Same shell pattern as
    CampaignsView/AutomationsView above."""
    name = "Usage"
    icon = "fa-solid fa-chart-column"
    identity = "waba-usage-page"

    def is_accessible(self, request: Request) -> bool:
        # Usage spans every unit's WABA, same reasoning as
        # UserAdmin/UnitAdmin restricting to superadmins -
        # a scoped user shouldn't see other units' volumes.
        return request.session.get("is_superadmin", False)

    @expose("/usage", methods=["GET"], identity="waba-usage-page")
    async def page(self, request: Request):
        from autosend.web.auth import get_current_web_user
        from autosend import storage

        if not request.session.get("is_superadmin", False):
            raise HTTPException(status_code=403, detail="Superadmin access required")

        user = get_current_web_user(request)

        days = request.query_params.get("days", "30")
        try:
            days = max(1, min(int(days), 365))
        except ValueError:
            days = 30

        rows = storage.daily_message_counts(days=days)
        labels = storage.waba_label_map()

        for row in rows:
            row["label"] = labels.get(row["limit_key"], row["limit_key"])

        totals: dict[str, int] = {}
        for row in rows:
            totals[row["label"]] = totals.get(row["label"], 0) + row["message_count"]
        totals_sorted = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)

        return await self.templates.TemplateResponse(
            request,
            "waba_usage.html",
            {
                "user": user,
                "rows": rows,
                "totals": totals_sorted,
                "days": days,
            },
        )


class ModulesView(BaseView):
    """Two-tier module control: superadmin grants which add-ons an org is
    entitled to (payment tier/agreement - storage.grant()/revoke()), then
    either a superadmin or that org's own org-admin users can flip a
    granted module on/off (storage.enable()/disable()). enable() itself
    refuses anything not granted, so the "not granted" case is defended
    at both layers, not just by hiding the checkbox here.

    This page itself is no longer linked from the nav - the checkboxes
    now live on admin_org_pages.OrganisationsView's per-org page (and its
    own-org equivalent, /organisation), which just POST to the two routes
    below with a `next` field (see _safe_redirect_target) so the toggle
    lands back on whichever org page the caller came from instead of here.

    Same BaseView shell pattern as WabaUsageView above."""
    name = "Modules"
    icon = "fa-solid fa-puzzle-piece"
    identity = "modules-page"

    def is_accessible(self, request: Request) -> bool:
        return request.session.get("is_superadmin", False) or request.session.get("is_org_admin", False)

    def is_visible(self, request: Request) -> bool:
        return self.is_accessible(request)

    @expose("/modules", methods=["GET"], identity="modules-page")
    async def page(self, request: Request):
        from autosend.web.auth import get_current_web_user
        from autosend import storage

        user = get_current_web_user(request)
        is_superadmin = request.session.get("is_superadmin", False)

        if is_superadmin:
            orgs = storage.list_organisations(active_only=False)
        else:
            org = storage.get_organisation(request.session.get("org_id"))
            orgs = [org] if org else []

        rows = []
        for org in orgs:
            granted = set(storage.granted_modules_for_org(org.id))
            enabled = set(storage.enabled_modules_for_org(org.id))
            rows.append({
                "org": org,
                "modules": [
                    {
                        "key": key,
                        "label": label,
                        "granted": key in granted,
                        "enabled": key in enabled,
                    }
                    for key, label in storage.AVAILABLE_MODULES
                ],
            })

        return await self.templates.TemplateResponse(
            request,
            "modules.html",
            {"user": user, "rows": rows, "is_superadmin": is_superadmin},
        )

    @expose("/modules/grant-toggle", methods=["POST"], identity="modules-grant-toggle")
    async def grant_toggle(self, request: Request):
        from starlette.responses import RedirectResponse

        from autosend import storage

        if not request.session.get("is_superadmin", False):
            raise HTTPException(status_code=403, detail="Superadmin only")

        form = await request.form()
        org_id = int(form["org_id"])
        module_key = form["module_key"]
        if form.get("action") == "grant":
            storage.grant(org_id, module_key)
        else:
            storage.revoke(org_id, module_key)
            # revoke() also disables the module at the storage layer if it
            # was enabled - mirror that immediately in the scheduler too,
            # same as an explicit disable via toggle() below, so a revoked
            # org's serving-reminder jobs don't linger until restart.
            if module_key == storage.MODULE_PCO:
                from autosend.scheduler import cancel_org_serving_rule_jobs

                cancel_org_serving_rule_jobs(org_id)
        return RedirectResponse(url=_safe_redirect_target(form, "/modules"), status_code=303)

    @expose("/modules/toggle", methods=["POST"], identity="modules-toggle")
    async def toggle(self, request: Request):
        from starlette.responses import RedirectResponse

        from autosend import storage

        form = await request.form()
        module_key = form["module_key"]

        if request.session.get("is_superadmin", False):
            org_id = int(form["org_id"])
        elif request.session.get("is_org_admin", False):
            # Never trust a posted org_id from a non-superadmin - an org
            # admin can only ever toggle their own org's modules.
            org_id = request.session.get("org_id")
        else:
            raise HTTPException(status_code=403, detail="Not permitted")

        try:
            if form.get("action") == "enable":
                storage.enable(org_id, module_key)
                # Immediate effect, not just at next restart - see
                # scheduler.reschedule_org_serving_rules's docstring.
                if module_key == storage.MODULE_PCO:
                    from autosend.scheduler import reschedule_org_serving_rules

                    reschedule_org_serving_rules(org_id)
            else:
                storage.disable(org_id, module_key)
                if module_key == storage.MODULE_PCO:
                    from autosend.scheduler import cancel_org_serving_rule_jobs

                    cancel_org_serving_rule_jobs(org_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        return RedirectResponse(url=_safe_redirect_target(form, "/modules"), status_code=303)


class HistoryView(BaseView):
    """Full, paginated history of every transactional send (registration
    poller + form webhook) - the automation-side equivalent of the
    bulk-campaign history page. Pulls from storage.get_recent_sends()
    (send_log table), an append-only log kept separate from
    processed_registrations/processed_form_submissions in dedup.py, which
    overwrite on retry and exist for idempotency, not reporting.

    Unlike WabaUsageView (superadmin-only, spans every unit's
    volume), this is scoped per-unit the same way ScopedModelView
    scopes CRUD views: a non-superadmin user only sees sends for the
    unit(s) in their session, since this is operational data about
    real people's messages, not an aggregate volume figure.

    Server-side paginated (PAGE_SIZE per page) rather than a bounded
    "last N" list, since this is meant to be the full available history -
    unlike AutomationsView's condensed recent-activity card, which fetches
    a bounded, client-side-paginated window instead."""
    name = "History"
    icon = "fa-solid fa-clock-rotate-left"
    identity = "history-page"

    PAGE_SIZE = 20
    DAY_RANGES = (7, 30, 90)
    SORT_COLUMNS = ("time", "source", "recipient", "number")

    @expose("/history", methods=["GET"], identity="history-page")
    async def page(self, request: Request):
        import math

        from autosend.web.auth import get_current_web_user
        from autosend import storage

        user = get_current_web_user(request)
        unit_ids = _scoped_unit_ids(request)

        try:
            page = max(1, int(request.query_params.get("page", "1")))
        except ValueError:
            page = 1

        number_id_param = request.query_params.get("number_id")
        whatsapp_number_id = int(number_id_param) if number_id_param and number_id_param.isdigit() else None

        try:
            days = int(request.query_params.get("days", "30"))
        except ValueError:
            days = 30
        if days not in self.DAY_RANGES:
            days = 30

        sort = request.query_params.get("sort", "time")
        if sort not in self.SORT_COLUMNS:
            sort = "time"
        # Default direction depends on the column: time defaults to
        # newest-first (desc), matching the page's pre-sorting behaviour;
        # every other column defaults to asc on first click, the more
        # intuitive direction for text/name-like columns.
        default_dir = "desc" if sort == "time" else "asc"
        direction = request.query_params.get("dir", default_dir)
        if direction not in ("asc", "desc"):
            direction = default_dir

        total = storage.get_send_count(unit_ids=unit_ids, whatsapp_number_id=whatsapp_number_id)
        total_pages = max(1, math.ceil(total / self.PAGE_SIZE))
        page = min(page, total_pages)
        offset = (page - 1) * self.PAGE_SIZE

        rows = _resolve_number_labels(storage.get_recent_sends(
            limit=self.PAGE_SIZE,
            offset=offset,
            unit_ids=unit_ids,
            whatsapp_number_id=whatsapp_number_id,
            sort=sort,
            direction=direction,
        ))
        available_numbers = _available_numbers(unit_ids)

        status_summary = storage.get_send_status_summary(days=days, unit_ids=unit_ids)
        summary_total = sum(status_summary.values())

        return await self.templates.TemplateResponse(
            request,
            "history.html",
            {
                "user": user,
                "rows": rows,
                "available_numbers": available_numbers,
                "page": page,
                "total_pages": total_pages,
                "page_numbers": _pagination_window(page, total_pages),
                "total": total,
                "selected_number_id": whatsapp_number_id,
                "days": days,
                "day_ranges": self.DAY_RANGES,
                "status_summary": status_summary,
                "summary_total": summary_total,
                "sort": sort,
                "dir": direction,
            },
        )


class CampaignsView(BaseView):
    """Puts the bulk-campaign dashboard in the SQLAdmin sidebar, embedded
    in the same layout (sidebar, no separate top bar) as every other admin
    page - not just linked from it. dashboard.html now lives inside
    web/sqladmin_theme/ (the templates_dir passed to Admin(...) below) and
    extends "sqladmin/layout.html" directly, which is why this uses
    self.templates (SQLAdmin's own Jinja2 environment, already wired up to
    resolve that) rather than a separate Jinja2Templates instance - a
    template extending sqladmin's layout has to be rendered through
    sqladmin's own loader to find it.
    sqladmin's own @expose already wraps this in login_required, so by the
    time this method runs the session is guaranteed to have user_id etc. -
    get_current_web_user() just reads it back out, it won't redirect."""
    name = "Campaigns"
    icon = "fa-solid fa-comment-dots"
    identity = "campaigns-page"

    # NOTE: identity="campaigns-page" is passed explicitly here because
    # SQLAdmin derives each exposed route's *internal* identity from the
    # method name when none is given (func.__name__, i.e. "page" for every
    # BaseView in this file that names its handler `page`). Two BaseViews
    # both named "page" therefore silently registered the same route name
    # ("view-page") and url_for() resolved to whichever one - Campaigns -
    # was added to the Admin first, which is why the Account sidebar link
    # used to land on the campaigns dashboard instead of the account page.
    @expose("/campaigns", methods=["GET"], identity="campaigns-page")
    async def page(self, request: Request):
        from autosend.web.auth import get_current_web_user
        user = get_current_web_user(request)
        return await self.templates.TemplateResponse(request, "dashboard.html", {"user": user})


class OnboardingView(BaseView):
    """The Embedded Signup unit picker. All the OAuth mechanics
    (the redirect to Meta, the /oauth/meta/whatsapp callback) live in
    web/onboarding_router.py - this just renders the picker form, same
    shell pattern as every other BaseView here. sqladmin's own @expose
    already wraps this in login_required, matching CampaignsView's note
    about get_current_web_user() just reading the session back out."""
    name = "Add Number"
    icon = "fa-brands fa-whatsapp"
    identity = "onboarding-page"

    @expose("/add-number", methods=["GET"], identity="onboarding-page")
    async def page(self, request: Request):
        from autosend.web.auth import get_current_web_user
        from autosend import storage

        user = get_current_web_user(request)
        all_units = storage.get_active_units()
        if user["is_superadmin"]:
            units = all_units
        else:
            allowed = set(user["unit_ids"])
            units = [c for c in all_units if c["id"] in allowed]

        return await self.templates.TemplateResponse(
            request, "add_number.html", {"user": user, "units": units},
        )


class AccountView(BaseView):
    """Same idea as CampaignsView, for the self-service password page."""
    name = "Account"
    icon = "fa-solid fa-user"
    identity = "account-page"

    @expose("/account", methods=["GET"], identity="account-page")
    async def page(self, request: Request):
        from autosend.web.auth import get_current_web_user
        user = get_current_web_user(request)
        return await self.templates.TemplateResponse(request, "account.html", {"user": user})


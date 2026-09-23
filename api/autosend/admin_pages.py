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
    """None means "no filter" (superadmin only) - otherwise the list of
    unit ids this session may see, same resolve_unit_ids() choke point
    ScopedModelView itself is built on.

    NOT interchangeable with admin_org_pages.py's org-scoped
    "_visible_unit_ids" helpers: those additionally unfilter for
    org-admins because they're always paired with an explicit
    Unit.org_id == org_id filter on the same query. This one is used
    standalone (no accompanying org filter), so unfiltering for an
    org-admin here would leak every other org's units/numbers too."""
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

    Kryx Bookings (/automations/kryx-bookings, kryx_bookings_page below)
    is a fourth, independent branch: its shape is one automation per
    (unit, booking status) - storage.BOOKING_STATUSES, a fixed set like
    PCO's sections, but with no inbound-email/PCO-webhook trigger and no
    provider registry - so it doesn't fit _render()'s pco_visible/
    provider_config split either. It gets its own @expose route, its own
    template (kryx_bookings_automations.html), and its own JSON API
    (web/kryx_bookings_router.py) rather than being forced into
    automations.html - see KryxBookingsSettingsView (admin_org_pages.py)
    for the paired Settings-page half of this split (API key management
    only).

    Voice Transcription (/automations/voice-transcription,
    voice_transcription_page below) is a fifth, independent branch: unlike
    every module above, it has no automation history/number-filter
    dropdown of its own (it's not itself a triggered send - it forwards
    voice notes on a number and replies with a transcription), just the
    number-assignment + language-whitelist form, rendered from its own
    pre-existing template (voice_transcription_settings.html). It used to
    be its own standalone nav item (VoiceTranscriptionSettingsView) before
    moving here so org admins/users find every automation module - PCO,
    SME Metrics, Email-to-WhatsApp, Kryx Bookings, Voice Transcription -
    in one place; the superadmin-only raw CRUD screens over the
    underlying settings tables (VoiceTranscriptionSettingsAdmin/
    VoiceTranscriptionConfusableSpellingAdmin, admin_views.py) are
    unrelated and still live under the Admin dropdown.

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
            kryx_bookings_module_visible,
            pco_module_visible,
            sme_metrics_module_visible,
            voice_transcription_module_visible,
        )

        return (
            pco_module_visible(request)
            or sme_metrics_module_visible(request)
            or email_wa_module_visible(request)
            or kryx_bookings_module_visible(request)
            or voice_transcription_module_visible(request)
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

    @expose("/automations/kryx-bookings", methods=["GET"], identity="automations-kryx-bookings-page")
    async def kryx_bookings_page(self, request: Request):
        from autosend.web.auth import get_current_web_user, kryx_bookings_module_visible
        from autosend import storage

        if not kryx_bookings_module_visible(request):
            raise HTTPException(status_code=403, detail="The Kryx Bookings module is not enabled for this organisation")

        unit_ids = _scoped_unit_ids(request)
        automation_history = _resolve_number_labels(
            storage.get_recent_sends(limit=50, unit_ids=unit_ids)
        )
        available_numbers = _available_numbers(unit_ids)

        return await self.templates.TemplateResponse(
            request,
            "kryx_bookings_automations.html",
            {
                "user": get_current_web_user(request),
                "automation_history": automation_history,
                "available_numbers": available_numbers,
                "booking_statuses": storage.BOOKING_STATUSES,
                "status_labels": storage.KRYX_BOOKINGS_STATUS_LABELS,
                "page_title": "Kryx Bookings Automations",
            },
        )

    @expose("/automations/voice-transcription", methods=["GET"], identity="automations-voice-transcription-page")
    async def voice_transcription_page(self, request: Request):
        # A fifth, independent branch alongside Kryx Bookings above: no
        # automation history/number-filter dropdown (voice transcription
        # isn't itself a triggered send), just the number-assignment +
        # language-whitelist form. Data operations go through
        # web/voice_transcription_router.py, which re-checks the module +
        # number scope itself - this only gates whether the page shell
        # renders at all, same split as every other BaseView page here.
        from autosend.web.auth import get_current_web_user, voice_transcription_module_visible

        if not voice_transcription_module_visible(request):
            raise HTTPException(status_code=403, detail="The Voice Transcription module is not enabled for this organisation")

        return await self.templates.TemplateResponse(
            request, "voice_transcription_settings.html", {"user": get_current_web_user(request)},
        )

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


class _AIAssistantPageBase(VisibleIfAccessible, BaseView):
    """Shared is_accessible for every AI Assistant module page - open to
    any logged-in staff whose org has the module enabled (superadmin
    always sees it, same bypass as every other module-gated page), not
    just org-admins. Actual data operations go through
    web/knowledge_router.py / web/ai_settings_router.py, which re-check
    the module + unit/org scope themselves - this only gates whether the
    page shell renders at all."""

    def is_accessible(self, request: Request) -> bool:
        from autosend.web.auth import ai_assistant_module_visible
        return ai_assistant_module_visible(request)


class KnowledgeBaseView(_AIAssistantPageBase):
    name = "Knowledge Base"
    icon = "fa-solid fa-book"
    identity = "knowledge-base-page"

    @expose("/knowledge-base", methods=["GET"], identity="knowledge-base-page")
    async def page(self, request: Request):
        from autosend.web.auth import get_current_web_user
        user = get_current_web_user(request)
        return await self.templates.TemplateResponse(request, "knowledge_base.html", {"user": user})


class AISettingsView(_AIAssistantPageBase):
    name = "AI Settings"
    icon = "fa-solid fa-robot"
    identity = "ai-settings-page"

    @expose("/ai-settings", methods=["GET"], identity="ai-settings-page")
    async def page(self, request: Request):
        from autosend.web.auth import get_current_web_user
        user = get_current_web_user(request)
        return await self.templates.TemplateResponse(request, "ai_settings.html", {"user": user})


class AutoReplyRulesView(_AIAssistantPageBase):
    name = "Auto-Reply Rules"
    icon = "fa-solid fa-comment-dots"
    identity = "auto-reply-rules-page"

    @expose("/auto-reply-rules", methods=["GET"], identity="auto-reply-rules-page")
    async def page(self, request: Request):
        from autosend.web.auth import get_current_web_user
        user = get_current_web_user(request)
        return await self.templates.TemplateResponse(request, "auto_reply_rules.html", {"user": user})


class AIPlaygroundView(_AIAssistantPageBase):
    name = "AI Playground"
    icon = "fa-solid fa-flask"
    identity = "ai-playground-page"

    @expose("/ai-playground", methods=["GET"], identity="ai-playground-page")
    async def page(self, request: Request):
        from autosend.web.auth import get_current_web_user
        from autosend import storage
        from autosend.services import model_catalog
        user = get_current_web_user(request)
        # The model picker is superadmin-only (see ai_playground.html), so
        # only hit the Anthropic Models API for them. Defaults to whatever
        # the AI Replies tab has saved, so the playground starts out
        # matching the live auto-reply.
        model_choices, default_model = [], None
        if (user and user.get("is_superadmin")) or request.session.get("is_superadmin"):
            default_model = (storage.get_ai_credentials() or {}).get("model")
            model_choices = await model_catalog.claude_models(default_model)
        return await self.templates.TemplateResponse(
            request, "ai_playground.html",
            {"user": user, "model_choices": model_choices, "default_model": default_model},
        )


class WabaUsageView(VisibleIfAccessible, BaseView):
    """Read-only usage report: real sent-message volume per unit/number, so
    you can see which units/numbers are actually using the platform and
    spot unusual volume. Pulls from storage.send_totals_by_number()/
    daily_send_counts() - a UNION across every table that represents an
    actually-sent outbound message (bulk campaigns, PCO automations, Inbox
    staff replies, AI auto-replies), not message_log (storage/limits.py),
    which only exists to gate the 24h WABA messaging limit and therefore
    only ever sees business-initiated template sends. No new writes, no
    change to the limiter's behaviour. Same shell pattern as
    CampaignsView/AutomationsView above.

    Server-side paginated daily breakdown (PAGE_SIZE per page), same
    page/offset convention as HistoryView - this can grow large across
    every unit/day combination, unlike the totals tiles above it which
    are always one row per number."""
    name = "Usage"
    icon = "fa-solid fa-chart-column"
    identity = "waba-usage-page"

    PAGE_SIZE = 10

    def is_accessible(self, request: Request) -> bool:
        # Usage spans every unit's WABA, same reasoning as
        # UserAdmin/UnitAdmin restricting to superadmins -
        # a scoped user shouldn't see other units' volumes.
        return request.session.get("is_superadmin", False)

    @expose("/usage", methods=["GET"], identity="waba-usage-page")
    async def page(self, request: Request):
        import math

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

        try:
            page = max(1, int(request.query_params.get("page", "1")))
        except ValueError:
            page = 1

        number_labels = storage.number_label_map()
        totals = storage.send_totals_by_number(days=days)
        totals_sorted = [
            (number_labels.get(row["whatsapp_number_id"], f"Number #{row['whatsapp_number_id']}"), row["message_count"])
            for row in totals
        ]

        total_groups = storage.daily_send_group_count(days=days)
        total_pages = max(1, math.ceil(total_groups / self.PAGE_SIZE))
        page = min(page, total_pages)
        offset = (page - 1) * self.PAGE_SIZE

        rows = storage.daily_send_counts(days=days, limit=self.PAGE_SIZE, offset=offset)
        unit_labels = storage.unit_label_map()
        for row in rows:
            unit_id = row["unit_id"]
            if unit_id is None:
                # send_log.unit_id is nullable - org-wide sends (e.g. Kryx
                # Bookings, which is org- not unit-scoped) record no unit.
                row["label"] = "Unassigned"
            else:
                row["label"] = unit_labels.get(unit_id, f"Unit #{unit_id}")

        # AI token usage: ingestion and AI auto-reply are reported in
        # separate cards (not one merged table) because ingestion
        # (knowledge-base scrape/PDF upload) and auto-reply generation are
        # independently configurable models with their own pricing, so a
        # combined total wouldn't mean anything (see
        # services/knowledge_ingest.py / services/ai_reply.py,
        # ai_ingestion_log / ai_reply_log tables).
        ingestion_usage = storage.ingestion_token_usage_by_org(days=days)
        reply_usage = storage.reply_token_usage_by_org(days=days)
        keyword_usage = storage.keyword_reply_counts_by_org(days=days)
        # Voice Transcription's own Claude clean-up call (separate model/
        # prompt from AI Assistant's reply_usage above) and ElevenLabs'
        # audio-duration-based usage (only populated when
        # transcription_provider is "elevenlabs" - see
        # services/audio_transcription.py) get their own cards, same
        # "independently configurable model/cost, don't blend totals"
        # reasoning as the cards above.
        transcription_cleanup_usage = storage.voice_transcription_token_usage_by_org(days=days)
        elevenlabs_usage = storage.elevenlabs_usage_by_org(days=days)
        for row in ingestion_usage + reply_usage + keyword_usage + transcription_cleanup_usage + elevenlabs_usage:
            row["org_name"] = row["org_name"] or "Unknown Organisation"

        # AI vs. keyword split of auto-replies actually sent - deliberately
        # separate from `totals`/`rows` above, which track usage against
        # the WABA 24h quota (a different question: AI/keyword replies
        # never touch that quota at all, see whatsapp.py's send_text()).
        reply_counts = storage.reply_counts_by_category(days=days)
        category_totals = [
            ("AI Auto-Reply", reply_counts["ai_auto_reply"]),
            ("Keyword Auto-Reply", reply_counts["keyword_auto_reply"]),
        ]

        # Voice Transcription: how many voice notes each assigned number
        # processed (claimed + attempted a clean-up/reply for), and how
        # many actually got a reply delivered - deliberately per-number
        # (like `totals` above), not per-org, since the module is
        # assigned to one specific number at a time.
        voice_transcription_usage = storage.voice_transcription_counts_by_number(days=days)
        for row in voice_transcription_usage:
            row["label"] = number_labels.get(row["whatsapp_number_id"], f"Number #{row['whatsapp_number_id']}")

        return await self.templates.TemplateResponse(
            request,
            "waba_usage.html",
            {
                "user": user,
                "rows": rows,
                "totals": totals_sorted,
                "days": days,
                "ingestion_usage": ingestion_usage,
                "reply_usage": reply_usage,
                "keyword_usage": keyword_usage,
                "transcription_cleanup_usage": transcription_cleanup_usage,
                "elevenlabs_usage": elevenlabs_usage,
                "category_totals": category_totals,
                "voice_transcription_usage": voice_transcription_usage,
                "page": page,
                "total_pages": total_pages,
                "page_numbers": _pagination_window(page, total_pages),
                "total": total_groups,
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


class MetaSettingsView(VisibleIfAccessible, BaseView):
    """Superadmin-only singleton settings page - platform-wide Meta app
    credentials for WhatsApp Embedded Signup and webhook signature
    verification. Replaces the retired MetaPlatformSettingsAdmin, and also
    absorbs the retired MetaAppAdmin (extra, manually-added Meta Apps whose
    webhook signatures should also be accepted - see schema.py's meta_apps
    table docstring for the Tech Provider/BSP scenario this covers) as a
    card list rendered below the main credentials form on the same page,
    since both concern "which Meta app(s) this deployment trusts."

    Writes go through the SQLAlchemy ORM models (admin_models.MetaPlatformSettings/
    MetaApp), not a raw storage/*.py helper, because app_secret/
    webhook_verify_token are EncryptedString columns - the ORM's TypeDecorator
    is what transparently Fernet-encrypts them on write (storage.get_meta_platform_settings()
    only covers the decrypting read path application code needs).

    Method ordering matters, same reason as elsewhere in this file:
    app_new_page/app_create are defined LAST so their static route
    ("/meta-apps/new") registers before app_detail_page's dynamic
    "/meta-apps/{meta_app_id}" pattern and wins the match."""
    name = "Meta Platform Settings"
    icon = "fa-solid fa-key"
    identity = "meta-settings-page"

    def is_accessible(self, request: Request) -> bool:
        return request.session.get("is_superadmin", False)

    @staticmethod
    def _require_superadmin(request: Request) -> None:
        # is_accessible above only governs nav visibility/the auto-generated
        # CRUD routes SQLAdmin builds for a ModelView - a BaseView's own
        # @expose routes are never auto-guarded by it (see the sqladmin pin
        # gotcha in CLAUDE.md), so every route below re-checks explicitly.
        if not request.session.get("is_superadmin", False):
            raise HTTPException(status_code=403, detail="Superadmin only")

    @expose("/meta-settings", methods=["GET"], identity="meta-settings-page-get")
    async def page(self, request: Request):
        self._require_superadmin(request)
        from sqlalchemy import select
        from sqlalchemy.orm import Session

        from autosend.admin_models import engine, MetaApp, MetaPlatformSettings

        with Session(engine) as session:
            settings = session.execute(select(MetaPlatformSettings)).scalars().first()
            apps = session.execute(select(MetaApp).order_by(MetaApp.app_id)).scalars().all()
        return await self.templates.TemplateResponse(request, "meta_settings.html", {"settings": settings, "apps": apps})

    @expose("/meta-settings/save", methods=["POST"], identity="meta-settings-save")
    async def save(self, request: Request):
        self._require_superadmin(request)
        from datetime import datetime, timezone

        from sqlalchemy import select
        from sqlalchemy.orm import Session
        from starlette.responses import RedirectResponse

        from autosend.admin_models import engine, MetaApp, MetaPlatformSettings

        form = await request.form()
        app_id = (form.get("app_id") or "").strip()
        app_secret = form.get("app_secret") or None
        config_id = (form.get("config_id") or "").strip()
        webhook_verify_token = form.get("webhook_verify_token") or None
        with Session(engine) as session:
            settings = session.execute(select(MetaPlatformSettings)).scalars().first()
            if settings is None:
                if not app_id or not app_secret or not config_id:
                    apps = session.execute(select(MetaApp).order_by(MetaApp.app_id)).scalars().all()
                    return await self.templates.TemplateResponse(
                        request, "meta_settings.html",
                        {"settings": None, "apps": apps, "error": "App ID, App Secret and Config ID are all required."},
                        status_code=400,
                    )
                settings = MetaPlatformSettings(
                    app_id=app_id, app_secret=app_secret, config_id=config_id,
                    webhook_verify_token=webhook_verify_token,
                    created_at=datetime.now(timezone.utc).isoformat(),
                )
                session.add(settings)
            else:
                if app_id:
                    settings.app_id = app_id
                if app_secret:
                    settings.app_secret = app_secret
                if config_id:
                    settings.config_id = config_id
                if webhook_verify_token:
                    settings.webhook_verify_token = webhook_verify_token
            session.commit()
        return RedirectResponse(url="/meta-settings", status_code=303)

    @expose("/meta-apps/{meta_app_id}", methods=["GET"], identity="meta-apps-detail-page")
    async def app_detail_page(self, request: Request):
        self._require_superadmin(request)
        from sqlalchemy.orm import Session

        from autosend.admin_models import engine, MetaApp

        meta_app_pk = int(request.path_params["meta_app_id"])
        with Session(engine) as session:
            app = session.get(MetaApp, meta_app_pk)
            if app is None:
                raise HTTPException(status_code=404)
        return await self.templates.TemplateResponse(request, "meta_app_detail.html", {"app": app})

    @expose("/meta-apps/{meta_app_id}/update", methods=["POST"], identity="meta-apps-update")
    async def app_update(self, request: Request):
        self._require_superadmin(request)
        from sqlalchemy.orm import Session
        from starlette.responses import RedirectResponse

        from autosend.admin_models import engine, MetaApp

        meta_app_pk = int(request.path_params["meta_app_id"])
        form = await request.form()
        with Session(engine) as session:
            app = session.get(MetaApp, meta_app_pk)
            if app is None:
                raise HTTPException(status_code=404)
            app_id = (form.get("app_id") or "").strip()
            if app_id:
                app.app_id = app_id
            app_secret = form.get("app_secret") or None
            if app_secret:
                app.app_secret = app_secret
            app.label = (form.get("label") or "").strip() or None
            session.commit()
        return RedirectResponse(url=f"/meta-apps/{meta_app_pk}", status_code=303)

    # Defined last (registers first - see class docstring): "/meta-apps/new"
    # is a static path that would otherwise be shadowed by
    # app_detail_page's "/meta-apps/{meta_app_id}" pattern.
    @expose("/meta-apps/new", methods=["GET"], identity="meta-apps-new-page")
    async def app_new_page(self, request: Request):
        self._require_superadmin(request)
        return await self.templates.TemplateResponse(request, "meta_app_new.html", {})

    @expose("/meta-apps", methods=["POST"], identity="meta-apps-create")
    async def app_create(self, request: Request):
        self._require_superadmin(request)
        from datetime import datetime, timezone

        from sqlalchemy.orm import Session
        from starlette.responses import RedirectResponse

        from autosend.admin_models import engine, MetaApp

        form = await request.form()
        app_id = (form.get("app_id") or "").strip()
        app_secret = form.get("app_secret") or ""
        label = (form.get("label") or "").strip() or None
        if not app_id or not app_secret:
            return await self.templates.TemplateResponse(
                request, "meta_app_new.html", {"error": "App ID and app secret are both required."}, status_code=400,
            )
        with Session(engine) as session:
            app = MetaApp(
                app_id=app_id, app_secret=app_secret, label=label,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            session.add(app)
            session.commit()
            new_id = app.id
        return RedirectResponse(url=f"/meta-apps/{new_id}", status_code=303)


class PlatformEmailSettingsView(VisibleIfAccessible, BaseView):
    """Superadmin-only singleton settings page - platform-wide outbound
    SMTP credentials (currently Mailtrap), used for transactional email
    (signup email verification). Replaces the retired
    PlatformEmailSettingsAdmin ModelView with a hand-rolled page, same
    shape/styling as MetaSettingsView above (single form, no card list
    needed here since there's only ever the one settings row).

    Writes go through the SQLAlchemy ORM model (admin_models.PlatformEmailSettings),
    not a raw storage/*.py helper, because smtp_password is an
    EncryptedString column - the ORM's TypeDecorator is what transparently
    Fernet-encrypts it on write (storage.get_platform_email_settings()
    only covers the decrypting read path integrations/mailer.py needs)."""
    name = "Platform Email Settings"
    icon = "fa-solid fa-envelope"
    identity = "platform-email-settings-page"

    def is_accessible(self, request: Request) -> bool:
        return request.session.get("is_superadmin", False)

    @staticmethod
    def _require_superadmin(request: Request) -> None:
        # Same reason as MetaSettingsView._require_superadmin above -
        # is_accessible alone doesn't guard a BaseView's own @expose
        # routes (see the sqladmin pin gotcha in CLAUDE.md).
        if not request.session.get("is_superadmin", False):
            raise HTTPException(status_code=403, detail="Superadmin only")

    @expose("/platform-email-settings", methods=["GET"], identity="platform-email-settings-page-get")
    async def page(self, request: Request):
        self._require_superadmin(request)
        from sqlalchemy import select
        from sqlalchemy.orm import Session

        from autosend.admin_models import engine, PlatformEmailSettings

        with Session(engine) as session:
            settings = session.execute(select(PlatformEmailSettings)).scalars().first()
        return await self.templates.TemplateResponse(request, "platform_email_settings.html", {"settings": settings})

    @expose("/platform-email-settings/save", methods=["POST"], identity="platform-email-settings-save")
    async def save(self, request: Request):
        self._require_superadmin(request)
        from datetime import datetime, timezone

        from sqlalchemy import select
        from sqlalchemy.orm import Session
        from starlette.responses import RedirectResponse

        from autosend.admin_models import engine, PlatformEmailSettings

        form = await request.form()
        smtp_host = (form.get("smtp_host") or "").strip()
        smtp_port_raw = (form.get("smtp_port") or "").strip()
        smtp_username = (form.get("smtp_username") or "").strip() or None
        smtp_password = form.get("smtp_password") or None
        from_address = (form.get("from_address") or "").strip()

        smtp_port = None
        if smtp_port_raw:
            try:
                smtp_port = int(smtp_port_raw)
            except ValueError:
                pass

        with Session(engine) as session:
            settings = session.execute(select(PlatformEmailSettings)).scalars().first()
            if settings is None:
                if not smtp_host or smtp_port is None or not smtp_password or not from_address:
                    return await self.templates.TemplateResponse(
                        request, "platform_email_settings.html",
                        {
                            "settings": None,
                            "error": "SMTP Host, a valid SMTP Port, SMTP Password and From Address are all required.",
                        },
                        status_code=400,
                    )
                settings = PlatformEmailSettings(
                    smtp_host=smtp_host, smtp_port=smtp_port,
                    smtp_username=smtp_username, smtp_password=smtp_password,
                    from_address=from_address,
                    created_at=datetime.now(timezone.utc).isoformat(),
                )
                session.add(settings)
            else:
                if smtp_host:
                    settings.smtp_host = smtp_host
                if smtp_port is not None:
                    settings.smtp_port = smtp_port
                settings.smtp_username = smtp_username
                if smtp_password:
                    settings.smtp_password = smtp_password
                if from_address:
                    settings.from_address = from_address
            session.commit()
        return RedirectResponse(url="/platform-email-settings", status_code=303)


class AICredentialsView(VisibleIfAccessible, BaseView):
    """Superadmin-only singleton settings page - every platform-wide AI
    provider credential (Claude/Anthropic, Groq, ElevenLabs) plus the
    per-pipeline model/effort/prompt configuration for each of the three
    Anthropic-backed uses (AI Assistant replies, Knowledge Base ingestion,
    Voice Transcription clean-up). Replaces six retired ModelViews
    (AICredentialsAdmin, AIIngestionSettingsAdmin, GroqCredentialsAdmin,
    ElevenLabsCredentialsAdmin, VoiceTranscriptionSettingsAdmin,
    VoiceTranscriptionConfusableSpellingAdmin - all previously in
    admin_views.py) with one hand-rolled page, same shape/styling as
    MetaSettingsView/PlatformEmailSettingsView above, split into a
    "Providers" tab plus one tab per use (tab markup/JS follows the same
    tab-btn/data-target pattern as automations.html's per-module tabs).

    The underlying tables aren't 1:1 with the tabs: AICredentials.api_key
    (Providers > Claude) is the one platform-wide Anthropic key, shared by
    every Claude-backed pipeline - the AI Replies tab (same row's
    model/effort/system_prompt/custom_instructions), the Ingestion tab
    (AIIngestionSettings has no api_key column of its own any more - a
    real single account was never anything but the same key entered
    twice) and the Voice Transcription tab's Claude clean-up pass
    (VoiceTranscriptionSettings has no api_key column of its own either).
    Only the model/effort differ per pipeline, which is a genuine, kept
    distinction (e.g. a cheaper model for bulk ingestion than for live
    replies). GroqCredentials.model/ElevenLabsCredentials.model are the
    other way round: the Providers tab's Groq/ElevenLabs cards only hold
    the api_key - the model picker for each lives on the Voice
    Transcription tab instead (services/audio_transcription.py is the only
    caller of either credential, and only ever for that one pipeline), so
    save_voice_transcription below is what actually writes those two
    tables' model columns, not save_groq/save_elevenlabs. Each save route
    below only writes the columns its own form owns, upserting the
    singleton row if it doesn't exist yet - unlike the retired ModelViews,
    no single form is the sole entry point that can create these rows, so
    none of them treat their own fields as required on first save (nullable
    columns already tolerate a partially filled-in row; the application
    code that actually calls each provider is what raises a clear error if
    a required field was never set)."""
    name = "AI Credentials"
    icon = "fa-solid fa-robot"
    identity = "ai-credentials-page"

    def is_accessible(self, request: Request) -> bool:
        return request.session.get("is_superadmin", False)

    @staticmethod
    def _require_superadmin(request: Request) -> None:
        # is_accessible above only governs nav visibility - a BaseView's
        # own @expose routes are never auto-guarded by it (see the
        # sqladmin pin gotcha in CLAUDE.md), so every route below
        # re-checks explicitly.
        if not request.session.get("is_superadmin", False):
            raise HTTPException(status_code=403, detail="Superadmin only")

    async def _render(self, request: Request, error: str | None = None, error_tab: str | None = None, status_code: int = 200):
        import asyncio

        from sqlalchemy import select
        from sqlalchemy.orm import Session

        from autosend import storage
        from autosend.services import model_catalog
        from autosend.admin_models import (
            AICredentials, AIIngestionSettings, ElevenLabsCredentials, GroqCredentials,
            VoiceTranscriptionConfusableSpelling, VoiceTranscriptionSettings, engine,
        )

        with Session(engine) as session:
            claude = session.execute(select(AICredentials)).scalars().first()
            groq = session.execute(select(GroqCredentials)).scalars().first()
            elevenlabs = session.execute(select(ElevenLabsCredentials)).scalars().first()
            ingestion = session.execute(select(AIIngestionSettings)).scalars().first()
            voice = session.execute(select(VoiceTranscriptionSettings)).scalars().first()
            spellings = session.execute(
                select(VoiceTranscriptionConfusableSpelling).order_by(VoiceTranscriptionConfusableSpelling.language_code)
            ).scalars().all()

        # Model pickers are listed live from each provider rather than
        # hard-coded (see services/model_catalog.py), always keeping the
        # currently saved model(s) in the list.
        claude_models, whisper_models, scribe_models = await asyncio.gather(
            model_catalog.claude_models(
                claude and claude.model, ingestion and ingestion.model, voice and voice.model,
            ),
            model_catalog.whisper_models(groq and groq.model),
            model_catalog.scribe_models(elevenlabs and elevenlabs.model),
        )

        return await self.templates.TemplateResponse(
            request,
            "ai_credentials.html",
            {
                "claude": claude,
                "groq": groq,
                "elevenlabs": elevenlabs,
                "ingestion": ingestion,
                "voice": voice,
                "spellings": spellings,
                "claude_model_choices": claude_models,
                "whisper_model_choices": whisper_models,
                "scribe_model_choices": scribe_models,
                "language_choices": sorted(storage.VOICE_TRANSCRIPTION_LANGUAGE_CHOICES.items(), key=lambda item: item[1]),
                "error": error,
                "error_tab": error_tab,
                "active_tab": request.query_params.get("tab") or error_tab or "providers",
            },
            status_code=status_code,
        )

    @expose("/ai-credentials", methods=["GET"], identity="ai-credentials-page-get")
    async def page(self, request: Request):
        self._require_superadmin(request)
        return await self._render(request)

    @expose("/ai-credentials/claude/save", methods=["POST"], identity="ai-credentials-claude-save")
    async def save_claude(self, request: Request):
        self._require_superadmin(request)
        from datetime import datetime, timezone

        from sqlalchemy import select
        from sqlalchemy.orm import Session
        from starlette.responses import RedirectResponse

        from autosend.admin_models import AICredentials, engine

        form = await request.form()
        api_key = form.get("api_key") or None
        with Session(engine) as session:
            row = session.execute(select(AICredentials)).scalars().first()
            if row is None:
                if not api_key:
                    return await self._render(request, error="An Anthropic API key is required.", error_tab="providers", status_code=400)
                session.add(AICredentials(api_key=api_key, created_at=datetime.now(timezone.utc).isoformat()))
            elif api_key:
                row.api_key = api_key
            session.commit()
        if api_key:
            from autosend.services import model_catalog
            model_catalog.invalidate("anthropic")
        return RedirectResponse(url="/ai-credentials?tab=providers", status_code=303)

    @expose("/ai-credentials/ai-replies/save", methods=["POST"], identity="ai-credentials-ai-replies-save")
    async def save_ai_replies(self, request: Request):
        self._require_superadmin(request)
        from datetime import datetime, timezone

        from sqlalchemy import select
        from sqlalchemy.orm import Session
        from starlette.responses import RedirectResponse

        from autosend.admin_models import AICredentials, engine

        form = await request.form()
        model = (form.get("model") or "").strip() or None
        effort = (form.get("effort") or "").strip() or None
        system_prompt = (form.get("system_prompt") or "").strip() or None
        custom_instructions = (form.get("custom_instructions") or "").strip() or None
        with Session(engine) as session:
            row = session.execute(select(AICredentials)).scalars().first()
            if row is None:
                session.add(AICredentials(
                    model=model, effort=effort, system_prompt=system_prompt,
                    custom_instructions=custom_instructions,
                    created_at=datetime.now(timezone.utc).isoformat(),
                ))
            else:
                row.model = model
                row.effort = effort
                row.system_prompt = system_prompt
                row.custom_instructions = custom_instructions
            session.commit()
        return RedirectResponse(url="/ai-credentials?tab=ai-replies", status_code=303)

    @expose("/ai-credentials/groq/save", methods=["POST"], identity="ai-credentials-groq-save")
    async def save_groq(self, request: Request):
        self._require_superadmin(request)
        from datetime import datetime, timezone

        from sqlalchemy import select
        from sqlalchemy.orm import Session
        from starlette.responses import RedirectResponse

        from autosend.admin_models import GroqCredentials, engine

        form = await request.form()
        api_key = form.get("api_key") or None
        with Session(engine) as session:
            row = session.execute(select(GroqCredentials)).scalars().first()
            if row is None:
                if not api_key:
                    return await self._render(request, error="A Groq API key is required.", error_tab="providers", status_code=400)
                session.add(GroqCredentials(api_key=api_key, created_at=datetime.now(timezone.utc).isoformat()))
            elif api_key:
                row.api_key = api_key
            session.commit()
        if api_key:
            from autosend.services import model_catalog
            model_catalog.invalidate("groq")
        return RedirectResponse(url="/ai-credentials?tab=providers", status_code=303)

    @expose("/ai-credentials/elevenlabs/save", methods=["POST"], identity="ai-credentials-elevenlabs-save")
    async def save_elevenlabs(self, request: Request):
        self._require_superadmin(request)
        from datetime import datetime, timezone

        from sqlalchemy import select
        from sqlalchemy.orm import Session
        from starlette.responses import RedirectResponse

        from autosend.admin_models import ElevenLabsCredentials, engine

        form = await request.form()
        api_key = form.get("api_key") or None
        with Session(engine) as session:
            row = session.execute(select(ElevenLabsCredentials)).scalars().first()
            if row is None:
                if not api_key:
                    return await self._render(request, error="An ElevenLabs API key is required.", error_tab="providers", status_code=400)
                session.add(ElevenLabsCredentials(api_key=api_key, created_at=datetime.now(timezone.utc).isoformat()))
            elif api_key:
                row.api_key = api_key
            session.commit()
        if api_key:
            from autosend.services import model_catalog
            model_catalog.invalidate("elevenlabs")
        return RedirectResponse(url="/ai-credentials?tab=providers", status_code=303)

    @expose("/ai-credentials/ingestion/save", methods=["POST"], identity="ai-credentials-ingestion-save")
    async def save_ingestion(self, request: Request):
        self._require_superadmin(request)
        from datetime import datetime, timezone

        from sqlalchemy import select
        from sqlalchemy.orm import Session
        from starlette.responses import RedirectResponse

        from autosend.admin_models import AIIngestionSettings, engine

        form = await request.form()
        model = (form.get("model") or "").strip() or None
        effort = (form.get("effort") or "").strip() or None
        with Session(engine) as session:
            row = session.execute(select(AIIngestionSettings)).scalars().first()
            if row is None:
                session.add(AIIngestionSettings(
                    model=model, effort=effort,
                    created_at=datetime.now(timezone.utc).isoformat(),
                ))
            else:
                row.model = model
                row.effort = effort
            session.commit()
        return RedirectResponse(url="/ai-credentials?tab=ingestion", status_code=303)

    @expose("/ai-credentials/voice-transcription/save", methods=["POST"], identity="ai-credentials-voice-transcription-save")
    async def save_voice_transcription(self, request: Request):
        self._require_superadmin(request)
        from datetime import datetime, timezone

        from sqlalchemy import select
        from sqlalchemy.orm import Session
        from starlette.responses import RedirectResponse

        from autosend.admin_models import ElevenLabsCredentials, GroqCredentials, VoiceTranscriptionSettings, engine

        form = await request.form()
        transcription_provider = (form.get("transcription_provider") or "").strip() or None
        model = (form.get("model") or "").strip() or None
        effort = (form.get("effort") or "").strip() or None
        prompt = (form.get("prompt") or "").strip() or None
        multi_language_hint = (form.get("multi_language_hint") or "").strip() or None
        confusable_spelling_hint = (form.get("confusable_spelling_hint") or "").strip() or None
        # The Groq/ElevenLabs model choice lives here (under the provider
        # picker), not on the Providers tab - see those two cards, which
        # keep only the API key. Both <select>s are always present in the
        # form regardless of which one is visible/active client-side (see
        # ai_credentials.html's provider-model toggle script), so this
        # always writes both rather than only the currently-selected
        # provider's - each one's own model field defaults to blank unless
        # a value was already selected (i.e. previously saved), so this
        # can't silently wipe the inactive provider's configured model.
        groq_model = (form.get("groq_model") or "").strip() or None
        elevenlabs_model = (form.get("elevenlabs_model") or "").strip() or None
        with Session(engine) as session:
            row = session.execute(select(VoiceTranscriptionSettings)).scalars().first()
            if row is None:
                session.add(VoiceTranscriptionSettings(
                    transcription_provider=transcription_provider, model=model, effort=effort,
                    prompt=prompt, multi_language_hint=multi_language_hint,
                    confusable_spelling_hint=confusable_spelling_hint,
                    created_at=datetime.now(timezone.utc).isoformat(),
                ))
            else:
                row.transcription_provider = transcription_provider
                row.model = model
                row.effort = effort
                row.prompt = prompt
                row.multi_language_hint = multi_language_hint
                row.confusable_spelling_hint = confusable_spelling_hint

            groq_row = session.execute(select(GroqCredentials)).scalars().first()
            if groq_row is None:
                if groq_model:
                    session.add(GroqCredentials(model=groq_model, created_at=datetime.now(timezone.utc).isoformat()))
            else:
                groq_row.model = groq_model

            elevenlabs_row = session.execute(select(ElevenLabsCredentials)).scalars().first()
            if elevenlabs_row is None:
                if elevenlabs_model:
                    session.add(ElevenLabsCredentials(model=elevenlabs_model, created_at=datetime.now(timezone.utc).isoformat()))
            else:
                elevenlabs_row.model = elevenlabs_model

            session.commit()
        return RedirectResponse(url="/ai-credentials?tab=voice-transcription", status_code=303)

    @expose("/ai-credentials/confusable-spellings/{spelling_id}", methods=["GET"], identity="ai-credentials-confusable-spelling-detail")
    async def confusable_spelling_detail(self, request: Request):
        self._require_superadmin(request)
        from sqlalchemy.orm import Session

        from autosend import storage
        from autosend.admin_models import VoiceTranscriptionConfusableSpelling, engine

        spelling_pk = int(request.path_params["spelling_id"])
        with Session(engine) as session:
            spelling = session.get(VoiceTranscriptionConfusableSpelling, spelling_pk)
            if spelling is None:
                raise HTTPException(status_code=404)
        return await self.templates.TemplateResponse(
            request,
            "ai_credentials_confusable_spelling_detail.html",
            {
                "spelling": spelling,
                "language_choices": sorted(storage.VOICE_TRANSCRIPTION_LANGUAGE_CHOICES.items(), key=lambda item: item[1]),
            },
        )

    @expose("/ai-credentials/confusable-spellings/{spelling_id}/update", methods=["POST"], identity="ai-credentials-confusable-spelling-update")
    async def confusable_spelling_update(self, request: Request):
        self._require_superadmin(request)
        from sqlalchemy import select
        from sqlalchemy.orm import Session
        from starlette.responses import RedirectResponse

        from autosend import storage
        from autosend.admin_models import VoiceTranscriptionConfusableSpelling, engine

        spelling_pk = int(request.path_params["spelling_id"])
        form = await request.form()
        language_code = (form.get("language_code") or "").strip()
        confusable_name = (form.get("confusable_name") or "").strip()
        patterns = (form.get("patterns") or "").strip()
        with Session(engine) as session:
            spelling = session.get(VoiceTranscriptionConfusableSpelling, spelling_pk)
            if spelling is None:
                raise HTTPException(status_code=404)
            existing = session.execute(
                select(VoiceTranscriptionConfusableSpelling).where(
                    VoiceTranscriptionConfusableSpelling.language_code == language_code,
                    VoiceTranscriptionConfusableSpelling.id != spelling_pk,
                )
            ).first()
            if existing is not None:
                return await self.templates.TemplateResponse(
                    request,
                    "ai_credentials_confusable_spelling_detail.html",
                    {
                        "spelling": spelling,
                        "language_choices": sorted(storage.VOICE_TRANSCRIPTION_LANGUAGE_CHOICES.items(), key=lambda item: item[1]),
                        "error": "A confusable-spelling correction already exists for this language.",
                    },
                    status_code=400,
                )
            spelling.language_code = language_code
            spelling.confusable_name = confusable_name
            spelling.patterns = patterns
            session.commit()
        return RedirectResponse(url=f"/ai-credentials/confusable-spellings/{spelling_pk}", status_code=303)

    @expose("/ai-credentials/confusable-spellings/{spelling_id}/delete", methods=["POST"], identity="ai-credentials-confusable-spelling-delete")
    async def confusable_spelling_delete(self, request: Request):
        self._require_superadmin(request)
        from sqlalchemy.orm import Session
        from starlette.responses import RedirectResponse

        from autosend.admin_models import VoiceTranscriptionConfusableSpelling, engine

        spelling_pk = int(request.path_params["spelling_id"])
        with Session(engine) as session:
            spelling = session.get(VoiceTranscriptionConfusableSpelling, spelling_pk)
            if spelling is not None:
                session.delete(spelling)
                session.commit()
        return RedirectResponse(url="/ai-credentials?tab=voice-transcription", status_code=303)

    # Defined last (registers first - see class docstring on MetaSettingsView
    # for why): "/ai-credentials/confusable-spellings/new" is a static path
    # that would otherwise be shadowed by confusable_spelling_detail's
    # dynamic "/ai-credentials/confusable-spellings/{spelling_id}" pattern.
    @expose("/ai-credentials/confusable-spellings/new", methods=["GET"], identity="ai-credentials-confusable-spelling-new-page")
    async def confusable_spelling_new_page(self, request: Request):
        self._require_superadmin(request)
        from autosend import storage

        return await self.templates.TemplateResponse(
            request,
            "ai_credentials_confusable_spelling_new.html",
            {"language_choices": sorted(storage.VOICE_TRANSCRIPTION_LANGUAGE_CHOICES.items(), key=lambda item: item[1])},
        )

    @expose("/ai-credentials/confusable-spellings", methods=["POST"], identity="ai-credentials-confusable-spelling-create")
    async def confusable_spelling_create(self, request: Request):
        self._require_superadmin(request)
        from datetime import datetime, timezone

        from sqlalchemy import select
        from sqlalchemy.orm import Session
        from starlette.responses import RedirectResponse

        from autosend import storage
        from autosend.admin_models import VoiceTranscriptionConfusableSpelling, engine

        form = await request.form()
        language_code = (form.get("language_code") or "").strip()
        confusable_name = (form.get("confusable_name") or "").strip()
        patterns = (form.get("patterns") or "").strip()
        if not language_code or not confusable_name or not patterns:
            return await self.templates.TemplateResponse(
                request,
                "ai_credentials_confusable_spelling_new.html",
                {
                    "language_choices": sorted(storage.VOICE_TRANSCRIPTION_LANGUAGE_CHOICES.items(), key=lambda item: item[1]),
                    "error": "Language, confusable language name and patterns are all required.",
                },
                status_code=400,
            )
        with Session(engine) as session:
            existing = session.execute(
                select(VoiceTranscriptionConfusableSpelling).where(
                    VoiceTranscriptionConfusableSpelling.language_code == language_code,
                )
            ).first()
            if existing is not None:
                return await self.templates.TemplateResponse(
                    request,
                    "ai_credentials_confusable_spelling_new.html",
                    {
                        "language_choices": sorted(storage.VOICE_TRANSCRIPTION_LANGUAGE_CHOICES.items(), key=lambda item: item[1]),
                        "error": "A confusable-spelling correction already exists for this language.",
                    },
                    status_code=400,
                )
            spelling = VoiceTranscriptionConfusableSpelling(
                language_code=language_code, confusable_name=confusable_name, patterns=patterns,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            session.add(spelling)
            session.commit()
            new_id = spelling.id
        return RedirectResponse(url=f"/ai-credentials/confusable-spellings/{new_id}", status_code=303)


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


class InboxView(BaseView):
    """WhatsApp Inbox page shell - all data operations (conversation list,
    thread, reply, media) go through web/conversations_router.py's JSON
    API, same split as CampaignsView/dashboard.html. Core feature, no
    module gate: every org gets the Inbox, unlike the (future) AI
    Assistant module layered on top of it.
    sqladmin's own @expose already wraps this in login_required, matching
    every other BaseView here."""
    name = "Inbox"
    icon = "fa-solid fa-inbox"
    identity = "inbox-page"

    @expose("/inbox", methods=["GET"], identity="inbox-page")
    async def page(self, request: Request):
        from autosend.web.auth import get_current_web_user
        user = get_current_web_user(request)
        return await self.templates.TemplateResponse(request, "inbox.html", {"user": user})


class OnboardingView(BaseView):
    """The Embedded Signup unit picker. All the OAuth mechanics
    (the Facebook JS SDK's FB.login() call, the /onboarding/complete
    endpoint) live in web/onboarding_router.py / add_number.html - this
    just renders the picker form plus the app_id/config_id the page's JS
    needs to call FB.login(), same shell pattern as every other BaseView
    here. sqladmin's own @expose already wraps this in login_required,
    matching CampaignsView's note about get_current_web_user() just
    reading the session back out."""
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

        # app_id/config_id are plainly visible in the FB.login() call this
        # page renders client-side anyway - not secrets, unlike app_secret
        # (see onboarding_router._require_meta_settings).
        meta_settings = storage.get_meta_platform_settings()
        meta_configured = bool(meta_settings and meta_settings.get("app_id") and meta_settings.get("config_id"))

        return await self.templates.TemplateResponse(
            request, "add_number.html", {
                "user": user,
                "units": units,
                "meta_configured": meta_configured,
                "meta_app_id": meta_settings.get("app_id") if meta_settings else None,
                "meta_config_id": meta_settings.get("config_id") if meta_settings else None,
            },
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


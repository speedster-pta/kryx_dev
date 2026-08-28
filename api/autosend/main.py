import logging
from contextlib import asynccontextmanager
from pathlib import Path

import sentry_sdk
from sentry_sdk.integrations.logging import LoggingIntegration
from fastapi import Depends, FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.responses import FileResponse, RedirectResponse

from autosend.integrations.webhooks import router as webhook_router
from autosend.integrations.sme_metrics.webhook import router as sme_metrics_webhook_router
from autosend.integrations.email_wa.webhook import router as email_wa_webhook_router
from autosend.integrations.external_send import router as external_send_router
from autosend.integrations.kryx_bookings import router as kryx_bookings_router
from autosend.admin import setup_admin
from autosend.admin_auth import ScopeCleanupMiddleware
from starlette.middleware.sessions import SessionMiddleware
from autosend.auth import require_admin_key
from autosend.clients import close_clients
from autosend.config import settings
from autosend.core.db_init import init_db
from autosend.scheduler import (
    scheduler, reload_pending_campaigns, reload_serving_rules,
    reload_pending_downgrades, reload_pending_cancellations,
)
from autosend.services.registration_poller import poll_for_new_registrations
from autosend import storage
from autosend.storage.header_images import HEADER_IMAGES_DIR
from autosend.utils.logging import get_logger
from autosend.web.campaigns_router import router as campaigns_router
from autosend.web.automations_router import router as automations_router
from autosend.web.sme_metrics_router import router as sme_metrics_router
from autosend.web.email_wa_router import router as email_wa_router
from autosend.web.kryx_bookings_router import router as kryx_bookings_settings_router
from autosend.web import templates_router
from autosend.web.recipient_import import router as recipient_import_router
from autosend.web.numbers_router import router as numbers_router
from autosend.web.onboarding_router import router as onboarding_router
from autosend.web.pco_oauth_router import router as pco_oauth_router
from autosend.web.account_router import router as account_router
from autosend.web.signup_router import router as signup_router
from autosend.web.ical_router import router as ical_router
from autosend.web.billing_router import router as billing_router

logger = get_logger(__name__)

if settings.sentry_dsn:
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.environment,
        release=settings.app_version,
        # FastAPI/Starlette integrations are auto-enabled since both
        # packages are installed - this just adds the logging one, so every
        # existing logger.error/.exception/.critical call across the app
        # (registration_poller, scheduler, webhook handlers, etc.) becomes
        # a Sentry event with no per-call-site changes needed. INFO+ is
        # kept as breadcrumbs for context leading up to an error event.
        integrations=[LoggingIntegration(level=logging.INFO, event_level=logging.ERROR)],
        # No performance tracing - this is error monitoring only, not APM.
        traces_sample_rate=0.0,
    )
    logger.info("Sentry error monitoring enabled (environment=%s)", settings.environment)
else:
    logger.info("Sentry disabled (SENTRY_DSN not set)")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if settings.dry_run:
        logger.info("[SIMULATION MODE / DRY RUN] Active. All WhatsApp sends will be logged and intercepted.")

    if settings.enable_poller:
        scheduler.add_job(
            poll_for_new_registrations,
            "interval",
            minutes=settings.registration_poll_interval_minutes,
            id="poll_registrations",
            # Avoid overlapping runs if a poll takes longer than the interval
            max_instances=1,
            coalesce=True,
        )
        logger.info(
            "Registration poller started, interval=%d min",
            settings.registration_poll_interval_minutes,
        )
    else:
        logger.info("Registration poller disabled (ENABLE_POLLER=false)")

    scheduler.start()
    reload_pending_campaigns()
    reload_serving_rules()
    reload_pending_downgrades()
    reload_pending_cancellations()
    yield
    scheduler.shutdown(wait=False)
    await close_clients()



app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "web" / "sqladmin_theme"))
# 404.html extends sqladmin/layout.html, whose nav links call
# pco_visible(request)/email_wa_visible(request)/
# automation_nav_modules(request) - those are only ever
# registered as globals on admin.templates.env (see admin.py's
# setup_admin(), called at the bottom of this file), a *different* Jinja
# Environment instance from this one. Any 404 raised by a plain FastAPI
# route (not one of SQLAdmin's own, e.g. an HTTPException from
# automations_router.py/email_wa_router.py) is rendered by
# custom_http_exception_handler below using *this* `templates` object, so
# without registering the same globals here too, that render would fail
# with "'pco_visible' is undefined" instead of showing 404.html.
from autosend.web.auth import (
    email_verified,
    email_wa_module_visible,
    kryx_bookings_module_visible,
    message_usage_badge,
    org_active,
    pco_module_visible,
    sme_metrics_module_visible,
    stitch_module_visible,
    visible_automation_modules,
)

templates.env.globals["pco_visible"] = pco_module_visible
templates.env.globals["sme_metrics_visible"] = sme_metrics_module_visible
templates.env.globals["email_wa_visible"] = email_wa_module_visible
templates.env.globals["stitch_visible"] = stitch_module_visible
templates.env.globals["kryx_bookings_visible"] = kryx_bookings_module_visible
templates.env.globals["automation_nav_modules"] = visible_automation_modules
templates.env.globals["org_active"] = org_active
templates.env.globals["email_verified"] = email_verified
# See message_usage_badge's own docstring (web/auth.py) - same "layout.html
# is shared but this Environment is a separate instance from admin.templates"
# reasoning as every other global registered above.
templates.env.globals["message_usage_badge"] = message_usage_badge

# One session for the whole app: SQLAdmin's own login (mounted at the site
# root, see below) is the sole login path, and this dependency signs both
# that session and everything our own routes read back out of it.
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret_key)

# Resets admin_auth.current_scope to its default at the end of every
# request (see ScopeCleanupMiddleware docstring in admin_auth.py for why
# this matters and why it must NOT be a starlette.middleware.base.
# BaseHTTPMiddleware). IMPORTANT: if a BaseHTTPMiddleware-based middleware
# (logging, metrics, etc.) is ever added to this app, it must be added
# BEFORE this line (so it ends up wrapping OUTSIDE ScopeCleanupMiddleware
# in the stack) - AdminAuth.authenticate()'s current_scope.set() has to
# stay in the same task/context as this middleware's reset() call.
app.add_middleware(ScopeCleanupMiddleware)


@app.exception_handler(HTTPException)
async def custom_http_exception_handler(request, exc: HTTPException):
    # get_current_web_user raises 303 + Location to bounce unauthenticated
    # users to /login instead of showing a raw JSON error.
    if exc.status_code == 303 and exc.headers and "Location" in exc.headers:
        return RedirectResponse(url=exc.headers["Location"], status_code=303)
    # Render the custom 404 page for missing routes or 404 exceptions
    if exc.status_code == 404:
        return templates.TemplateResponse(
            request=request,
            name="404.html",
            status_code=404,
        )

    return await http_exception_handler(request, exc)


# --- Everything below MUST be registered before setup_admin(app) at the
# bottom of this file. SQLAdmin is mounted at the site root ("/"), which is
# a Starlette Mount - Starlette matches routes/mounts in registration
# order, and a root Mount registered first would swallow every request
# before any route added after it ever got a chance. So: static files, our
# own routers, and the two plain routes below all come first;
# setup_admin(app) is the last line in this file. ---

app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "web" / "static")), name="static")

# Header images uploaded from the Automations page live under the same
# persistent volume as the DB (see docker-compose.yml's kryx-data
# volume mounted at /data) so they survive redeploys. WhatsApp's Graph
# API fetches header images by URL at send time, so this has to be a
# real HTTPS URL nginx will proxy through - not just readable by the
# browser - which is why it's mounted here rather than served some other
# way. HEADER_IMAGES_DIR itself is defined in storage/header_images.py,
# the single source of truth shared with automations_router.py's upload
# endpoint and the storage-layer cleanup in units.py/serving.py.
HEADER_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/media/header-images", StaticFiles(directory=str(HEADER_IMAGES_DIR)), name="header_images")

app.include_router(webhook_router)
app.include_router(sme_metrics_webhook_router)
app.include_router(email_wa_webhook_router)
app.include_router(external_send_router)
app.include_router(kryx_bookings_router)
app.include_router(campaigns_router)
app.include_router(automations_router)
app.include_router(sme_metrics_router)
app.include_router(email_wa_router)
app.include_router(kryx_bookings_settings_router)
app.include_router(templates_router.router)
app.include_router(recipient_import_router)
app.include_router(numbers_router)
app.include_router(onboarding_router)
app.include_router(pco_oauth_router)
app.include_router(account_router)
app.include_router(signup_router)
app.include_router(ical_router)
app.include_router(billing_router)

@app.get("/")
async def root(request: Request):
    # SQLAdmin's own index page would otherwise claim "/" once mounted at
    # the root. Logged-in users go straight to the campaign dashboard,
    # since that's the more useful landing page once you're actually
    # signed in; anyone else (the common case for a public marketing URL)
    # gets the public landing page instead.
    if request.session.get("user_id"):
        return RedirectResponse(url="/campaigns", status_code=303)
    return templates.TemplateResponse(request, "landing.html", {})


@app.get("/for-churches")
async def for_churches(request: Request):
    # Planning Center Online specifics live on their own page rather than
    # the general landing page, which stays neutral across all
    # integrations (see storage/modules.py::AVAILABLE_MODULES for the
    # full set - PCO is only one of several).
    return templates.TemplateResponse(request, "churches.html", {})


@app.get("/for-medical-practises")
async def for_medical_practises(request: Request):
    return templates.TemplateResponse(request, "medical.html", {})


@app.get("/for-schools")
async def for_schools(request: Request):
    return templates.TemplateResponse(request, "schools.html", {})


@app.get("/for-rental-agencies")
async def for_rental_agencies(request: Request):
    return templates.TemplateResponse(request, "rental_agencies.html", {})


@app.get("/faq")
async def faq(request: Request):
    return templates.TemplateResponse(request, "faq.html", {})


@app.get("/pricing")
async def pricing(request: Request):
    # Public, logged-out pricing page - reads the same superadmin-managed
    # catalogue tables (billing/schema.py) as the post-signup plan picker
    # (signup_plan.html) and the org-admin billing dashboard, active-only
    # since an inactive/retired plan or add-on shouldn't be advertised.
    # Add-ons split the same way as admin_org_pages.BillingCatalogueView
    # and billing_router.billing_manage_page: 'capacity' add-ons expand
    # core plan limits and stack (buy more than one), 'integration'
    # add-ons are a plain on/off module toggle.
    addons = storage.list_addons()
    capacity_addons = [a for a in addons if a["kind"] == "capacity"]
    integration_addons = sorted(
        (a for a in addons if a["kind"] != "capacity"), key=lambda a: a["name"].lower()
    )
    return templates.TemplateResponse(
        request,
        "pricing.html",
        {
            "plans": storage.list_plans(),
            "capacity_addons": capacity_addons,
            "integration_addons": integration_addons,
        },
    )


@app.get("/sitemap.xml", include_in_schema=False)
async def sitemap():
    # Served at the root path (not just /static/sitemap.xml) since that's
    # where search engine crawlers look by convention.
    path = Path(__file__).parent / "web" / "static" / "sitemap.xml"
    return FileResponse(path, media_type="application/xml")


@app.get("/health")
async def health():
    return {"healthy": True}


@app.get("/ops/sentry-test", dependencies=[Depends(require_admin_key)])
async def sentry_test():
    """Deliberately throws to confirm SENTRY_DSN is actually delivering
    events end-to-end. Behind require_admin_key (unlike the wizard's
    suggested public /sentry-debug/ route) since it's ops-only and always
    errors - matches every other /ops/* endpoint's gating. Hit this once
    after setting a new SENTRY_DSN; an event should show up in the Sentry
    project within a few seconds."""
    raise RuntimeError("Sentry test event - triggered manually via /ops/sentry-test")


@app.post("/ops/poll-now", dependencies=[Depends(require_admin_key)])
async def trigger_poll_now():
    """Manual trigger for testing without waiting for the scheduled interval.
    Renamed from /admin/poll-now - "/admin" is no longer a meaningful
    prefix in this app now that SQLAdmin lives at the root; update any
    cron/monitoring hitting the old path."""
    await poll_for_new_registrations()
    return {"triggered": True}


@app.post("/ops/sync-phone-numbers", dependencies=[Depends(require_admin_key)])
async def sync_phone_numbers():
    """One-off backfill for WhatsAppNumber rows whose display_phone_number
    (the human-readable MSISDN) is still NULL - either added manually
    before that field existed, or created before this column was
    introduced. Going forward, Embedded Signup captures this at onboarding
    time (see onboarding_router.py), so this only needs re-running for
    stragglers, not on a schedule."""
    from autosend import whatsapp_limits

    numbers = [n for n in storage.get_whatsapp_numbers(None) if not n.get("display_phone_number")]
    synced, failed = [], []
    for number in numbers:
        display_number = whatsapp_limits.sync_display_number_from_meta(
            number["access_token"], number["phone_number_id"]
        )
        if display_number:
            storage.update_whatsapp_number_display_number(number["id"], display_number)
            synced.append(number["id"])
        else:
            failed.append(number["id"])
    return {"synced": synced, "failed": failed}


@app.get("/ops/failures", dependencies=[Depends(require_admin_key)])
async def list_failures():
    """Things that failed to send a WhatsApp message and will NOT be
    auto-retried. Check this regularly, or wire it up to an alert -
    registration failures involve real money, form failures mean a
    visitor never got welcomed. Renamed from /admin/failures, see above."""
    return {
        "registration_failures": storage.get_recent_failures(),
        "form_failures": storage.get_recent_form_failures(),
    }


setup_admin(app)


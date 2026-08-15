"""Composition root: wires the model layer, auth, scoping, CRUD views, and
page shells together and mounts sqladmin onto the FastAPI app.

This module used to contain everything (models, auth, scoping, every
ModelView/BaseView) in one ~960-line file. It's now split into:

    admin_models.py   - SQLAlchemy ORM models + engine (mirrors storage.py's schema)
    admin_widgets.py   - generic sqladmin form/widget helpers
    admin_auth.py       - authenticate_user(), current_scope, AdminAuth
    admin_scoping.py   - ScopedModelView (row-level unit scoping)
    admin_views.py       - the CRUD ModelViews (Unit/PCOOrgSettings/WhatsAppNumber/User)
    admin_pages.py       - the BaseView page shells (Campaigns/Automations/Templates/Usage/Account)

Everything that used to be importable as `autosend.admin.X` is
re-exported below, so no other module needs to change its imports because
of this split.
"""
from pathlib import Path

from sqladmin import Admin
from sqladmin.models import ModelView
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqladmin.helpers import slugify_class_name

from autosend.config import settings

# Re-exported for backward compatibility - see module docstring.
from autosend.admin_models import (
    EncryptedString,
    Base,
    engine,
    Organisation,
    Unit,
    PCOOrganizationSettings,
    MetaPlatformSettings,
    WhatsAppNumber,
    WhatsAppTemplate,
    FormTemplate,
    user_units_table,
    User,
)
from autosend.admin_widgets import (
    _checkbox_render_kw,
    CheckboxListWidget,
    CheckboxQuerySelectMultipleField,
)
from autosend.admin_auth import (
    authenticate_user,
    current_scope,
    AdminAuth,
)
from autosend.admin_scoping import ScopedModelView
from autosend.admin_views import (
    _slugify,
    OrganisationAdmin,
    UnitAdmin,
    UnitWebhookAdmin,
    PCOOrganizationSettingsAdmin,
    MetaPlatformSettingsAdmin,
    WhatsAppNumberAdmin,
    UserAdmin,
)
from autosend.admin_pages import (
    CampaignsView,
    AutomationsView,
    TemplatesView,
    WabaUsageView,
    ModulesView,
    HistoryView,
    OnboardingView,
    AccountView,
)
from autosend.admin_org_pages import (
    OrganisationsView,
    PcoSettingsView,
    EmailWaSettingsView,
)

def _identity_for_object(self, obj):
    # Same model as the current view.
    if obj.__class__ is self.model:
        return self.identity
    # Otherwise, find the registered ModelView for this model.
    for view in self._admin_ref._views:
        if getattr(view, "is_model", False) and view.model is obj.__class__:
            return view.identity
    # Fallback to SQLAdmin's original behaviour.
    return slugify_class_name(obj.__class__.__name__)

ModelView._identity_for_object = _identity_for_object

def _related_field_linkable(self, request, name):
    # Default: every relation column renders as a link, same as SQLAdmin's
    # own behaviour. Overridden per-view where the link target's own
    # is_accessible() would 403 for the viewer's role (e.g.
    # WhatsAppNumberAdmin.unit - see admin_views.py - since UnitAdmin is
    # superadmin/org-admin only).
    return True

ModelView._related_field_linkable = _related_field_linkable

def setup_admin(app):
    # base_url="/": everything (SQLAdmin's model views, our BaseView pages,
    # its login/logout) now lives at the root instead of under /admin.
    # IMPORTANT: because this performs a Starlette Mount("/"), it must be
    # called LAST in main.py, after every other route/mount on `app` -
    # Starlette matches routes in registration order, and a root Mount
    # registered first would silently shadow (404) anything added after it.
    admin = Admin(
        app,
        engine,
        authentication_backend=AdminAuth(secret_key=settings.session_secret_key),
        	title="Shofar Automation",
        	logo_url="/static/icon_whatsapp.svg",
        	templates_dir=str(Path(__file__).parent / "web" / "sqladmin_theme"),
        	base_url="/",
    )
    # Lets list.html render a sort link on a relationship column (e.g.
    # WhatsAppNumberAdmin's "unit") whose column_sortable_list
    # entry is a dotted related-field path (e.g. "unit.name")
    # rather than the relationship attribute itself - see list.html's use
    # of sort_field_for() in its header loop.
    def _sort_field_for(model_view, name: str) -> str | None:
        for field in model_view._sort_fields:
            if field == name or field.startswith(f"{name}."):
                return field
        return None

    admin.templates.env.globals["sort_field_for"] = _sort_field_for

    # Lets layout.html hide every PCO-specific nav link (Automations, PCO
    # Webhook) for orgs without the PCO module enabled - same check
    # AutomationsView/UnitWebhookAdmin's is_accessible and
    # automations_router.py's dependency gate use
    # (web.auth.pco_module_visible), so all of them stay in lockstep.
    from autosend.web.auth import email_wa_module_visible, pco_module_visible

    admin.templates.env.globals["pco_visible"] = pco_module_visible
    # Same purpose as pco_visible above, for the independent
    # email-to-WhatsApp module - see web.auth.email_wa_module_visible.
    admin.templates.env.globals["email_wa_visible"] = email_wa_module_visible

    admin.add_view(CampaignsView)
    admin.add_view(AutomationsView)
    admin.add_view(HistoryView)
    admin.add_view(TemplatesView)
    admin.add_view(WabaUsageView)
    admin.add_view(OrganisationAdmin)
    admin.add_view(OrganisationsView)
    admin.add_view(ModulesView)
    admin.add_view(PcoSettingsView)
    admin.add_view(EmailWaSettingsView)
    admin.add_view(UnitAdmin)
    admin.add_view(PCOOrganizationSettingsAdmin)
    admin.add_view(MetaPlatformSettingsAdmin)
    admin.add_view(WhatsAppNumberAdmin)
    admin.add_view(OnboardingView)
    admin.add_view(UnitWebhookAdmin)
    admin.add_view(UserAdmin)
    admin.add_view(AccountView)

    # SQLAdmin mounts itself as its own Starlette sub-app at base_url="/"
    # (see Admin.__init__ in sqladmin/application.py) with its OWN
    # exception_handlers dict, keyed on starlette.exceptions.HTTPException.
    # Because that Mount catches every URL under "/", any 404 that isn't
    # matched by one of our routers above never reaches main.py's
    # @app.exception_handler(HTTPException) - sqladmin's internal handler
    # renders its own bare "sqladmin/error.html" first (extends the same
    # layout, hence the header-only look with no styled content). This
    # replaces that internal handler with one that renders our 404.html
    # for 404s and falls back to sqladmin's own error template for
    # anything else (403, 500, etc.) so we don't lose that behaviour.
    async def admin_http_exception(request, exc):
        if exc.status_code == 404:
            return await admin.templates.TemplateResponse(
                request, "404.html", {}, status_code=404
            )
        return await admin.templates.TemplateResponse(
            request,
            "sqladmin/error.html",
            {"status_code": exc.status_code, "message": exc.detail},
            status_code=exc.status_code,
        )

    admin.admin.exception_handlers = {StarletteHTTPException: admin_http_exception}

    return admin

UserAdmin.identity = "users"
WhatsAppNumberAdmin.identity = "whatsapp-numbers"
PCOOrganizationSettingsAdmin.identity = "pco-settings"
MetaPlatformSettingsAdmin.identity = "meta-settings"
UnitWebhookAdmin.identity = "pco-webhook"

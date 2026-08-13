"""Composition root: wires the model layer, auth, scoping, CRUD views, and
page shells together and mounts sqladmin onto the FastAPI app.

This module used to contain everything (models, auth, scoping, every
ModelView/BaseView) in one ~960-line file. It's now split into:

    admin_models.py   - SQLAlchemy ORM models + engine (mirrors storage.py's schema)
    admin_widgets.py   - generic sqladmin form/widget helpers
    admin_auth.py       - authenticate_staff_user(), current_scope, AdminAuth
    admin_scoping.py   - ScopedModelView (row-level unit scoping)
    admin_views.py       - the CRUD ModelViews (Unit/PCOOrgSettings/WhatsAppNumber/StaffUser)
    admin_pages.py       - the BaseView page shells (Campaigns/Automations/Templates/Usage/Account)

Everything that used to be importable as `shofar_automation.admin.X` is
re-exported below, so no other module needs to change its imports because
of this split.
"""
from pathlib import Path

from sqladmin import Admin
from sqladmin.models import ModelView
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqladmin.helpers import slugify_class_name

from shofar_automation.config import settings

# Re-exported for backward compatibility - see module docstring.
from shofar_automation.admin_models import (
    EncryptedString,
    Base,
    engine,
    Unit,
    PCOOrganizationSettings,
    MetaPlatformSettings,
    WhatsAppNumber,
    WhatsAppTemplate,
    FormTemplate,
    staff_user_units_table,
    StaffUser,
)
from shofar_automation.admin_widgets import (
    _checkbox_render_kw,
    CheckboxListWidget,
    CheckboxQuerySelectMultipleField,
)
from shofar_automation.admin_auth import (
    authenticate_staff_user,
    current_scope,
    AdminAuth,
)
from shofar_automation.admin_scoping import ScopedModelView
from shofar_automation.admin_views import (
    _slugify,
    UnitAdmin,
    UnitWebhookAdmin,
    PCOOrganizationSettingsAdmin,
    MetaPlatformSettingsAdmin,
    WhatsAppNumberAdmin,
    StaffUserAdmin,
)
from shofar_automation.admin_pages import (
    CampaignsView,
    AutomationsView,
    TemplatesView,
    WabaUsageView,
    HistoryView,
    OnboardingView,
    AccountView,
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

    admin.add_view(CampaignsView)
    admin.add_view(AutomationsView)
    admin.add_view(HistoryView)
    admin.add_view(TemplatesView)
    admin.add_view(WabaUsageView)
    admin.add_view(UnitAdmin)
    admin.add_view(PCOOrganizationSettingsAdmin)
    admin.add_view(MetaPlatformSettingsAdmin)
    admin.add_view(WhatsAppNumberAdmin)
    admin.add_view(OnboardingView)
    admin.add_view(UnitWebhookAdmin)
    admin.add_view(StaffUserAdmin)
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

StaffUserAdmin.identity = "users"
WhatsAppNumberAdmin.identity = "whatsapp-numbers"
PCOOrganizationSettingsAdmin.identity = "pco-settings"
MetaPlatformSettingsAdmin.identity = "meta-settings"
UnitWebhookAdmin.identity = "pco-webhook"

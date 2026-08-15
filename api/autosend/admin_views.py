"""sqladmin CRUD views (ModelViews) for each unit-scoped or
org-wide table. Page shells (BaseView dashboards) live in admin_pages.py
instead - this file is only the SQLAdmin/CRUD side."""
import re
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from markupsafe import Markup, escape
from wtforms import PasswordField, BooleanField
from wtforms.validators import NumberRange, Optional
import bcrypt
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqladmin.models import ModelView
from starlette.requests import Request

from autosend.admin_models import (
    engine,
    Organisation,
    Unit,
    PCOOrganizationSettings,
    MetaPlatformSettings,
    WhatsAppNumber,
    User,
)
from autosend.admin_scoping import OrgScopedModelView, ScopedModelView, VisibleIfAccessible
from autosend.admin_widgets import _checkbox_render_kw, CheckboxQuerySelectMultipleField
from autosend.whatsapp_limits import CAMPAIGN_RESERVE_FRACTION, sync_display_number_from_meta

def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "unit"


def _flash_slug_change_reminder(request: Request, new_slug: str) -> None:
    """Stashes a one-time reminder in the session for layout.html to render
    on the next page load. A unit's slug is embedded in any per-unit
    webhook URL keyed off it (currently the PCO people-form webhook, but
    kept generic since other integrations may key off it too) - so
    whoever configured such a webhook needs to update its URL to match.
    Session-based (not a query param) since sqladmin's post-update
    redirect gives us no hook to attach one directly."""
    request.session["flash_message"] = (
        f"This unit's slug changed to '{new_slug}'. If you have any "
        f"webhooks configured for this unit, update their URLs to use "
        f"the new slug."
    )


def _keep_existing_if_blank(data: dict, *fields: str) -> None:
    """A blank submission for any of these fields means "keep the existing
    DB value" - masked PasswordField credentials never re-render their
    current value, so a truly-intended-blank looks identical to "user
    didn't touch this field". Pops each blank field from the update
    payload rather than overwriting it with an empty string."""
    for field in fields:
        if not data.get(field):
            data.pop(field, None)


def _reject_if_exists(model: Any, message: str, where: Any = None) -> None:
    """400s if a row already exists (optionally filtered by `where`) -
    used for singleton/one-per-org tables where the DB's own UNIQUE
    constraint would catch this too, but this gives a clean error instead
    of a raw IntegrityError."""
    with Session(engine) as session:
        stmt = select(model)
        if where is not None:
            stmt = stmt.where(where)
        existing = session.execute(stmt).first()
    if existing is not None:
        raise HTTPException(status_code=400, detail=message)


def _organisation_link(model: Any, _attribute: str, request: Request) -> Any:
    """Unit's default relationship link (from sqladmin's own
    _identity_for_object machinery, see admin.py) points at
    OrganisationAdmin's bare generic details route (/organisation/details/
    {id}) - that view is superadmin-only (OrganisationAdmin.is_accessible),
    so the link 403s for the org admins who are the ones actually clicking
    it from their own unit's page. Building the href explicitly here
    instead: superadmins (who can view any org) get the real per-org page
    (OrganisationsView.detail_page, admin_org_pages.py); org admins get
    their own-org page (OrganisationsView.own_page), which needs no org_id
    since UnitAdmin already scopes them to their own org's units anyway."""
    org = model.organisation
    if org is None:
        return ""
    if request.session.get("is_superadmin", False):
        href = f"/organisations/{org.id}"
    else:
        href = "/organisation"
    return Markup(
        f'<a class="text-brand-primary" href="{href}">{escape(str(org))}</a>'
    )


class OrganisationAdmin(VisibleIfAccessible, ModelView, model=Organisation):
    """Top of the tenancy hierarchy - superadmin-only, same gating as
    UnitAdmin/UserAdmin below. Deliberately NOT reachable by an org
    admin: there is no "create organisation" action anywhere in the
    logged-in admin for anyone but a superadmin.

    can_create is off: creating an org needs more than a bare
    `organisations` row (see storage.create_organisation - every org must
    be provisioned with a default "Main" unit in the same transaction, or
    UnitAdmin.delete_model's last-unit guard leaves it stranded with none
    and no way to add one). This view's generic scaffolded create form
    used to be reachable and would skip that entirely; the real "create
    organisation" action for a superadmin is now
    OrganisationsView.new_page/create (admin_org_pages.py, "/organisations/new"),
    which calls storage.create_organisation directly. The other way an
    organisation gets created is the public self-serve /signup flow
    (web/signup_router.py), a distinct identity, not an action available
    from within an existing org admin's own session."""
    column_list = [Organisation.id, Organisation.name, Organisation.slug, Organisation.active]
    column_labels = {
        Organisation.id: "ID",
        Organisation.name: "Name",
        Organisation.slug: "Slug",
        Organisation.active: "Active",
        Organisation.created_at: "Created At",
        Organisation.units: "Units",
    }
    column_sortable_list = [Organisation.name]
    # Units is a to-many relationship - sqladmin wraps those in parens
    # by default ("(Main)") when show_compact_lists is on; off renders
    # each unit as its own line/link instead (same reasoning as
    # UnitAdmin.show_compact_lists above).
    show_compact_lists = False
    form_columns = [Organisation.name, Organisation.active]
    form_overrides = {"active": BooleanField}
    form_args = {"active": _checkbox_render_kw()}
    can_create = False
    # Deactivate via `active` instead - hard delete would orphan this
    # org's units/staff (no FK cascade enforcement at the SQLite level for
    # a delete issued through the ORM here).
    can_delete = False
    name = "Organisation"
    name_plural = "Organisations"
    icon = "fa-solid fa-building"

    def is_accessible(self, request: Request) -> bool:
        return request.session.get("is_superadmin", False)


class UnitAdmin(VisibleIfAccessible, ScopedModelView, model=Unit):
    unit_field = "id"  # Unit's own PK is the scoping field
    # SQLAdmin wraps to-many relationship values like whatsapp_numbers in
    # parens by default ("(Main Line)") when show_compact_lists is on -
    # turning it off renders each one as its own line instead.
    show_compact_lists = False
    # slug: unused everywhere else in the codebase (was only ever read back
    # by this admin page) - dropped from both the list and the form.
    # insert_model() below derives one from the name instead, purely to
    # satisfy the existing NOT NULL column without a migration.
    column_list = [Unit.id, Unit.organisation, Unit.name, Unit.whatsapp_numbers, Unit.active]
    column_labels = {
        Unit.organisation: "Organisation",
        Unit.whatsapp_numbers: "Numbers",
        Unit.templates: "Automations",
        Unit.id: "ID",
        Unit.slug: "Slug",
        # "Unit name" rather than plain "Name" - this sits directly below
        # the Organisation link on the Details page, where "Name" alone
        # reads ambiguously (the org's name or the unit's?).
        Unit.name: "Unit name",
        Unit.active: "Active",
        Unit.pco_webhook_user_name: "PCO Webhook User",
        Unit.pco_campus_id: "PCO Campus ID",
        Unit.created_at: "Created At",
    }
    column_sortable_list = [Unit.name]
    column_formatters = {
        # to-many relationship columns are rendered by zipping this list
        # 1:1 against each related object - a single joined string here
        # gets zipped character-by-character instead, so this must return
        # one formatted string per WhatsApp number, not one combined string.
        Unit.whatsapp_numbers: lambda m, a: [n.label for n in m.whatsapp_numbers],
        Unit.organisation: _organisation_link,
    }
    column_formatters_detail = {
        Unit.organisation: _organisation_link,
    }
    form_columns = [
        Unit.organisation, Unit.name, Unit.active,
        Unit.pco_webhook_secret, Unit.pco_campus_id,
    ]
    # PCO webhook secret is a live credential, same treatment as
    # WhatsAppNumber.access_token: masked password-style input, never
    # re-rendered with the plaintext value on the edit form. Leaving it
    # blank on edit keeps the existing value (see update_model below).
    form_overrides = {
        "pco_webhook_secret": PasswordField,
        "active": BooleanField,
    }
    form_args = {
        "pco_webhook_secret": {"label": "PCO Webhook Secret", "validators": []},
        "active": _checkbox_render_kw(),
    }
    # Explicit order (rather than column_details_exclude_list) so the
    # Details page reads Organisation -> Unit name -> Numbers -> Automations
    # -> ... instead of the SQLAlchemy mapper's declaration order (which put
    # both relationship columns before id/name/active). This also drops:
    # - org_id: the raw FK int, redundant with the Organisation link above it
    # - pco_webhook_secret: a live credential - form_overrides only masks
    #   the create/edit form, the separate Details view has no exclusion by
    #   default and would otherwise render this live, decrypted secret in
    #   plaintext (same gap as WhatsAppNumberAdmin.access_token below)
    # - form_mappings: not relevant to what staff need on this page (the
    #   relation itself, and its edit form, are unaffected; this only hides
    #   it here)
    column_details_list = [
        Unit.organisation, Unit.name, Unit.whatsapp_numbers, Unit.templates,
        Unit.id, Unit.slug, Unit.active,
        Unit.pco_webhook_user_name, Unit.pco_campus_id, Unit.created_at,
    ]
    name = "Unit"
    name_plural = "Units"
    icon = "fa-solid fa-people-group"

    def is_accessible(self, request: Request) -> bool:
        # Superadmins manage every org's units; org admins manage their
        # own org's units. Plain unit-scoped staff never reach this view -
        # UnitWebhookAdmin below is their equivalent for the two fields
        # they're allowed to touch.
        return request.session.get("is_superadmin", False) or request.session.get("is_org_admin", False)

    def _related_field_linkable(self, request: Request, name: str) -> bool:
        # The list/details templates always wrap a relation column's own
        # auto-generated href (OrganisationAdmin's bare /organisation/
        # details/{id} route, superadmin-only) around whatever
        # column_formatters returns - so a formatter alone can't swap in a
        # different href, only its inner text. Disabling the template's
        # own link here (falling back to plain formatted_value) is what
        # lets _organisation_link's own <a> tag - pointing at the correct,
        # role-appropriate page - render unwrapped instead.
        if name == "organisation":
            return False
        return True

    async def insert_model(self, request: Request, data: dict) -> Any:
        if not request.session.get("is_superadmin", False):
            # Never trust the submitted organisation field for a
            # non-superadmin - force their own org regardless of what the
            # form posted (scaffold_form's query_factory override already
            # limits the dropdown to just their org, but this is the real
            # boundary, not that).
            data["org_id"] = request.session.get("org_id")
            data.pop("organisation", None)
        data["slug"] = _slugify(data.get("name") or "")
        data["created_at"] = datetime.now(timezone.utc).isoformat()
        return await super().insert_model(request, data)

    async def update_model(self, request: Request, pk: str, data: dict) -> Any:
        if not request.session.get("is_superadmin", False):
            # Same server-side enforcement as insert_model above - a unit
            # can never be moved to another org by an org admin, regardless
            # of what the form posted.
            data["org_id"] = request.session.get("org_id")
            data.pop("organisation", None)
        _keep_existing_if_blank(data, "pco_webhook_secret")
        # slug is hidden from this form entirely (see class comment above)
        # but always tracks name - re-derived on every edit, not just on
        # creation, so a later name fix/typo correction can't leave a
        # stale slug behind. Look up the pre-edit slug first so we can
        # tell whether this save actually changes it - name edits that
        # don't change the slugified form (e.g. capitalization only)
        # shouldn't trigger a PCO reminder.
        with Session(engine) as session:
            old_slug = session.get(Unit, int(pk)).slug
        new_slug = _slugify(data.get("name") or "")
        data["slug"] = new_slug
        result = await super().update_model(request, pk, data)
        if new_slug != old_slug:
            _flash_slug_change_reminder(request, new_slug)
        return result

    async def delete_model(self, request: Request, pk: Any) -> None:
        # Every organisation must always have at least one unit (see
        # storage.organisations.create_organisation, which auto-provisions
        # one) - refuse to leave an org with zero by deleting its last one.
        with Session(engine) as session:
            unit = session.get(Unit, int(pk))
            if unit is not None:
                remaining = session.query(Unit).filter(Unit.org_id == unit.org_id).count()
                if remaining <= 1:
                    raise HTTPException(
                        status_code=400,
                        detail="Can't delete an organisation's last remaining unit.",
                    )
        await super().delete_model(request, pk)

    async def scaffold_form(self, rules: list[str] | None = None):
        """Drops the two PCO fields from the create/edit form unless PCO
        is enabled for the relevant org - otherwise every unit's form
        shows a "PCO Webhook Secret"/"PCO Campus ID" pair that does
        nothing for orgs without PCO.

        For an org admin, "the relevant org" is just their own session
        org_id (admin_auth.current_scope) - true on both create and edit,
        since an org admin can only ever create/edit units in their own
        org anyway. For a superadmin editing an *existing* unit, it's
        that specific unit's org - looked up via admin_auth.current_edit_pk,
        a contextvar populated the same way as current_scope (see its
        docstring) because scaffold_form itself gets no request/pk to
        work with. A superadmin's *create* form is the one case with no
        org to check at all (they haven't picked one in the dropdown yet
        at the point this runs, and can create a unit under any org) -
        treated the same as "not enabled", so the fields are hidden
        rather than shown for a guess that might be wrong; they're one
        "Save and continue editing" click away on the edit form, which
        does know the org once the unit exists.

        wtforms' FormMeta rebuilds _unbound_fields (and therefore the
        rendered field list) whenever a class attribute is added/removed,
        so delattr here is enough to drop a field - no need to touch
        form_columns itself, and scaffold_form's own cache-if-`self.form`
        check means a fresh Form class (safe to mutate) is built every
        call anyway."""
        form_cls = await super().scaffold_form(rules)

        from autosend.admin_auth import current_edit_pk, current_scope
        from autosend import storage

        scope = current_scope.get()
        if scope is None:
            return form_cls
        is_superadmin, _is_org_admin, session_org_id, _unit_ids = scope

        if is_superadmin:
            org_id = None
            pk = current_edit_pk.get()
            if pk is not None:
                with Session(engine) as session:
                    unit = session.get(Unit, int(pk))
                org_id = unit.org_id if unit is not None else None
        else:
            org_id = session_org_id

        if org_id is not None and storage.is_enabled(org_id, storage.MODULE_PCO):
            return form_cls

        for field_name in ("pco_webhook_secret", "pco_campus_id"):
            if hasattr(form_cls, field_name):
                delattr(form_cls, field_name)
        return form_cls

    async def details_context(self, request: Request) -> dict:
        """Drops "PCO Webhook User"/"PCO Campus ID" from the read-only
        Details page when *this specific unit's* org doesn't have PCO
        enabled - unlike scaffold_form above, this applies to superadmins
        too: Details is read-only, so there's no "set these up before a
        module grant" reason for a superadmin to need them here, and the
        org's own Integrations section (organisation_detail.html) is
        already the place to see/grant module state. Also unlike
        scaffold_form, request.path_params has the pk being viewed, so
        this checks the *unit's actual org* rather than the viewer's own
        session org - correct for a superadmin looking at any org's unit,
        not just an org admin looking at their own.

        sqladmin computes model_view._details_prop_names once at
        registration time (not per-request), so it can't be mutated here
        - instead this hands the template a request-scoped
        "visible_detail_props" override (see details.html's for-loop),
        which only this view ever sets."""
        context = await super().details_context(request)

        pk = request.path_params.get("pk")
        if pk is None:
            return context

        with Session(engine) as session:
            unit = session.get(Unit, int(pk))
        org_id = unit.org_id if unit is not None else None

        from autosend import storage

        if org_id is not None and storage.is_enabled(org_id, storage.MODULE_PCO):
            return context

        hidden = {"pco_webhook_user_name", "pco_campus_id"}
        context["visible_detail_props"] = [
            name for name in self._details_prop_names if name not in hidden
        ]
        return context


class UnitWebhookAdmin(VisibleIfAccessible, ScopedModelView, model=Unit):
    """Second CRUD view over the *same* Unit model/table as
    UnitAdmin above, restricted to the two PCO webhook fields
    (secret + who-to-ask). UnitAdmin itself stays superadmin-only
    (it also edits name/slug/active/campus, real unit identity,
    not just webhook config) - but these two need to be settable by
    ordinary unit-scoped staff, the same people who already
    manage WhatsAppNumber.access_token on the Numbers page. Reusing
    ScopedModelView here (unit_field="id", same as
    UnitAdmin) means staff only ever see/edit their own
    unit(s), with no new scoping logic to maintain.

    can_create/can_delete are off: this view exists purely to edit
    webhook config on a unit that already exists (created by a
    superadmin via UnitAdmin) - not to manage units
    themselves.

    Because admin.py's _identity_for_object() resolves a Unit's
    link identity to whichever registered view for that model comes
    first, and UnitAdmin is added to Admin() before this one (see
    setup_admin()), links to a unit from elsewhere (e.g. the
    WhatsAppNumberAdmin list's unit column) still land on the
    full UnitAdmin page, not here - this view is only reached via
    its own nav link/URL.
    """
    identity = "pco-webhook"
    unit_field = "id"  # same as UnitAdmin - Unit's own PK is the scoping field
    can_create = False
    can_delete = False
    # Same reasoning as UnitAdmin: sqladmin wraps to-many values
    # like whatsapp_numbers in parens ("(Main Line)") on the Details page
    # by default when show_compact_lists is on - turning it off renders
    # each one plainly, one per line, instead.
    show_compact_lists = False
    column_list = [Unit.name, Unit.pco_webhook_user_name, Unit.active]
    column_labels = {
        Unit.whatsapp_numbers: "WhatsApp Numbers",
        Unit.id: "ID",
        Unit.slug: "Slug",
        Unit.name: "Name",
        Unit.active: "Active",
        Unit.pco_webhook_user_name: "PCO Webhook User",
        Unit.pco_campus_id: "PCO Campus ID",
        Unit.created_at: "Created At",
    }
    form_columns = [Unit.pco_webhook_secret, Unit.pco_webhook_user_name]
    # Same masked-credential treatment as UnitAdmin/WhatsAppNumberAdmin:
    # blank on edit keeps the existing value (see update_model below), and
    # the Details page never renders the live decrypted secret.
    # pco_webhook_user_name isn't a credential (just free text naming who
    # to ask) so it isn't masked or blank-preserved the same way - a
    # blank submission clears it, same as any ordinary text field.
    form_overrides = {"pco_webhook_secret": PasswordField}
    form_args = {
        "pco_webhook_secret": {
            "label": "PCO Webhook Secret",
            "validators": [],
            "description": (
                "The Authenticity Secret from this unit's PCO webhook "
                "subscription. Validates every inbound PCO form/registration "
                "webhook for this unit - one secret covers all of "
                "them, since PCO signs deliveries per subscription, not per form."
            ),
        },
        "pco_webhook_user_name": {
            "label": "PCO Webhook User",
            "description": (
                "Purely informational - which staff member to ask about this "
                "unit's PCO form/registration webhook."
            ),
        },
    }
    # templates/form_mappings aren't relevant to webhook config - hidden
    # from this Details view the same way form_mappings is hidden on
    # UnitAdmin's own Details view above.
    column_details_exclude_list = [
        Unit.pco_webhook_secret, Unit.templates, Unit.form_mappings,
    ]
    name = "PCO Webhook"
    name_plural = "PCO Webhook"
    icon = "fa-solid fa-satellite-dish"

    def is_accessible(self, request: Request) -> bool:
        # Row-level scoping (ScopedModelView's unit_field="id") is what
        # restricts *which* unit(s) a non-superadmin can reach here, same
        # trust model as WhatsAppNumberAdmin - but this whole view is
        # pointless for an org without the PCO module enabled (the
        # webhook route itself 404s regardless of secret, see
        # integrations/webhooks.py), so gate it the same way as
        # Automations/UnitWebhookAdmin's nav link (web.auth.pco_module_visible).
        from autosend.web.auth import pco_module_visible

        return pco_module_visible(request)

    def accessible_units(self, request: Request) -> list[tuple[int, str]]:
        """(id, name) pairs, sorted by name, for every unit the
        current user is allowed to reach on this view - same scoping rule
        as _scope() in ScopedModelView (all units for a
        superadmin, only request.session["unit_ids"] for scoped
        staff). Used by edit.html to render a unit switcher: most
        staff have exactly one unit and never see it (the caller
        only renders the picker when len() > 1), but superadmins and any
        staff member assigned more than one unit need a way to
        get to a *different* unit's webhook config from the edit
        page itself, since the list page's own "Configure Webhook" button
        only ever jumps to the first accessible row.
        """
        with Session(engine) as session:
            stmt = select(Unit.id, Unit.name).order_by(Unit.name)
            if not request.session.get("is_superadmin"):
                allowed_ids = request.session.get("unit_ids", [])
                stmt = stmt.where(Unit.id.in_(allowed_ids))
            return [(row.id, row.name) for row in session.execute(stmt).all()]

    async def update_model(self, request: Request, pk: str, data: dict) -> Any:
        _keep_existing_if_blank(data, "pco_webhook_secret")
        return await super().update_model(request, pk, data)


class PCOOrganizationSettingsAdmin(VisibleIfAccessible, OrgScopedModelView, model=PCOOrganizationSettings):
    """Per-organisation PCO Personal Access Token - one row per org
    (org_id is NOT NULL UNIQUE, see integrations/pco/schema.py). Scoped
    like UserAdmin below rather than ScopedModelView: org_id lives
    directly on this table (no unit_id to key off), so list/count queries
    filter on org_id and insert_model/update_model force it server-side.
    Superadmins manage every org's token; org admins manage only their own
    org's - never trust the client for which org a submitted row belongs
    to."""
    column_list = [PCOOrganizationSettings.id, PCOOrganizationSettings.organisation, PCOOrganizationSettings.pco_token_id]
    form_columns = [
        PCOOrganizationSettings.organisation,
        PCOOrganizationSettings.pco_token_id,
        PCOOrganizationSettings.pco_token_secret,
    ]
    form_overrides = {"pco_token_secret": PasswordField}
    form_args = {"pco_token_secret": {"label": "PCO Token Secret", "validators": []}}
    column_details_exclude_list = [PCOOrganizationSettings.pco_token_secret]
    can_delete = False
    name = "PCO Organization Settings"
    name_plural = "PCO Organization Settings"
    icon = "fa-solid fa-key"

    def is_accessible(self, request: Request) -> bool:
        # This is the raw sqladmin CRUD screen over the same org-level PCO
        # token as the friendlier PcoSettingsView (admin_org_pages.py,
        # identity="pco-config-page" at literal path "/pco-settings") -
        # kept registered as a superadmin escape hatch (see that module's
        # docstring), but it was never gated on PCO module enablement the
        # way that page is, so an org admin could reach
        # /pco-settings/edit/{pk} directly (unlinked from nav, but not
        # access-controlled) and set up a token for an org that isn't
        # even provisioned for PCO. pco_module_visible always returns True
        # for a superadmin (bypass, same as elsewhere), so this only
        # restricts org admins. Unlike PcoSettingsView, no extra inline
        # check is needed on top of this: sqladmin's own _list/_create/
        # _details/_edit/_delete DO call is_accessible automatically for a
        # real ModelView (that gap only applies to a BaseView's hand-rolled
        # @expose routes).
        from autosend.web.auth import pco_module_visible

        is_superadmin = request.session.get("is_superadmin", False)
        is_org_admin = request.session.get("is_org_admin", False)
        return (is_superadmin or is_org_admin) and pco_module_visible(request)

    async def update_model(self, request: Request, pk: str, data: dict) -> Any:
        self._check_row_scope(request, pk)
        if not request.session.get("is_superadmin", False):
            # Same server-side enforcement as insert_model above - an org
            # admin can never move this row to another org.
            data["org_id"] = request.session.get("org_id")
            data.pop("organisation", None)
        _keep_existing_if_blank(data, "pco_token_secret")
        return await super().update_model(request, pk, data)

    async def insert_model(self, request: Request, data: dict) -> Any:
        if not request.session.get("is_superadmin", False):
            # Never trust the submitted organisation field for an org
            # admin - force their own org regardless of what the form
            # posted.
            org_id = request.session.get("org_id")
            data["org_id"] = org_id
            data.pop("organisation", None)
        else:
            organisation = data.get("organisation")
            org_id = organisation.id if organisation is not None else data.get("org_id")
        if not org_id:
            raise HTTPException(status_code=400, detail="Organisation is required")
        _reject_if_exists(
            PCOOrganizationSettings,
            "This organisation already has PCO settings - edit the existing entry instead of creating a new one.",
            where=PCOOrganizationSettings.org_id == org_id,
        )
        if not data.get("pco_token_secret"):
            raise HTTPException(status_code=400, detail="PCO token secret is required")
        data["created_at"] = datetime.now(timezone.utc).isoformat()
        return await super().insert_model(request, data)


class MetaPlatformSettingsAdmin(VisibleIfAccessible, ModelView, model=MetaPlatformSettings):
    """Singleton settings page - org-wide Meta app credentials for
    WhatsApp Embedded Signup (app secret, webhook verify token). Same
    singleton-guard/masked-credential/superadmin-only pattern as
    PCOOrganizationSettingsAdmin above - see that class for the reasoning."""
    column_list = [MetaPlatformSettings.id, MetaPlatformSettings.app_id, MetaPlatformSettings.config_id]
    form_columns = [
        MetaPlatformSettings.app_id, MetaPlatformSettings.app_secret,
        MetaPlatformSettings.config_id, MetaPlatformSettings.webhook_verify_token,
    ]
    form_overrides = {
        "app_secret": PasswordField,
        "webhook_verify_token": PasswordField,
    }
    form_args = {
        "app_secret": {"label": "App Secret", "validators": []},
        "webhook_verify_token": {
            "label": "Webhook Verify Token",
            "validators": [],
            "description": (
                "A string you choose yourself (not issued by Meta) - enter "
                "the same value here and in the App Dashboard's WhatsApp "
                "webhook subscription setup."
            ),
        },
    }
    column_details_exclude_list = [
        MetaPlatformSettings.app_secret, MetaPlatformSettings.webhook_verify_token,
    ]
    can_delete = False
    name = "Meta Platform Settings"
    name_plural = "Meta Platform Settings"
    icon = "fa-solid fa-key"

    def is_accessible(self, request: Request) -> bool:
        return request.session.get("is_superadmin", False)

    async def insert_model(self, request: Request, data: dict) -> Any:
        # Singleton guard, same as PCOOrganizationSettingsAdmin.
        _reject_if_exists(
            MetaPlatformSettings,
            "Meta platform settings already exist - edit the existing entry instead of creating a new one.",
        )
        if not data.get("app_secret"):
            raise HTTPException(status_code=400, detail="App secret is required")
        data["created_at"] = datetime.now(timezone.utc).isoformat()
        return await super().insert_model(request, data)

    async def update_model(self, request: Request, pk: str, data: dict) -> Any:
        _keep_existing_if_blank(data, "app_secret", "webhook_verify_token")
        return await super().update_model(request, pk, data)


def _display_phone_number_display(model: "WhatsAppNumber", attribute) -> str:
    """display_phone_number is populated automatically from Meta on every
    create/save of this row (see WhatsAppNumberAdmin._sync_display_number
    below) and via Embedded Signup (onboarding_router.py) - staff never
    type it in. It can still be blank: a row saved without a valid
    access_token/phone_number_id yet (e.g. mid-setup), or one that
    predates this feature and hasn't been saved or backfilled via POST
    /ops/sync-phone-numbers since. Say so instead of rendering a blank
    cell that reads as a data-loss bug."""
    return model.display_phone_number or "Not synced yet"


def _reserve_percent_display(model: "WhatsAppNumber", attribute) -> str:
    """campaign_reserve_percent is nullable - NULL means "no override, use
    the app-wide CAMPAIGN_RESERVE_FRACTION default" (see
    whatsapp_limits.reserve_fraction_for). Rendering that as a blank cell
    would look like the number has no reserve at all, so this spells out
    the effective value either way."""
    value = model.campaign_reserve_percent
    if value is None:
        return f"{int(CAMPAIGN_RESERVE_FRACTION * 100)}% (default)"
    return f"{value}%"


class WhatsAppNumberAdmin(ScopedModelView, model=WhatsAppNumber):
    identity = "whatsapp-numbers"
    # Custom template pairs Label/Phone Number ID and WABA ID/Meta App ID
    # onto shared lines instead of every field getting its own full-width
    # row - see whatsapp_number_edit.html.
    edit_template = "sqladmin/whatsapp_number_edit.html"
    column_list = [
        WhatsAppNumber.unit, WhatsAppNumber.label,
        WhatsAppNumber.display_phone_number,
        WhatsAppNumber.active, WhatsAppNumber.send_delay_seconds,
        WhatsAppNumber.send_concurrency, WhatsAppNumber.campaign_reserve_percent,
    ]
    column_formatters = {
        WhatsAppNumber.campaign_reserve_percent: _reserve_percent_display,
        WhatsAppNumber.display_phone_number: _display_phone_number_display,
    }
    column_formatters_detail = {
        WhatsAppNumber.campaign_reserve_percent: _reserve_percent_display,
        WhatsAppNumber.display_phone_number: _display_phone_number_display,
    }
    column_labels = {
        WhatsAppNumber.unit: "Unit",
        WhatsAppNumber.label: "Label",
        WhatsAppNumber.phone_number_id: "Phone Number ID",
        WhatsAppNumber.display_phone_number: "Phone Number",
        WhatsAppNumber.waba_id: "WABA ID",
        WhatsAppNumber.meta_app_id: "Meta App ID",
        WhatsAppNumber.active: "Active",
        WhatsAppNumber.send_delay_seconds: "Send Delay (seconds)",
        WhatsAppNumber.send_concurrency: "Concurrent Sends",
        WhatsAppNumber.campaign_reserve_percent: "Campaign Reserve %",
        WhatsAppNumber.onboarded_via: "Onboarded Via",
        WhatsAppNumber.created_at: "Created At",
    }
    column_sortable_list = ["unit.name"]
    form_columns = [
        WhatsAppNumber.unit, WhatsAppNumber.label, WhatsAppNumber.phone_number_id,
        WhatsAppNumber.access_token, WhatsAppNumber.waba_id, WhatsAppNumber.meta_app_id,
        WhatsAppNumber.active,
        WhatsAppNumber.send_delay_seconds, WhatsAppNumber.send_concurrency,
        WhatsAppNumber.campaign_reserve_percent,
    ]
    # display_phone_number is deliberately NOT a form field - insert_model/
    # update_model below fetch it from Meta automatically (using whatever
    # phone_number_id + access_token the save just submitted) rather than
    # asking staff to type it in or run the ops endpoint by hand. It still
    # shows read-only on the list/details pages via column_formatters above.
    # access_token is a live credential - SQLAdmin's list view already
    # excludes it (column_list above), but the separate Details view
    # (clicking into a row) has no such exclusion by default and would
    # otherwise render the live decrypted token in plaintext, since
    # EncryptedString transparently decrypts on any ORM read. id and
    # unit_id are dropped too - the raw numeric id is meaningless
    # to staff, and unit_id duplicates the Unit link
    # already shown via the unit relationship above it.
    # Explicit order (rather than column_details_exclude_list) so
    # display_phone_number ("Phone Number") can be positioned right after
    # Label instead of falling where it's declared on the model (near the
    # bottom, after send_delay_seconds/send_concurrency/etc).
    column_details_list = [
        WhatsAppNumber.unit, WhatsAppNumber.label,
        WhatsAppNumber.display_phone_number, WhatsAppNumber.phone_number_id,
        WhatsAppNumber.waba_id, WhatsAppNumber.meta_app_id,
        WhatsAppNumber.active, WhatsAppNumber.send_delay_seconds,
        WhatsAppNumber.send_concurrency, WhatsAppNumber.campaign_reserve_percent,
        WhatsAppNumber.onboarded_via, WhatsAppNumber.created_at,
    ]
    # access_token is a live WhatsApp credential - same treatment as
    # User.password_hash below: masked password-style input, and the
    # DB row is never re-rendered with the plaintext value on the edit
    # form. Leaving it blank on edit keeps the existing token; the
    # EncryptedString column type (see above) still transparently encrypts
    # whatever value does get written, this just stops the current token
    # from being echoed back in cleartext HTML every time someone opens
    # the edit page.
    form_overrides = {
        "access_token": PasswordField,
        "active": BooleanField,
    }
    form_args = {
        "access_token": {"label": "Access Token", "validators": []},
        "active": _checkbox_render_kw(),
        "send_delay_seconds": {
            "label": "Delay Between Messages (seconds)",
            "validators": [NumberRange(min=0, max=10)],
            "description": "Lower = faster sends, higher = safer for newer numbers.",
        },
        "send_concurrency": {
            "label": "Concurrent Sends",
            # Upper-bounded at 40 to line up with Meta's documented 20
            # msg/s ceiling - this isn't a literal
            # messages-per-second cap (that also depends on per-message
            # latency, see campaign_runner.py), just a guardrail against
            # someone setting a batch size that could plausibly outrun it.
            "validators": [NumberRange(min=1, max=40)],
            "description": "Default 20. Raise if this number can sustain more, lower if sends start failing.",
        },
        "campaign_reserve_percent": {
            "label": "Campaign Reserve %",
            # Optional() must come first - it's what lets a blank submission
            # coerce to None (falling back to the app-wide default) instead
            # of WTForms rejecting the empty string as "not a valid integer"
            # before NumberRange ever runs.
            "validators": [Optional(), NumberRange(min=0, max=100)],
            "description": "Leave blank to use the app-wide default (5%).",
        },
    }
    name = "WhatsApp Number"
    name_plural = "WhatsApp Numbers"
    icon = "fa-solid fa-phone"

    # No is_accessible/is_visible override, unlike UnitAdmin -
    # intentional, confirmed: unit-scoped staff CAN see and edit
    # WhatsAppNumber rows (including access_token) for their own
    # unit(s), not just superadmins. ScopedModelView's row-level
    # scoping (unit_field="id" default -> "unit_id" here)
    # is what limits which numbers a non-superadmin can reach; there is no
    # additional superadmin gate on top of that for this view, by design.
    # (A UnitAdmin-style superadmin-only comment used to sit here
    # describing a check that was never actually wired up - this is the
    # real, confirmed policy.)

    def _related_field_linkable(self, request: Request, name: str) -> bool:
        # UnitAdmin (identity "unit") is superadmin/org-admin only (see its
        # is_accessible) - linking to /unit/details/<pk> from here would
        # 403 for plain staff even when it's their own unit. Fall back to
        # plain text for them instead of a dead-end link; org-admins/
        # superadmins keep the working link.
        if name == "unit":
            return request.session.get("is_superadmin", False) or request.session.get("is_org_admin", False)
        return True

    def _sync_display_number(self, data: dict, access_token: str | None, phone_number_id: str | None) -> None:
        """Fetches display_phone_number from Meta and stashes it in `data`
        so the upcoming insert/update writes it in the same save - staff
        never type this in themselves (removed from form_columns above).
        Only sets the key on success: a transient Graph failure should
        leave whatever's already on the row alone rather than blanking it
        out (same "keep the last good value" philosophy as
        sync_quality_from_meta/sync_tier_from_meta)."""
        if not access_token or not phone_number_id:
            return
        display_number = sync_display_number_from_meta(access_token, phone_number_id)
        if display_number:
            data["display_phone_number"] = display_number

    async def insert_model(self, request: Request, data: dict) -> Any:
        # created_at is NOT NULL in the DB but wasn't being set here before
        # this change - same pre-existing gap as UnitAdmin above.
        data["created_at"] = datetime.now(timezone.utc).isoformat()
        self._sync_display_number(data, data.get("access_token"), data.get("phone_number_id"))
        return await super().insert_model(request, data)

    async def update_model(self, request: Request, pk: str, data: dict) -> Any:
        _keep_existing_if_blank(data, "access_token")
        access_token = data.get("access_token")
        phone_number_id = data.get("phone_number_id")
        if not access_token or not phone_number_id:
            # Access token left blank (keeping the existing one) and/or
            # phone_number_id unchanged from what's already on the row -
            # either way, re-fetch whichever of the two the form didn't
            # supply from the existing row rather than skipping the sync.
            with Session(engine) as session:
                existing = session.get(WhatsAppNumber, int(pk))
            if existing:
                access_token = access_token or existing.access_token
                phone_number_id = phone_number_id or existing.phone_number_id
        self._sync_display_number(data, access_token, phone_number_id)
        return await super().update_model(request, pk, data)



class UserAdmin(VisibleIfAccessible, OrgScopedModelView, model=User):
    column_list = [
        User.id, User.organisation, User.username,
        User.is_superadmin, User.is_org_admin, User.active,
    ]
    column_labels = {
        User.units: "Units",
        User.organisation: "Organisation",
        User.id: "ID",
        User.username: "Username",
        User.is_superadmin: "Super Admin",
        User.is_org_admin: "Org Admin",
        User.active: "Active",
        User.created_at: "Created At"
    }
    column_sortable_list = [User.username]
    # SQLAdmin wraps to-many relationship values in parens by default
    # ("(Unit Name)") when show_compact_lists is on - same reason
    # UnitAdmin turns it off for its own to-many relation above.
    show_compact_lists = False
    # User.units (many-to-many via user_units)
    # already existed on the model but was never exposed on the form -
    # SQLAdmin renders a many-to-many relationship as a multi-select
    # automatically once it's listed here. Note there is no independent
    # per-number assignment: a user with access to a unit sees
    # every WhatsApp number under it (_accessible_numbers() in
    # campaigns_router.py), there's no separate numbers-only scoping.
    form_columns = [
        User.organisation, User.username, User.password_hash,
        User.is_superadmin, User.is_org_admin,
        User.active, User.units,
    ]
    # password_hash is a bcrypt hash - there's no legitimate reason to
    # display it on the Details page (it can't be reversed, and it's not
    # useful for support/debugging the way a live API credential can be),
    # so it's dropped there entirely rather than just masked. It still
    # appears on the *edit* form since that's how a new password is set.
    # org_id is dropped too - the Organisation relationship column already
    # shows the org by name, so the raw id is redundant.
    column_details_exclude_list = [User.password_hash, User.org_id]
    form_overrides = {
        "password_hash": PasswordField,
        "is_superadmin": BooleanField,
        "is_org_admin": BooleanField,
        "active": BooleanField,
        "units": CheckboxQuerySelectMultipleField,
    }
    form_args = {
        "password_hash": {"label": "Password", "validators": []},
        "is_superadmin": _checkbox_render_kw(),
        "is_org_admin": _checkbox_render_kw(),
        "active": _checkbox_render_kw(),
    }
    name = "User"
    name_plural = "Users"
    icon = "fa-solid fa-user-shield"
    # Hand-laid-out field order/pairing - see user_form_fields.html: password
    # comes right after username, Organisation and Units sit side by side
    # below it, and the three admin toggles share one row.
    edit_template = "sqladmin/user_edit.html"
    create_template = "sqladmin/user_create.html"

    def is_accessible(self, request: Request) -> bool:
        # Superadmins manage every org's staff; org admins manage their own
        # org's staff, including promoting another user to org admin - but
        # never to superadmin (enforced in insert_model/update_model below,
        # not just by hiding the field - never trust the client).
        return request.session.get("is_superadmin", False) or request.session.get("is_org_admin", False)

    def _restrict_units_to_org(self, org_id: int | None, data: dict) -> None:
        """Real boundary for the Units picker, same "never trust the
        client" principle already applied to org_id/is_superadmin above -
        scaffold_form's filtering below is UX/defense in depth only (same
        split as ScopedModelView.scaffold_form's own docstring in
        admin_scoping.py). Without this, a crafted POST naming another
        organisation's unit id would still grant the target user real
        access to that org's WhatsApp numbers/campaigns/templates via
        resolve_unit_ids(), even though the dropdown itself never showed
        it. Strips anything outside the caller's own org rather than
        rejecting the whole request, so an org admin can still create/edit
        a user with a mix of valid-and-invalid units."""
        if "units" not in data:
            return
        from autosend import storage

        allowed_ids = {str(i) for i in storage.get_unit_ids_for_org(org_id)} if org_id else set()
        data["units"] = [u for u in (data.get("units") or []) if str(u) in allowed_ids]

    async def scaffold_form(self, rules: list[str] | None = None):
        """Filters the Units checkbox list down to the caller's own org
        for non-superadmins - mirrors ScopedModelView.scaffold_form's
        unit/organisation filtering (admin_scoping.py), which this view
        never inherited since it scopes by org_id directly rather than
        unit_id. Narrows what's displayed and, via WTForms
        QuerySelectMultipleField's own pre_validate, rejects any submitted
        id outside it - _restrict_units_to_org above is still the real
        boundary, this is not."""
        form_cls = await super().scaffold_form(rules)

        from autosend.admin_auth import current_scope

        scope = current_scope.get()
        if scope is None:
            return form_cls
        is_superadmin, _is_org_admin, org_id, _unit_ids = scope
        if is_superadmin or org_id is None or not hasattr(form_cls, "units"):
            return form_cls

        from autosend import storage

        allowed_ids = storage.get_unit_ids_for_org(org_id)
        with Session(engine) as session:
            units = session.query(Unit).filter(Unit.id.in_(allowed_ids)).all() if allowed_ids else []
        form_cls.units.kwargs["data"] = [(str(u.id), str(u)) for u in units]
        return form_cls

    async def insert_model(self, request: Request, data: dict) -> Any:
        raw_password = data.get("password_hash")
        if not raw_password:
            raise HTTPException(status_code=400, detail="Password is required")
        if not request.session.get("is_superadmin", False):
            # Org admin: force their own org (can't create a user in
            # another org), and strip is_superadmin from the submitted
            # data entirely - not just left unset, actively removed, so a
            # crafted form post can't grant platform-superadmin either.
            data["org_id"] = request.session.get("org_id")
            data.pop("organisation", None)
            data.pop("is_superadmin", None)
            if not data.get("org_id"):
                raise HTTPException(status_code=400, detail="Organisation is required")
            self._restrict_units_to_org(data["org_id"], data)
        elif not data.get("is_superadmin") and not data.get("org_id") and not data.get("organisation"):
            raise HTTPException(
                status_code=400,
                detail="Organisation is required for non-superadmin staff",
            )
        data["password_hash"] = bcrypt.hashpw(raw_password.encode(), bcrypt.gensalt()).decode()
        data["created_at"] = datetime.now(timezone.utc).isoformat()
        return await super().insert_model(request, data)

    async def update_model(self, request: Request, pk: str, data: dict) -> Any:
        # Real boundary, not just the query-scoping above: sqladmin's own
        # update() re-fetches the row by pk alone, bypassing
        # form_edit_query/list_query entirely - so a crafted POST to
        # another org's staff row id must be caught here explicitly.
        self._check_row_scope(request, pk)
        if not request.session.get("is_superadmin", False):
            data["org_id"] = request.session.get("org_id")
            data.pop("organisation", None)
            data.pop("is_superadmin", None)
            self._restrict_units_to_org(data["org_id"], data)
        raw_password = data.get("password_hash")
        if raw_password:
            data["password_hash"] = bcrypt.hashpw(raw_password.encode(), bcrypt.gensalt()).decode()
        else:
            data.pop("password_hash", None)  # blank on edit = keep existing password
        return await super().update_model(request, pk, data)

    async def delete_model(self, request: Request, pk: Any) -> None:
        # Same reasoning as update_model above - Query.delete() also
        # re-fetches by raw pk, bypassing list_query/count_query, so an
        # org admin could otherwise delete another org's staff user
        # outright.
        self._check_row_scope(request, pk)
        await super().delete_model(request, pk)

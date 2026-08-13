"""sqladmin CRUD views (ModelViews) for each unit-scoped or
org-wide table. Page shells (BaseView dashboards) live in admin_pages.py
instead - this file is only the SQLAdmin/CRUD side."""
import re
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
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
from autosend.admin_scoping import ScopedModelView
from autosend.admin_widgets import _checkbox_render_kw, CheckboxQuerySelectMultipleField

def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "unit"


def _flash_slug_change_reminder(request: Request, new_slug: str) -> None:
    """Stashes a one-time reminder in the session for layout.html to render
    on the next page load, naming the PCO webhook URL that now needs
    updating in PCO's own webhook subscription settings. Session-based
    (not a query param) since sqladmin's post-update redirect gives us no
    hook to attach one directly.

    NOT YET WIRED UP: this assumes layout.html has (or will have) a block
    that pops and renders request.session["flash_message"]. Confirm/add
    that before relying on this - see chat for the template snippet."""
    url = f"https://whatsapp.shofaronline.org/webhooks/planning-center/people-form/{new_slug}"
    request.session["flash_message"] = (
        f"This unit's slug changed to '{new_slug}'. "
        f"Update its PCO webhook subscription URL to: {url}"
    )


class OrganisationAdmin(ModelView, model=Organisation):
    """Top of the tenancy hierarchy - superadmin-only, same gating as
    UnitAdmin/UserAdmin below. Deliberately NOT reachable by an org
    admin: there is no "create organisation" action anywhere in the
    logged-in admin for anyone but a superadmin - the only other way an
    organisation gets created is the public self-serve /signup flow
    (web/signup_router.py), which is a distinct identity, not an action
    available from within an existing org admin's own session."""
    column_list = [Organisation.id, Organisation.name, Organisation.slug, Organisation.active]
    column_labels = {
        Organisation.id: "ID",
        Organisation.name: "Name",
        Organisation.slug: "Slug",
        Organisation.active: "Active",
        Organisation.created_at: "Created At",
    }
    column_sortable_list = [Organisation.name]
    form_columns = [Organisation.name, Organisation.active]
    form_overrides = {"active": BooleanField}
    form_args = {"active": _checkbox_render_kw()}
    # Deactivate via `active` instead - hard delete would orphan this
    # org's units/staff (no FK cascade enforcement at the SQLite level for
    # a delete issued through the ORM here).
    can_delete = False
    name = "Organisation"
    name_plural = "Organisations"
    icon = "fa-solid fa-building"

    def is_accessible(self, request: Request) -> bool:
        return request.session.get("is_superadmin", False)

    def is_visible(self, request: Request) -> bool:
        return request.session.get("is_superadmin", False)

    async def insert_model(self, request: Request, data: dict) -> Any:
        data["slug"] = _slugify(data.get("name") or "")
        data["created_at"] = datetime.now(timezone.utc).isoformat()
        return await super().insert_model(request, data)


class UnitAdmin(ScopedModelView, model=Unit):
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
        Unit.name: "Name",
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
    # Same plaintext-exposure gap as WhatsAppNumberAdmin.access_token below:
    # form_overrides only masks the create/edit form. The separate Details
    # view has no exclusion by default and was rendering this live,
    # decrypted secret in plaintext.
    # form_mappings: not relevant to what staff need on this page - dropped
    # from the Details view (the relation itself, and its edit form, are
    # unaffected; this only hides it here).
    column_details_exclude_list = [
        Unit.pco_webhook_secret, Unit.form_mappings,
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

    def is_visible(self, request: Request) -> bool:
        return self.is_accessible(request)

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
        if not data.get("pco_webhook_secret"):
            data.pop("pco_webhook_secret", None)  # blank on edit = keep existing
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


class UnitWebhookAdmin(ScopedModelView, model=Unit):
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
        # Unlike UnitAdmin, open to any logged-in staff member -
        # ScopedModelView's own row-level scoping (unit_field="id")
        # is what actually restricts which unit(s) they can reach,
        # same trust model as WhatsAppNumberAdmin.
        return True

    def is_visible(self, request: Request) -> bool:
        return True

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
        if not data.get("pco_webhook_secret"):
            data.pop("pco_webhook_secret", None)  # blank on edit = keep existing
        return await super().update_model(request, pk, data)


class PCOOrganizationSettingsAdmin(ModelView, model=PCOOrganizationSettings):
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
        return request.session.get("is_superadmin", False) or request.session.get("is_org_admin", False)

    def is_visible(self, request: Request) -> bool:
        return self.is_accessible(request)

    def _apply_org_scope(self, stmt, request: Request):
        if request.session.get("is_superadmin", False):
            return stmt
        return stmt.where(PCOOrganizationSettings.org_id == request.session.get("org_id"))

    def list_query(self, request: Request):
        return self._apply_org_scope(super().list_query(request), request)

    def count_query(self, request: Request):
        return self._apply_org_scope(super().count_query(request), request)

    def form_edit_query(self, request: Request):
        # sqladmin fetches the edit-page object by pk alone (not via
        # list_query), so without this an org admin who guesses another
        # org's row id could still reach its edit page - this 404s it
        # instead, same scope as the list page.
        return self._apply_org_scope(super().form_edit_query(request), request)

    def details_query(self, request: Request):
        return self._apply_org_scope(super().details_query(request), request)

    async def update_model(self, request: Request, pk: str, data: dict) -> Any:
        if not request.session.get("is_superadmin", False):
            # Real boundary, not just the query-scoping above: sqladmin's
            # own update() re-fetches the row by pk alone, bypassing
            # form_edit_query/list_query entirely - so a crafted POST to
            # another org's row id must be caught here explicitly.
            with Session(engine) as session:
                existing = session.get(PCOOrganizationSettings, int(pk))
            if existing is None or existing.org_id != request.session.get("org_id"):
                raise HTTPException(status_code=404, detail="Not found")
            # Same server-side enforcement as insert_model above - an org
            # admin can never move this row to another org.
            data["org_id"] = request.session.get("org_id")
            data.pop("organisation", None)
        if not data.get("pco_token_secret"):
            data.pop("pco_token_secret", None)  # blank on edit = keep existing
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
        # Singleton-per-org guard: the DB's own UNIQUE(org_id) would catch
        # this too, but this gives a clean 400 instead of a raw
        # IntegrityError.
        with Session(engine) as session:
            existing = session.execute(
                select(PCOOrganizationSettings).where(PCOOrganizationSettings.org_id == org_id)
            ).first()
        if existing is not None:
            raise HTTPException(
                status_code=400,
                detail="This organisation already has PCO settings - edit the existing entry instead of creating a new one.",
            )
        if not data.get("pco_token_secret"):
            raise HTTPException(status_code=400, detail="PCO token secret is required")
        data["created_at"] = datetime.now(timezone.utc).isoformat()
        return await super().insert_model(request, data)


class MetaPlatformSettingsAdmin(ModelView, model=MetaPlatformSettings):
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

    def is_visible(self, request: Request) -> bool:
        return request.session.get("is_superadmin", False)

    async def insert_model(self, request: Request, data: dict) -> Any:
        # Singleton guard, same as PCOOrganizationSettingsAdmin.
        with Session(engine) as session:
            existing = session.execute(select(MetaPlatformSettings)).first()
        if existing is not None:
            raise HTTPException(
                status_code=400,
                detail="Meta platform settings already exist - edit the existing entry instead of creating a new one.",
            )
        if not data.get("app_secret"):
            raise HTTPException(status_code=400, detail="App secret is required")
        data["created_at"] = datetime.now(timezone.utc).isoformat()
        return await super().insert_model(request, data)

    async def update_model(self, request: Request, pk: str, data: dict) -> Any:
        if not data.get("app_secret"):
            data.pop("app_secret", None)  # blank on edit = keep existing
        if not data.get("webhook_verify_token"):
            data.pop("webhook_verify_token", None)  # blank on edit = keep existing
        return await super().update_model(request, pk, data)


class WhatsAppNumberAdmin(ScopedModelView, model=WhatsAppNumber):
    identity = "whatsapp-numbers"
    column_list = [
        WhatsAppNumber.unit, WhatsAppNumber.label,
        WhatsAppNumber.is_primary,
        WhatsAppNumber.active, WhatsAppNumber.send_delay_seconds,
        WhatsAppNumber.send_concurrency, WhatsAppNumber.campaign_reserve_percent,
    ]
    column_labels = {
        WhatsAppNumber.unit: "Unit",
        WhatsAppNumber.label: "Label",
        WhatsAppNumber.phone_number_id: "Phone Number ID",
        WhatsAppNumber.waba_id: "WABA ID",
        WhatsAppNumber.meta_app_id: "Meta App ID",
        WhatsAppNumber.is_primary: "Primary",
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
        WhatsAppNumber.is_primary, WhatsAppNumber.active,
        WhatsAppNumber.send_delay_seconds, WhatsAppNumber.send_concurrency,
        WhatsAppNumber.campaign_reserve_percent,
    ]
    # access_token is a live credential - SQLAdmin's list view already
    # excludes it (column_list above), but the separate Details view
    # (clicking into a row) has no such exclusion by default and would
    # otherwise render the live decrypted token in plaintext, since
    # EncryptedString transparently decrypts on any ORM read. id and
    # unit_id are dropped too - the raw numeric id is meaningless
    # to staff, and unit_id duplicates the Unit link
    # already shown via the unit relationship above it.
    column_details_exclude_list = [
        WhatsAppNumber.access_token, WhatsAppNumber.id, WhatsAppNumber.unit_id,
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
        "is_primary": BooleanField,
        "active": BooleanField,
    }
    form_args = {
        "access_token": {"label": "Access Token", "validators": []},
        "is_primary": _checkbox_render_kw(),
        "active": _checkbox_render_kw(),
        "send_delay_seconds": {
            "label": "Delay Between Messages (seconds)",
            "validators": [NumberRange(min=0, max=10)],
            "description": (
                "Pacing between messages during a bulk campaign sent from "
                "this number. Lower = faster sends, higher = more "
                "conservative (helps protect quality rating on newer "
                "numbers)."
            ),
        },
        "send_concurrency": {
            "label": "Concurrent Sends",
            # Upper-bounded at 40 to line up with Meta's documented 20
            # msg/s ceiling - this isn't a literal
            # messages-per-second cap (that also depends on per-message
            # latency, see campaign_runner.py), just a guardrail against
            # someone setting a batch size that could plausibly outrun it.
            "validators": [NumberRange(min=1, max=40)],
            "description": (
                "How many messages this number sends in flight at once "
                "during a bulk campaign. Default of 20 measured ~10 msg/s "
                "in testing; raise if this number can sustain more, lower "
                "if sends start failing or quality drops."
            ),
        },
        "campaign_reserve_percent": {
            "label": "Campaign Reserve %",
            # Optional() must come first - it's what lets a blank submission
            # coerce to None (falling back to the app-wide default) instead
            # of WTForms rejecting the empty string as "not a valid integer"
            # before NumberRange ever runs.
            "validators": [Optional(), NumberRange(min=0, max=100)],
            "description": (
                "% of this number's 24h messaging limit that bulk campaigns "
                "must leave unused, reserved for registration/payment "
                "confirmations. Leave blank to use the app-wide default (5%)."
            ),
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

    async def insert_model(self, request: Request, data: dict) -> Any:
        # created_at is NOT NULL in the DB but wasn't being set here before
        # this change - same pre-existing gap as UnitAdmin above.
        data["created_at"] = datetime.now(timezone.utc).isoformat()
        return await super().insert_model(request, data)

    async def update_model(self, request: Request, pk: str, data: dict) -> Any:
        if not data.get("access_token"):
            data.pop("access_token", None)  # blank on edit = keep existing token
        return await super().update_model(request, pk, data)



class UserAdmin(ModelView, model=User):
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
    column_details_exclude_list = [User.password_hash]
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

    def is_accessible(self, request: Request) -> bool:
        # Superadmins manage every org's staff; org admins manage their own
        # org's staff, including promoting another user to org admin - but
        # never to superadmin (enforced in insert_model/update_model below,
        # not just by hiding the field - never trust the client).
        return request.session.get("is_superadmin", False) or request.session.get("is_org_admin", False)

    def is_visible(self, request: Request) -> bool:
        return self.is_accessible(request)

    def _apply_org_scope(self, stmt, request: Request):
        if request.session.get("is_superadmin", False):
            return stmt
        return stmt.where(User.org_id == request.session.get("org_id"))

    def list_query(self, request: Request):
        return self._apply_org_scope(super().list_query(request), request)

    def count_query(self, request: Request):
        return self._apply_org_scope(super().count_query(request), request)

    def form_edit_query(self, request: Request):
        # sqladmin fetches the edit-page object by pk alone (not via
        # list_query), so without this an org admin who guesses another
        # org's staff row id could still reach its edit page - this 404s
        # it instead, same scope as the list page.
        return self._apply_org_scope(super().form_edit_query(request), request)

    def details_query(self, request: Request):
        return self._apply_org_scope(super().details_query(request), request)

    def _check_row_scope(self, request: Request, pk: str) -> None:
        if request.session.get("is_superadmin", False):
            return
        with Session(engine) as session:
            existing = session.get(User, int(pk))
        if existing is None or existing.org_id != request.session.get("org_id"):
            raise HTTPException(status_code=404, detail="Not found")

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

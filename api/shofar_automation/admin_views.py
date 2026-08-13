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

from shofar_automation.admin_models import (
    engine,
    Unit,
    PCOOrganizationSettings,
    MetaPlatformSettings,
    WhatsAppNumber,
    StaffUser,
)
from shofar_automation.admin_scoping import ScopedModelView
from shofar_automation.admin_widgets import _checkbox_render_kw, CheckboxQuerySelectMultipleField

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
    column_list = [Unit.id, Unit.name, Unit.whatsapp_numbers, Unit.active]
    column_labels = {
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
        Unit.name, Unit.active,
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
        # Only superadmins can create/edit unit credentials themselves
        return request.session.get("is_superadmin", False)

    def is_visible(self, request: Request) -> bool:
        return request.session.get("is_superadmin", False)

    async def insert_model(self, request: Request, data: dict) -> Any:
        data["slug"] = _slugify(data.get("name") or "")
        data["created_at"] = datetime.now(timezone.utc).isoformat()
        return await super().insert_model(request, data)

    async def update_model(self, request: Request, pk: str, data: dict) -> Any:
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
    """Singleton settings page - org-wide PCO Personal Access Token. Not
    scoped by ScopedModelView (there's no unit_id here); gated
    superadmin-only the same way UnitAdmin/WhatsAppNumberAdmin are."""
    column_list = [PCOOrganizationSettings.id, PCOOrganizationSettings.pco_token_id]
    form_columns = [PCOOrganizationSettings.pco_token_id, PCOOrganizationSettings.pco_token_secret]
    form_overrides = {"pco_token_secret": PasswordField}
    form_args = {"pco_token_secret": {"label": "PCO Token Secret", "validators": []}}
    column_details_exclude_list = [PCOOrganizationSettings.pco_token_secret]
    can_delete = False
    name = "PCO Organization Settings"
    name_plural = "PCO Organization Settings"
    icon = "fa-solid fa-key"

    def is_accessible(self, request: Request) -> bool:
        return request.session.get("is_superadmin", False)

    def is_visible(self, request: Request) -> bool:
        return request.session.get("is_superadmin", False)

    async def insert_model(self, request: Request, data: dict) -> Any:
        # Singleton guard: SQLAdmin's own can_create toggle doesn't know
        # about "already has one row", so this checks at insert time
        # instead - the create form otherwise still works normally and
        # would happily create a second row.
        with Session(engine) as session:
            existing = session.execute(select(PCOOrganizationSettings)).first()
        if existing is not None:
            raise HTTPException(
                status_code=400,
                detail="PCO organization settings already exist - edit the existing entry instead of creating a new one.",
            )
        if not data.get("pco_token_secret"):
            raise HTTPException(status_code=400, detail="PCO token secret is required")
        data["created_at"] = datetime.now(timezone.utc).isoformat()
        return await super().insert_model(request, data)

    async def update_model(self, request: Request, pk: str, data: dict) -> Any:
        if not data.get("pco_token_secret"):
            data.pop("pco_token_secret", None)  # blank on edit = keep existing
        return await super().update_model(request, pk, data)


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
    # StaffUser.password_hash below: masked password-style input, and the
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



class StaffUserAdmin(ModelView, model=StaffUser):
    column_list = [StaffUser.id, StaffUser.username, StaffUser.is_superadmin, StaffUser.active]
    column_labels = {
        StaffUser.units: "Units",
        StaffUser.id: "ID",
        StaffUser.username: "Username",
        StaffUser.is_superadmin: "Super Admin",
        StaffUser.active: "Active",
        StaffUser.created_at: "Created At"
    }
    column_sortable_list = [StaffUser.username]
    # SQLAdmin wraps to-many relationship values in parens by default
    # ("(Unit Name)") when show_compact_lists is on - same reason
    # UnitAdmin turns it off for its own to-many relation above.
    show_compact_lists = False
    # StaffUser.units (many-to-many via staff_user_units)
    # already existed on the model but was never exposed on the form -
    # SQLAdmin renders a many-to-many relationship as a multi-select
    # automatically once it's listed here. Note there is no independent
    # per-number assignment: a user with access to a unit sees
    # every WhatsApp number under it (_accessible_numbers() in
    # campaigns_router.py), there's no separate numbers-only scoping.
    form_columns = [
        StaffUser.username, StaffUser.password_hash, StaffUser.is_superadmin,
        StaffUser.active, StaffUser.units,
    ]
    # password_hash is a bcrypt hash - there's no legitimate reason to
    # display it on the Details page (it can't be reversed, and it's not
    # useful for support/debugging the way a live API credential can be),
    # so it's dropped there entirely rather than just masked. It still
    # appears on the *edit* form since that's how a new password is set.
    column_details_exclude_list = [StaffUser.password_hash]
    form_overrides = {
        "password_hash": PasswordField,
        "is_superadmin": BooleanField,
        "active": BooleanField,
        "units": CheckboxQuerySelectMultipleField,
    }
    form_args = {
        "password_hash": {"label": "Password", "validators": []},
        "is_superadmin": _checkbox_render_kw(),
        "active": _checkbox_render_kw(),
    }
    name = "User"
    name_plural = "Users"
    icon = "fa-solid fa-user-shield"

    def is_accessible(self, request: Request) -> bool:
        return request.session.get("is_superadmin", False)

    def is_visible(self, request: Request) -> bool:
        return request.session.get("is_superadmin", False)

    async def insert_model(self, request: Request, data: dict) -> Any:
        raw_password = data.get("password_hash")
        if not raw_password:
            raise HTTPException(status_code=400, detail="Password is required")
        data["password_hash"] = bcrypt.hashpw(raw_password.encode(), bcrypt.gensalt()).decode()
        data["created_at"] = datetime.now(timezone.utc).isoformat()
        return await super().insert_model(request, data)

    async def update_model(self, request: Request, pk: str, data: dict) -> Any:
        raw_password = data.get("password_hash")
        if raw_password:
            data["password_hash"] = bcrypt.hashpw(raw_password.encode(), bcrypt.gensalt()).decode()
        else:
            data.pop("password_hash", None)  # blank on edit = keep existing password
        return await super().update_model(request, pk, data)
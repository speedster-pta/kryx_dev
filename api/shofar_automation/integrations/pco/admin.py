"""
integrations/pco/admin.py

SQLAdmin views for PCO settings. Registered into the admin app by the
core composition root, but nav visibility for a given logged-in staff
user follows that org's module enablement — not a separate flag. Fernet
credential exclusion pattern preserved from the parent project:
column_details_exclude_list on Details view, form_overrides =
PasswordField restricted to create/edit only.

These views subclass the core ScopedModelView so org/unit scoping is
inherited rather than reimplemented — PCO admin code depends on core's
scoping primitive, not the other way around, keeping the dependency
direction intact even in the admin layer.
"""

from __future__ import annotations

from wtforms import PasswordField

# ScopedModelView lives in core admin plumbing (admin_scoping.py in the
# parent project); PCO admin views subclass it rather than ModelView
# directly so org/unit row-level filtering is inherited for free.
from shofar_automation.core.admin_scoping import ScopedModelView  # noqa: F401  (placeholder import)


class PcoOrganizationSettingsAdmin(ScopedModelView):
    name = "PCO Organisation Settings"
    name_plural = "PCO Organisation Settings"
    icon = "fa-solid fa-plug"

    column_list = ["id", "org_id", "pco_token_id", "updated_at"]
    column_details_exclude_list = ["pco_token_secret"]
    form_excluded_columns = ["created_at", "updated_at"]
    form_overrides = {"pco_token_secret": PasswordField}

    # Scoping: org admins should only ever see their own org's row.
    # Enforced via ScopedModelView's query hook (current_scope.org_id),
    # not by hiding rows in the template layer only.


class PcoUnitSettingsAdmin(ScopedModelView):
    name = "PCO Unit Settings"
    name_plural = "PCO Unit Settings"
    icon = "fa-solid fa-code-branch"

    column_list = ["id", "unit_id", "pco_campus_id"]
    column_details_exclude_list = ["pco_webhook_secret"]
    form_excluded_columns = ["created_at"]
    form_overrides = {"pco_webhook_secret": PasswordField}


def get_admin_views() -> list:
    """
    Called by the core composition root when wiring up SQLAdmin. Views
    are registered unconditionally at the app level (SQLAdmin doesn't
    support per-request conditional registration cleanly); per-org nav
    visibility is instead handled by hiding the section in the sidebar
    template when `"pco" not in enabled_modules_for_org(current_org_id)`.
    """
    return [PcoOrganizationSettingsAdmin, PcoUnitSettingsAdmin]

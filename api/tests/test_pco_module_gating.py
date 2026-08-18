"""Coverage for PCO-module gating: an org without the "pco" module
enabled must not be able to reach the Automations or PCO Webhook
page/nav/API, and enabling/disabling it must take effect immediately (no
restart needed).

See storage/modules.py (grant/enable/disable), web/auth.py
(pco_module_visible - the single check shared by AutomationsView,
UnitWebhookAdmin, layout.html's nav links, and automations_router.py's
dependency gate), and admin_pages.py's ModulesView routes.
"""
from autosend import storage


class TestAutomationsHiddenWithoutModule:
    """A fresh tenant (from the `tenants` fixture) has PCO neither granted
    nor enabled - the default, pre-onboarding state."""

    def test_org_admin_cannot_reach_automations_page(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/automations")
        assert resp.status_code == 403

    def test_org_admin_cannot_call_automations_api(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/api/automations/units")
        assert resp.status_code == 403

    def test_nav_link_absent_from_other_pages(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/campaigns")
        assert resp.status_code == 200
        assert 'href="/automations"' not in resp.text

    def test_superadmin_always_sees_automations(self, client, login_as, tenants, superadmin_username):
        tenant_a, _tenant_b = tenants
        login_as(client, superadmin_username)
        resp = client.get("/automations")
        assert resp.status_code == 200


class TestPCOWebhookHiddenWithoutModule:
    """UnitWebhookAdmin (/pco-webhook/*) is open to any logged-in staff,
    not just org-admins - but is just as pointless without the module
    enabled as Automations is, since the webhook route itself 404s
    regardless of secret (see integrations/webhooks.py)."""

    def test_plain_staff_cannot_reach_pco_webhook_page(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.staff_username)
        resp = client.get("/pco-webhook/list")
        assert resp.status_code == 403

    def test_nav_link_absent_for_plain_staff(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.staff_username)
        resp = client.get("/campaigns")
        assert 'href="/pco-webhook/list"' not in resp.text

    def test_visible_and_reachable_once_enabled(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        storage.grant(tenant_a.org_id, storage.MODULE_PCO)
        storage.enable(tenant_a.org_id, storage.MODULE_PCO)

        login_as(client, tenant_a.staff_username)
        resp = client.get("/campaigns")
        assert 'href="/pco-webhook/list"' in resp.text

        resp = client.get("/pco-webhook/list")
        assert resp.status_code == 200

    def test_superadmin_always_sees_pco_webhook(self, client, login_as, tenants, superadmin_username):
        tenant_a, _tenant_b = tenants
        login_as(client, superadmin_username)
        resp = client.get("/pco-webhook/list")
        assert resp.status_code == 200


class TestAutomationsVisibleOnceEnabled:
    def test_org_admin_can_reach_automations_once_granted_and_enabled(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        storage.grant(tenant_a.org_id, storage.MODULE_PCO)
        storage.enable(tenant_a.org_id, storage.MODULE_PCO)

        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/automations")
        assert resp.status_code == 200

        resp = client.get("/api/automations/units")
        assert resp.status_code == 200

        resp = client.get("/campaigns")
        assert 'href="/automations/pco"' in resp.text

    def test_other_org_stays_gated(self, client, login_as, tenants):
        """Enabling tenant_a's module must not leak visibility to tenant_b."""
        tenant_a, tenant_b = tenants
        storage.grant(tenant_a.org_id, storage.MODULE_PCO)
        storage.enable(tenant_a.org_id, storage.MODULE_PCO)

        login_as(client, tenant_b.org_admin_username)
        resp = client.get("/automations")
        assert resp.status_code == 403


class TestModuleToggleTakesEffectImmediately:
    """storage/modules.py's own docstring promises toggling takes effect
    "immediately everywhere ... without a restart" - these exercise that
    through the actual /modules routes, not just the storage layer."""

    def test_enable_disable_round_trip_via_org_admin(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        storage.grant(tenant_a.org_id, storage.MODULE_PCO)

        login_as(client, tenant_a.org_admin_username)

        # Not yet enabled - still gated even though granted.
        resp = client.get("/automations")
        assert resp.status_code == 403

        resp = client.post(
            "/modules/toggle",
            data={"module_key": storage.MODULE_PCO, "action": "enable"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert client.get("/automations").status_code == 200

        resp = client.post(
            "/modules/toggle",
            data={"module_key": storage.MODULE_PCO, "action": "disable"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert client.get("/automations").status_code == 403

    def test_org_admin_cannot_toggle_another_orgs_module(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        storage.grant(tenant_b.org_id, storage.MODULE_PCO)

        login_as(client, tenant_a.org_admin_username)
        client.post(
            "/modules/toggle",
            data={"org_id": tenant_b.org_id, "module_key": storage.MODULE_PCO, "action": "enable"},
            follow_redirects=False,
        )
        # org_id from the posted form must be ignored for a non-superadmin -
        # tenant_a has no grant at all, so this must fail, not silently
        # enable tenant_b's module.
        assert not storage.is_enabled(tenant_b.org_id, storage.MODULE_PCO)

    def test_toggle_refuses_ungranted_module(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.post(
            "/modules/toggle",
            data={"module_key": storage.MODULE_PCO, "action": "enable"},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert not storage.is_enabled(tenant_a.org_id, storage.MODULE_PCO)


class TestPcoSettingsPageGatedByModule:
    """/pco-settings (admin_org_pages.PcoSettingsView) used to only check
    is_superadmin/is_org_admin, so an org admin whose org wasn't
    granted/enabled for PCO could still reach it - both the nav link and
    the route itself needed the same pco_module_visible gate the rest of
    the PCO surface already has."""

    def test_org_admin_without_module_cannot_reach_page(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/pco-settings")
        assert resp.status_code == 403

    def test_nav_link_absent_for_org_admin_without_module(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/campaigns")
        assert resp.status_code == 200
        assert 'href="/pco-settings"' not in resp.text

    def test_nav_link_present_once_enabled(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        storage.grant(tenant_a.org_id, storage.MODULE_PCO)
        storage.enable(tenant_a.org_id, storage.MODULE_PCO)
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/campaigns")
        assert 'href="/pco-settings"' in resp.text
        assert client.get("/pco-settings").status_code == 200

    def test_superadmin_always_reaches_page(self, client, login_as, tenants, superadmin_username):
        tenant_a, _tenant_b = tenants
        login_as(client, superadmin_username)
        resp = client.get(f"/pco-settings?org_id={tenant_a.org_id}")
        assert resp.status_code == 200


class TestRawPcoOrganizationSettingsCrudGatedByModule:
    """/pco-settings/list, /pco-settings/edit/{pk}, /pco-settings/create -
    the raw sqladmin CRUD screen over PCOOrganizationSettings
    (admin_views.PCOOrganizationSettingsAdmin, identity="pco-settings",
    set in admin.py). Unlinked from nav (superseded by the friendlier
    PcoSettingsView above, which shares the "/pco-settings" URL prefix but
    not any literal path), yet still directly reachable and, until now,
    not gated on PCO module enablement at all - an org admin could set up
    an org's PCO token here even though their org was never
    granted/enabled for PCO."""

    def test_org_admin_without_module_cannot_reach_list(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/pco-settings/list")
        assert resp.status_code == 403

    def test_org_admin_without_module_cannot_reach_edit_page(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.get(f"/pco-settings/edit/{tenant_a.pco_settings_id}")
        assert resp.status_code == 403

    def test_org_admin_without_module_cannot_create(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.post(
            "/pco-settings/create",
            data={"organisation": str(tenant_a.org_id), "pco_token_id": "x", "pco_token_secret": "y"},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_org_admin_can_reach_list_once_enabled(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        storage.grant(tenant_a.org_id, storage.MODULE_PCO)
        storage.enable(tenant_a.org_id, storage.MODULE_PCO)
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/pco-settings/list")
        assert resp.status_code == 200

    def test_superadmin_always_reaches_list(self, client, login_as, tenants, superadmin_username):
        tenant_a, _tenant_b = tenants
        login_as(client, superadmin_username)
        resp = client.get("/pco-settings/list")
        assert resp.status_code == 200


class TestUnitFormPcoFieldsGatedByModule:
    """UnitAdmin's create/edit form (/unit/edit/{pk}) always included the
    PCO Webhook Secret and PCO Campus ID fields, even for a unit whose org
    was never granted/enabled for PCO - clutter/confusion for an org admin
    who has no PCO integration to configure. UnitAdmin.scaffold_form drops
    both fields in that case for an org admin, and - via
    admin_auth.current_edit_pk - for a superadmin editing that same unit
    too, since a superadmin's own session has no single org to check
    against. The one case that keeps both fields regardless is the
    *create* form: there's no target unit/org yet to look up at the point
    scaffold_form runs, for either role."""

    def test_org_admin_without_module_does_not_see_pco_fields(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.get(f"/unit/edit/{tenant_a.unit_id}")
        assert resp.status_code == 200
        assert "pco_webhook_secret" not in resp.text
        assert "pco_campus_id" not in resp.text

    def test_org_admin_with_module_sees_pco_fields(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        storage.grant(tenant_a.org_id, storage.MODULE_PCO)
        storage.enable(tenant_a.org_id, storage.MODULE_PCO)
        login_as(client, tenant_a.org_admin_username)
        resp = client.get(f"/unit/edit/{tenant_a.unit_id}")
        assert resp.status_code == 200
        assert "pco_webhook_secret" in resp.text
        assert "pco_campus_id" in resp.text

    def test_superadmin_does_not_see_pco_fields_when_editing_unprovisioned_unit(
        self, client, login_as, tenants, superadmin_username
    ):
        tenant_a, _tenant_b = tenants
        login_as(client, superadmin_username)
        resp = client.get(f"/unit/edit/{tenant_a.unit_id}")
        assert resp.status_code == 200
        assert "pco_webhook_secret" not in resp.text
        assert "pco_campus_id" not in resp.text

    def test_superadmin_sees_pco_fields_once_enabled(self, client, login_as, tenants, superadmin_username):
        tenant_a, _tenant_b = tenants
        storage.grant(tenant_a.org_id, storage.MODULE_PCO)
        storage.enable(tenant_a.org_id, storage.MODULE_PCO)
        login_as(client, superadmin_username)
        resp = client.get(f"/unit/edit/{tenant_a.unit_id}")
        assert resp.status_code == 200
        assert "pco_webhook_secret" in resp.text
        assert "pco_campus_id" in resp.text

    def test_superadmin_does_not_see_pco_fields_on_create_form(self, client, login_as, superadmin_username):
        """Superadmin's create form has no unit yet, hence no org to
        check - and thus no org whose organisation dropdown they haven't
        even picked from yet could be confirmed to have PCO enabled.
        Hidden rather than shown for a guess that might be wrong; visible
        again on "Save and continue editing" if the org they picked does
        have it enabled (see test_superadmin_sees_pco_fields_once_enabled
        above, which exercises exactly that follow-up edit page)."""
        login_as(client, superadmin_username)
        resp = client.get("/unit/create")
        assert resp.status_code == 200
        assert "pco_webhook_secret" not in resp.text
        assert "pco_campus_id" not in resp.text

    def test_org_admin_without_module_does_not_see_pco_fields_on_create_form(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/unit/create")
        assert resp.status_code == 200
        assert "pco_webhook_secret" not in resp.text
        assert "pco_campus_id" not in resp.text

    def test_org_admin_with_module_sees_pco_fields_on_create_form(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        storage.grant(tenant_a.org_id, storage.MODULE_PCO)
        storage.enable(tenant_a.org_id, storage.MODULE_PCO)
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/unit/create")
        assert resp.status_code == 200
        assert "pco_webhook_secret" in resp.text
        assert "pco_campus_id" in resp.text


class TestUnitDetailsPcoFieldsGatedByModule:
    """UnitAdmin's read-only Details page (/unit/details/{pk}) always
    showed the "PCO Webhook User"/"PCO Campus ID" rows, even for a unit
    whose org was never granted/enabled for PCO. Unlike the edit-form
    fields above, this applies to superadmins too - Details is read-only,
    so there's no setup reason to keep them, and it checks the specific
    unit's own org (not the viewer's session org), since a superadmin can
    view any org's unit."""

    def test_org_admin_without_module_does_not_see_pco_rows(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.get(f"/unit/details/{tenant_a.unit_id}")
        assert resp.status_code == 200
        assert "PCO Webhook User" not in resp.text
        assert "PCO Campus ID" not in resp.text

    def test_org_admin_with_module_sees_pco_rows(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        storage.grant(tenant_a.org_id, storage.MODULE_PCO)
        storage.enable(tenant_a.org_id, storage.MODULE_PCO)
        login_as(client, tenant_a.org_admin_username)
        resp = client.get(f"/unit/details/{tenant_a.unit_id}")
        assert resp.status_code == 200
        assert "PCO Webhook User" in resp.text
        assert "PCO Campus ID" in resp.text

    def test_superadmin_does_not_see_pco_rows_when_not_enabled(self, client, login_as, tenants, superadmin_username):
        tenant_a, _tenant_b = tenants
        login_as(client, superadmin_username)
        resp = client.get(f"/unit/details/{tenant_a.unit_id}")
        assert resp.status_code == 200
        assert "PCO Webhook User" not in resp.text
        assert "PCO Campus ID" not in resp.text

    def test_superadmin_sees_pco_rows_once_enabled(self, client, login_as, tenants, superadmin_username):
        tenant_a, _tenant_b = tenants
        storage.grant(tenant_a.org_id, storage.MODULE_PCO)
        storage.enable(tenant_a.org_id, storage.MODULE_PCO)
        login_as(client, superadmin_username)
        resp = client.get(f"/unit/details/{tenant_a.unit_id}")
        assert resp.status_code == 200
        assert "PCO Webhook User" in resp.text
        assert "PCO Campus ID" in resp.text


class TestSchedulerReflectsModuleState:
    """cancel_org_serving_rule_jobs/reschedule_org_serving_rules - the
    scheduler side-effect wired into ModulesView.toggle (admin_pages.py)."""

    def test_disabling_module_cancels_live_serving_rule_job(self, client, login_as, tenants):
        from autosend.scheduler import scheduler, schedule_serving_rule, _serving_rule_job_id

        tenant_a, _tenant_b = tenants
        storage.grant(tenant_a.org_id, storage.MODULE_PCO)
        storage.enable(tenant_a.org_id, storage.MODULE_PCO)

        rule_id = storage.upsert_serving_rule(
            rule_id=None, unit_id=tenant_a.unit_id, pco_service_type_id="svc-1",
            pco_service_type_name="Sunday Service", send_day_of_week="sun",
            send_time="08:00", timezone_name="Africa/Johannesburg",
            status_filter="confirmed_only", template_name="reminder_template",
            body_variable_order=[], whatsapp_number_id=None, button_variables=[],
            header_image_url=None, active=True,
        )
        rule = storage.get_serving_rule_by_id(rule_id)
        schedule_serving_rule(rule)
        assert scheduler.get_job(_serving_rule_job_id(rule_id)) is not None

        login_as(client, tenant_a.org_admin_username)
        client.post(
            "/modules/toggle",
            data={"module_key": storage.MODULE_PCO, "action": "disable"},
            follow_redirects=False,
        )

        assert scheduler.get_job(_serving_rule_job_id(rule_id)) is None

"""Coverage for the email-to-WhatsApp admin UI: module gating on the
Automations page's Email-to-WhatsApp section and the Email-to-WhatsApp
Settings page, plus cross-org isolation for web/email_wa_router.py's API
and admin_org_pages.EmailWaSettingsView - same pattern as
test_pco_module_gating.py and test_cross_org_isolation.py's PCO cases,
applied to the independent email_wa module.
"""
from autosend import storage


def _grant_and_enable(org_id: int) -> None:
    storage.grant(org_id, storage.MODULE_EMAIL_WA)
    storage.enable(org_id, storage.MODULE_EMAIL_WA)


class TestAutomationsGatedByEmailWaModule:
    def test_org_admin_cannot_reach_automations_without_either_module(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/automations")
        assert resp.status_code == 403

    def test_org_admin_can_reach_automations_with_only_email_wa_enabled(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        _grant_and_enable(tenant_a.org_id)
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/automations")
        assert resp.status_code == 200
        assert "Email-to-WhatsApp" in resp.text
        # PCO sections must not render at all - the org has no PCO module.
        assert "Free Registrations Automations" not in resp.text

    def test_nav_link_present_with_only_email_wa_enabled(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        _grant_and_enable(tenant_a.org_id)
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/campaigns")
        assert 'href="/automations"' in resp.text

    def test_api_gated_independently_of_pco(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/api/email-wa/providers")
        assert resp.status_code == 403

        _grant_and_enable(tenant_a.org_id)
        resp = client.get("/api/email-wa/providers")
        assert resp.status_code == 200
        payload = resp.json()
        assert any(p["key"] == "sme_metrics" for p in payload)

    def test_superadmin_always_sees_automations(self, client, login_as, tenants, superadmin_username):
        tenant_a, _tenant_b = tenants
        login_as(client, superadmin_username)
        resp = client.get("/automations")
        assert resp.status_code == 200


class TestEmailWaSettingsPageGatedByModule:
    def test_org_admin_without_module_cannot_reach_page(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/email-wa-settings")
        assert resp.status_code == 403

    def test_nav_link_absent_without_module(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/campaigns")
        assert 'href="/email-wa-settings"' not in resp.text

    def test_nav_link_present_and_reachable_once_enabled(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        _grant_and_enable(tenant_a.org_id)
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/campaigns")
        assert 'href="/email-wa-settings"' in resp.text
        assert client.get("/email-wa-settings").status_code == 200

    def test_plain_staff_cannot_reach_page_even_when_enabled(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        _grant_and_enable(tenant_a.org_id)
        login_as(client, tenant_a.staff_username)
        resp = client.get("/email-wa-settings")
        assert resp.status_code == 403

    def test_superadmin_always_reaches_page(self, client, login_as, tenants, superadmin_username):
        tenant_a, _tenant_b = tenants
        login_as(client, superadmin_username)
        resp = client.get(f"/email-wa-settings?org_id={tenant_a.org_id}")
        assert resp.status_code == 200


class TestEmailWaApiCrossOrgIsolation:
    def _make_integration(self, tenant, email_type="booking_request"):
        return storage.upsert_email_integration(
            unit_id=tenant.unit_id,
            provider_key="sme_metrics",
            email_type=email_type,
            template_name="booking_template",
            body_variable_order=["name", "phone"],
            whatsapp_number_id=tenant.number_id,
            button_variables=[],
            header_image_url=None,
            active=True,
        )

    def test_list_excludes_other_orgs_integration(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        _grant_and_enable(tenant_a.org_id)
        _grant_and_enable(tenant_b.org_id)
        self._make_integration(tenant_a)
        self._make_integration(tenant_b)

        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/api/email-wa/integrations")
        assert resp.status_code == 200
        unit_ids = {row["unit_id"] for row in resp.json()}
        assert unit_ids == {tenant_a.unit_id}

    def test_create_rejects_another_orgs_unit_id(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        _grant_and_enable(tenant_a.org_id)

        login_as(client, tenant_a.org_admin_username)
        resp = client.post(
            "/api/email-wa/integrations",
            json={
                "unit_id": tenant_b.unit_id,
                "provider_key": "sme_metrics",
                "email_type": "booking_request",
                "template_name": "booking_template",
                "body_variable_order": ["name", "phone"],
                "whatsapp_number_id": tenant_b.number_id,
                "button_variables": [],
                "active": True,
            },
        )
        assert resp.status_code == 403
        assert storage.list_email_integrations([tenant_b.unit_id]) == []

    def test_create_rejects_unknown_provider_or_email_type(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        _grant_and_enable(tenant_a.org_id)
        login_as(client, tenant_a.org_admin_username)

        resp = client.post(
            "/api/email-wa/integrations",
            json={
                "unit_id": tenant_a.unit_id,
                "provider_key": "not_a_real_provider",
                "email_type": "booking_request",
                "template_name": "t",
                "whatsapp_number_id": tenant_a.number_id,
                "active": True,
            },
        )
        assert resp.status_code == 400

        resp = client.post(
            "/api/email-wa/integrations",
            json={
                "unit_id": tenant_a.unit_id,
                "provider_key": "sme_metrics",
                "email_type": "booking_confirmed",  # not registered - see sme_metrics.py
                "template_name": "t",
                "whatsapp_number_id": tenant_a.number_id,
                "active": True,
            },
        )
        assert resp.status_code == 400

    def test_delete_blocked_for_guessed_id_of_other_org(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        _grant_and_enable(tenant_a.org_id)
        _grant_and_enable(tenant_b.org_id)
        result = self._make_integration(tenant_b)

        login_as(client, tenant_a.org_admin_username)
        resp = client.delete(f"/api/email-wa/integrations/{result['id']}")
        assert resp.status_code == 404
        assert storage.get_email_integration_by_id(result["id"]) is not None

    def test_superadmin_sees_every_orgs_integrations(self, client, login_as, tenants, superadmin_username):
        tenant_a, tenant_b = tenants
        _grant_and_enable(tenant_a.org_id)
        _grant_and_enable(tenant_b.org_id)
        self._make_integration(tenant_a)
        self._make_integration(tenant_b)

        login_as(client, superadmin_username)
        resp = client.get("/api/email-wa/integrations")
        assert resp.status_code == 200
        unit_ids = {row["unit_id"] for row in resp.json()}
        assert {tenant_a.unit_id, tenant_b.unit_id} <= unit_ids


class TestEmailWaSettingsPageIsolation:
    def _make_integration(self, tenant):
        return storage.upsert_email_integration(
            unit_id=tenant.unit_id,
            provider_key="sme_metrics",
            email_type="booking_request",
            template_name="booking_template",
            body_variable_order=["name", "phone"],
            whatsapp_number_id=tenant.number_id,
            button_variables=[],
            header_image_url=None,
            active=True,
        )

    def test_list_excludes_other_orgs_row(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        _grant_and_enable(tenant_a.org_id)
        _grant_and_enable(tenant_b.org_id)
        self._make_integration(tenant_a)
        self._make_integration(tenant_b)

        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/email-wa-settings")
        assert resp.status_code == 200
        assert tenant_a.unit_name in resp.text
        assert tenant_b.unit_name not in resp.text

    def test_delete_blocked_for_guessed_id_of_other_org(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        _grant_and_enable(tenant_a.org_id)
        _grant_and_enable(tenant_b.org_id)
        result = self._make_integration(tenant_b)

        login_as(client, tenant_a.org_admin_username)
        resp = client.post(f"/email-wa-settings/{result['id']}/delete", follow_redirects=False)
        assert resp.status_code == 404
        assert storage.get_email_integration_by_id(result["id"]) is not None

    def test_delete_works_for_own_orgs_row(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        _grant_and_enable(tenant_a.org_id)
        result = self._make_integration(tenant_a)

        login_as(client, tenant_a.org_admin_username)
        resp = client.post(f"/email-wa-settings/{result['id']}/delete", follow_redirects=False)
        assert resp.status_code == 303
        assert storage.get_email_integration_by_id(result["id"]) is None

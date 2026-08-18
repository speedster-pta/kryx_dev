"""Coverage for the new, genuinely generic Email-to-WhatsApp module: an
empty provider registry (integrations/email_wa/providers/PROVIDERS starts
with nothing registered), module gating independent of SME Metrics, and
that its settings/automations pages render a sensible empty state rather
than erroring. See storage/modules.py's MODULE_SME_METRICS docstring for
why this module exists separately from (and reuses the module key
formerly held by) SME Metrics - test_sme_metrics_admin.py covers that
module's own, still fully pre-configured, behaviour.
"""
from types import SimpleNamespace

from autosend import storage
from autosend.integrations.email_wa.providers import EmailTypeSpec


def _grant_and_enable(org_id: int) -> None:
    storage.grant(org_id, storage.MODULE_EMAIL_WA)
    storage.enable(org_id, storage.MODULE_EMAIL_WA)


class TestAutomationsGatedByEmailWaModule:
    def test_org_admin_cannot_reach_page_without_module(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/automations/email-wa")
        assert resp.status_code == 403

    def test_org_admin_can_reach_page_once_enabled(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        _grant_and_enable(tenant_a.org_id)
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/automations/email-wa")
        assert resp.status_code == 200
        assert "Email-to-WhatsApp Automations" in resp.text
        # Empty registry - no SME Metrics content should leak in here.
        assert "smeMetrics" not in resp.text
        assert "No providers configured yet" in resp.text

    def test_granting_sme_metrics_does_not_grant_email_wa(self, client, login_as, tenants):
        """The two modules are independent - see
        storage/modules.py's migrate_legacy_email_wa_module_key docstring
        for why that independence matters."""
        tenant_a, _tenant_b = tenants
        storage.grant(tenant_a.org_id, storage.MODULE_SME_METRICS)
        storage.enable(tenant_a.org_id, storage.MODULE_SME_METRICS)

        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/automations/email-wa")
        assert resp.status_code == 403

    def test_nav_link_present_once_enabled(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        _grant_and_enable(tenant_a.org_id)
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/campaigns")
        assert 'href="/automations/email-wa"' in resp.text

    def test_api_providers_empty(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        _grant_and_enable(tenant_a.org_id)
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/api/email-wa/providers")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_superadmin_always_sees_page(self, client, login_as, tenants, superadmin_username):
        tenant_a, _tenant_b = tenants
        login_as(client, superadmin_username)
        resp = client.get("/automations/email-wa")
        assert resp.status_code == 200


class TestEmailWaSettingsPageGatedByModule:
    def test_org_admin_without_module_cannot_reach_page(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/email-wa-settings")
        assert resp.status_code == 403

    def test_reachable_and_shows_empty_state_once_enabled(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        _grant_and_enable(tenant_a.org_id)
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/email-wa-settings")
        assert resp.status_code == 200
        assert "No integrations configured yet" in resp.text


class TestEmailWaApiCrossOrgIsolationWithARegisteredProvider:
    """PROVIDERS is empty by default (see integrations/email_wa/providers/
    __init__.py), so the create/list/delete flow can't be exercised
    end-to-end through the real API without at least one provider
    registered - these tests temporarily register a minimal fake one
    (monkeypatch reverts it automatically) to prove storage/email_wa.py's
    own tables/isolation logic work, independent of storage/sme_metrics.py's."""

    @staticmethod
    def _fake_provider():
        return SimpleNamespace(
            PROVIDER_KEY="fake_crm",
            LABEL="Fake CRM",
            EMAIL_TYPES={"booked": EmailTypeSpec(label="Booked", fields=["name", "phone"], phone_field="phone")},
        )

    def _register_fake_provider(self, monkeypatch):
        import autosend.integrations.email_wa.providers as providers_module

        monkeypatch.setitem(providers_module.PROVIDERS, "fake_crm", self._fake_provider())

    def _make_integration(self, tenant):
        return storage.upsert_email_wa_integration(
            unit_id=tenant.unit_id,
            provider_key="fake_crm",
            email_type="booked",
            template_name="booking_template",
            body_variable_order=["name", "phone"],
            whatsapp_number_id=tenant.number_id,
            button_variables=[],
            header_image_url=None,
            active=True,
        )

    def test_create_list_delete_round_trip(self, client, login_as, tenants, monkeypatch):
        self._register_fake_provider(monkeypatch)
        tenant_a, _tenant_b = tenants
        _grant_and_enable(tenant_a.org_id)
        login_as(client, tenant_a.org_admin_username)

        resp = client.post(
            "/api/email-wa/integrations",
            json={
                "unit_id": tenant_a.unit_id,
                "provider_key": "fake_crm",
                "email_type": "booked",
                "template_name": "booking_template",
                "body_variable_order": ["name", "phone"],
                "whatsapp_number_id": tenant_a.number_id,
                "button_variables": [],
                "active": True,
            },
        )
        assert resp.status_code == 200
        integration_id = resp.json()["id"]

        resp = client.get("/api/email-wa/integrations")
        assert resp.status_code == 200
        assert {row["id"] for row in resp.json()} == {integration_id}

        resp = client.delete(f"/api/email-wa/integrations/{integration_id}")
        assert resp.status_code == 200
        assert storage.get_email_wa_integration_by_id(integration_id) is None

    def test_list_excludes_other_orgs_integration(self, client, login_as, tenants, monkeypatch):
        self._register_fake_provider(monkeypatch)
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

    def test_delete_blocked_for_guessed_id_of_other_org(self, client, login_as, tenants, monkeypatch):
        self._register_fake_provider(monkeypatch)
        tenant_a, tenant_b = tenants
        _grant_and_enable(tenant_a.org_id)
        _grant_and_enable(tenant_b.org_id)
        result = self._make_integration(tenant_b)

        login_as(client, tenant_a.org_admin_username)
        resp = client.delete(f"/api/email-wa/integrations/{result['id']}")
        assert resp.status_code == 404
        assert storage.get_email_wa_integration_by_id(result["id"]) is not None

    def test_rows_never_show_up_in_sme_metrics_endpoints(self, client, login_as, tenants, monkeypatch):
        """The two modules' tables must never mix - a row created through
        the new generic module's API must not appear via SME Metrics'."""
        self._register_fake_provider(monkeypatch)
        tenant_a, _tenant_b = tenants
        _grant_and_enable(tenant_a.org_id)
        storage.grant(tenant_a.org_id, storage.MODULE_SME_METRICS)
        storage.enable(tenant_a.org_id, storage.MODULE_SME_METRICS)
        self._make_integration(tenant_a)

        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/api/sme-metrics/integrations")
        assert resp.status_code == 200
        assert resp.json() == []

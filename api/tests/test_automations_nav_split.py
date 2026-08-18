"""Coverage for the per-integration Automations nav split: as more
automation-driving modules ship, the old single combined /automations
page (every integration's sub-tabs crammed into one tab bar) becomes
unworkable - especially for a superadmin, who always sees every module
(see web.auth.pco_module_visible/sme_metrics_module_visible/
email_wa_module_visible) and so hits whatever the "most integrations"
case is soonest - today that's all three: PCO, SME Metrics, and the new
generic Email-to-WhatsApp module.

web.auth.visible_automation_modules is the single choke point deciding
the nav shape layout.html renders: no entries hides the Automations nav
item entirely, exactly one renders it as a direct link to that module's
own page (/automations/pco, /automations/sme-metrics, or
/automations/email-wa), and two or more renders a dropdown listing each.
/automations itself is now just a redirect to whichever per-module
page(s) apply, kept for old links/bookmarks (see
admin_pages.AutomationsView).
"""
from autosend import storage


def _grant_and_enable_email_wa(org_id: int) -> None:
    storage.grant(org_id, storage.MODULE_EMAIL_WA)
    storage.enable(org_id, storage.MODULE_EMAIL_WA)


def _grant_and_enable_sme_metrics(org_id: int) -> None:
    storage.grant(org_id, storage.MODULE_SME_METRICS)
    storage.enable(org_id, storage.MODULE_SME_METRICS)


def _grant_and_enable_pco(org_id: int) -> None:
    storage.grant(org_id, storage.MODULE_PCO)
    storage.enable(org_id, storage.MODULE_PCO)


class TestSingleModuleRendersDirectLink:
    def test_pco_only_renders_direct_link_not_dropdown(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        _grant_and_enable_pco(tenant_a.org_id)
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/campaigns")
        assert resp.status_code == 200
        assert 'href="/automations/pco"' in resp.text
        assert 'href="/automations/sme-metrics"' not in resp.text
        assert 'href="/automations/email-wa"' not in resp.text
        # Single-module case is a plain link, not the dropdown button.
        assert "Planning Center Automations" not in resp.text

    def test_sme_metrics_only_renders_direct_link_not_dropdown(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        _grant_and_enable_sme_metrics(tenant_a.org_id)
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/campaigns")
        assert resp.status_code == 200
        assert 'href="/automations/sme-metrics"' in resp.text
        assert 'href="/automations/pco"' not in resp.text
        assert 'href="/automations/email-wa"' not in resp.text
        assert "SME Metrics Automations" not in resp.text

    def test_email_wa_only_renders_direct_link_not_dropdown(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        _grant_and_enable_email_wa(tenant_a.org_id)
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/campaigns")
        assert resp.status_code == 200
        assert 'href="/automations/email-wa"' in resp.text
        assert 'href="/automations/pco"' not in resp.text
        assert 'href="/automations/sme-metrics"' not in resp.text
        assert "Email-to-WhatsApp Automations" not in resp.text


class TestMultipleModulesRenderDropdown:
    def test_org_admin_with_two_modules_sees_dropdown(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        _grant_and_enable_pco(tenant_a.org_id)
        _grant_and_enable_sme_metrics(tenant_a.org_id)
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/campaigns")
        assert resp.status_code == 200
        assert 'href="/automations/pco"' in resp.text
        assert 'href="/automations/sme-metrics"' in resp.text
        assert ">Planning Center</a>" in resp.text
        assert ">SME Metrics</a>" in resp.text

    def test_org_admin_with_all_three_modules_sees_dropdown(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        _grant_and_enable_pco(tenant_a.org_id)
        _grant_and_enable_sme_metrics(tenant_a.org_id)
        _grant_and_enable_email_wa(tenant_a.org_id)
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/campaigns")
        assert resp.status_code == 200
        assert 'href="/automations/pco"' in resp.text
        assert 'href="/automations/sme-metrics"' in resp.text
        assert 'href="/automations/email-wa"' in resp.text
        assert ">Planning Center</a>" in resp.text
        assert ">SME Metrics</a>" in resp.text
        assert ">Email-to-WhatsApp</a>" in resp.text

    def test_superadmin_always_sees_dropdown_with_every_module(self, client, login_as, superadmin_username):
        login_as(client, superadmin_username)
        resp = client.get("/campaigns")
        assert resp.status_code == 200
        assert 'href="/automations/pco"' in resp.text
        assert 'href="/automations/sme-metrics"' in resp.text
        assert 'href="/automations/email-wa"' in resp.text
        assert ">Planning Center</a>" in resp.text
        assert ">SME Metrics</a>" in resp.text
        assert ">Email-to-WhatsApp</a>" in resp.text


class TestPerModulePages:
    def test_pco_page_reachable_and_shows_only_pco_sections(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        _grant_and_enable_pco(tenant_a.org_id)
        _grant_and_enable_sme_metrics(tenant_a.org_id)
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/automations/pco")
        assert resp.status_code == 200
        assert "Free Registrations Automations" in resp.text
        assert "Planning Center Automations" in resp.text

    def test_sme_metrics_page_reachable_and_shows_only_its_sections(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        _grant_and_enable_pco(tenant_a.org_id)
        _grant_and_enable_sme_metrics(tenant_a.org_id)
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/automations/sme-metrics")
        assert resp.status_code == 200
        assert "Free Registrations Automations" not in resp.text
        assert "SME Metrics Automations" in resp.text

    def test_email_wa_page_reachable_and_shows_only_its_sections(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        _grant_and_enable_pco(tenant_a.org_id)
        _grant_and_enable_email_wa(tenant_a.org_id)
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/automations/email-wa")
        assert resp.status_code == 200
        assert "Free Registrations Automations" not in resp.text
        assert "Email-to-WhatsApp Automations" in resp.text

    def test_pco_page_403s_without_pco_module(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        _grant_and_enable_sme_metrics(tenant_a.org_id)
        login_as(client, tenant_a.org_admin_username)
        assert client.get("/automations/pco").status_code == 403

    def test_sme_metrics_page_403s_without_its_module(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        _grant_and_enable_pco(tenant_a.org_id)
        login_as(client, tenant_a.org_admin_username)
        assert client.get("/automations/sme-metrics").status_code == 403

    def test_email_wa_page_403s_without_its_module(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        _grant_and_enable_pco(tenant_a.org_id)
        login_as(client, tenant_a.org_admin_username)
        assert client.get("/automations/email-wa").status_code == 403


class TestLegacyAutomationsUrlRedirects:
    def test_redirects_to_only_enabled_module(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        _grant_and_enable_sme_metrics(tenant_a.org_id)
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/automations", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/automations/sme-metrics"

    def test_redirects_to_first_module_when_multiple_enabled(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        _grant_and_enable_pco(tenant_a.org_id)
        _grant_and_enable_sme_metrics(tenant_a.org_id)
        _grant_and_enable_email_wa(tenant_a.org_id)
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/automations", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/automations/pco"

    def test_403s_with_no_module_enabled(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        assert client.get("/automations").status_code == 403

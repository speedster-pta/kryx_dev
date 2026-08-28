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


def _grant_and_enable_kryx_bookings(org_id: int) -> None:
    storage.grant(org_id, storage.MODULE_KRYX_BOOKINGS)
    storage.enable(org_id, storage.MODULE_KRYX_BOOKINGS)


def _grant_and_enable_stitch(org_id: int) -> None:
    storage.grant(org_id, storage.MODULE_STITCH)
    storage.enable(org_id, storage.MODULE_STITCH)


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
        assert 'href="/automations/kryx-bookings"' not in resp.text
        # Single-module case is a plain link, not the dropdown button.
        assert "Planning Center Automations" not in resp.text

    def test_kryx_bookings_only_renders_direct_link_not_dropdown(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        _grant_and_enable_kryx_bookings(tenant_a.org_id)
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/campaigns")
        assert resp.status_code == 200
        assert 'href="/automations/kryx-bookings"' in resp.text
        assert 'href="/automations/pco"' not in resp.text
        assert 'href="/automations/sme-metrics"' not in resp.text
        assert 'href="/automations/email-wa"' not in resp.text
        assert "Kryx Bookings Automations" not in resp.text

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

    def test_org_admin_with_all_four_modules_sees_dropdown(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        _grant_and_enable_pco(tenant_a.org_id)
        _grant_and_enable_sme_metrics(tenant_a.org_id)
        _grant_and_enable_email_wa(tenant_a.org_id)
        _grant_and_enable_kryx_bookings(tenant_a.org_id)
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/campaigns")
        assert resp.status_code == 200
        assert 'href="/automations/pco"' in resp.text
        assert 'href="/automations/sme-metrics"' in resp.text
        assert 'href="/automations/email-wa"' in resp.text
        assert 'href="/automations/kryx-bookings"' in resp.text
        assert ">Planning Center</a>" in resp.text
        assert ">SME Metrics</a>" in resp.text
        assert ">Email-to-WhatsApp</a>" in resp.text
        assert ">Kryx Bookings</a>" in resp.text

    def test_superadmin_always_sees_dropdown_with_every_module(self, client, login_as, superadmin_username):
        login_as(client, superadmin_username)
        resp = client.get("/campaigns")
        assert resp.status_code == 200
        assert 'href="/automations/pco"' in resp.text
        assert 'href="/automations/sme-metrics"' in resp.text
        assert 'href="/automations/email-wa"' in resp.text
        assert 'href="/automations/kryx-bookings"' in resp.text
        assert ">Planning Center</a>" in resp.text
        assert ">SME Metrics</a>" in resp.text
        assert ">Email-to-WhatsApp</a>" in resp.text
        assert ">Kryx Bookings</a>" in resp.text

    def test_kryx_bookings_appears_in_both_automations_dropdown_and_admin_nav(self, client, login_as, superadmin_username):
        """Kryx Bookings now has two independent nav entries, same as PCO/
        SME Metrics/Email-to-WhatsApp: an Automations dropdown entry
        pointing at its own unscoped /automations/kryx-bookings page (see
        web.auth.visible_automation_modules), and a separate Kryx Bookings
        Settings admin nav entry (under the Admin dropdown, gated by
        layout.html's kryx_bookings_visible - see admin.py) pointing at
        the org-scoped /kryx-bookings-settings page for API key
        management. Previously Kryx Bookings was excluded from the
        Automations dropdown because it only linked to the org-scoped
        settings page as a stand-in; now that it has its own dedicated,
        unscoped Automations page, it belongs in both places."""
        from autosend.web.auth import visible_automation_modules

        login_as(client, superadmin_username)
        resp = client.get("/campaigns")
        assert resp.status_code == 200
        assert 'href="/automations/kryx-bookings"' in resp.text
        # Settings-page link still appears (twice - desktop nav + mobile
        # menu), same as every other Admin-dropdown link e.g. /stitch-settings.
        assert resp.text.count('href="/kryx-bookings-settings"') == resp.text.count('href="/stitch-settings"')

        class _FakeRequest:
            session = {"is_superadmin": True}

        assert "kryx-bookings" in {m["key"] for m in visible_automation_modules(_FakeRequest())}


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

    def test_kryx_bookings_page_reachable_and_shows_only_its_sections(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        _grant_and_enable_pco(tenant_a.org_id)
        _grant_and_enable_kryx_bookings(tenant_a.org_id)
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/automations/kryx-bookings")
        assert resp.status_code == 200
        assert "Free Registrations Automations" not in resp.text
        assert "Kryx Bookings Automations" in resp.text

    def test_kryx_bookings_page_403s_without_its_module(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        _grant_and_enable_pco(tenant_a.org_id)
        login_as(client, tenant_a.org_admin_username)
        assert client.get("/automations/kryx-bookings").status_code == 403


class TestLegacyAutomationsUrlRedirects:
    def test_redirects_to_only_enabled_module(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        _grant_and_enable_sme_metrics(tenant_a.org_id)
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/automations", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/automations/sme-metrics"

    def test_redirects_to_kryx_bookings_when_it_sorts_first(self, client, login_as, tenants):
        # "Kryx Bookings" sorts before "Planning Center" alphabetically.
        tenant_a, _tenant_b = tenants
        _grant_and_enable_pco(tenant_a.org_id)
        _grant_and_enable_kryx_bookings(tenant_a.org_id)
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/automations", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/automations/kryx-bookings"

    def test_redirects_to_first_module_when_multiple_enabled(self, client, login_as, tenants):
        # "First" is alphabetical-by-label order (web/auth.py::
        # visible_automation_modules), not declaration order - with pco,
        # sme-metrics and email-wa all enabled, "Email-to-WhatsApp" sorts
        # before "Planning Center" and "SME Metrics".
        tenant_a, _tenant_b = tenants
        _grant_and_enable_pco(tenant_a.org_id)
        _grant_and_enable_sme_metrics(tenant_a.org_id)
        _grant_and_enable_email_wa(tenant_a.org_id)
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/automations", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/automations/email-wa"

    def test_403s_with_no_module_enabled(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        assert client.get("/automations").status_code == 403


class TestAdminDropdownIntegrationSettingsLinks:
    """layout.html's Admin dropdown (desktop + mobile) - every one of the
    five integration Settings links (Planning Center, SME Metrics,
    Email-to-WhatsApp, Stitch, Kryx Bookings) is org-scoped
    (admin_org_pages._resolve_org_id): a superadmin has no owning org, so
    clicking one without a ?org_id= query param 303-redirects to
    /organisations instead of doing anything useful - a link that's
    visible but never actually reachable. A superadmin manages a specific
    org's integrations via that org's own Configure link on the
    organisation detail page instead (organisation_detail.html, which
    carries ?org_id=), so these five Admin-dropdown links are hidden for
    superadmin entirely rather than left dangling.

    PCO/Stitch/Kryx Bookings stay visible to plain unit-scoped staff too
    (not just org-admin) - see PcoSettingsView/StitchSettingsView/
    KryxBookingsSettingsView's own docstrings for why (managing your own
    unit's webhook secret/payment credentials/API key is normal unit-
    scoped-staff work). SME Metrics/Email-to-WhatsApp stay org-admin-only,
    matching SmeMetricsSettingsView/EmailWaSettingsView's own
    is_accessible."""

    def test_superadmin_sees_none_of_the_five_integration_links(self, client, login_as, tenants, superadmin_username):
        tenant_a, _tenant_b = tenants
        _grant_and_enable_pco(tenant_a.org_id)
        _grant_and_enable_sme_metrics(tenant_a.org_id)
        _grant_and_enable_email_wa(tenant_a.org_id)
        _grant_and_enable_stitch(tenant_a.org_id)
        _grant_and_enable_kryx_bookings(tenant_a.org_id)
        login_as(client, superadmin_username)
        resp = client.get("/campaigns")
        assert resp.status_code == 200
        assert 'href="/pco-settings"' not in resp.text
        assert 'href="/sme-metrics-settings"' not in resp.text
        assert 'href="/email-wa-settings"' not in resp.text
        assert 'href="/stitch-settings"' not in resp.text
        assert 'href="/kryx-bookings-settings"' not in resp.text

    def test_org_admin_sees_all_five_integration_links_when_enabled(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        _grant_and_enable_pco(tenant_a.org_id)
        _grant_and_enable_sme_metrics(tenant_a.org_id)
        _grant_and_enable_email_wa(tenant_a.org_id)
        _grant_and_enable_stitch(tenant_a.org_id)
        _grant_and_enable_kryx_bookings(tenant_a.org_id)
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/campaigns")
        assert resp.status_code == 200
        assert 'href="/pco-settings"' in resp.text
        assert 'href="/sme-metrics-settings"' in resp.text
        assert 'href="/email-wa-settings"' in resp.text
        assert 'href="/stitch-settings"' in resp.text
        assert 'href="/kryx-bookings-settings"' in resp.text

    def test_plain_staff_sees_org_scoped_but_not_org_admin_only_links(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        _grant_and_enable_pco(tenant_a.org_id)
        _grant_and_enable_sme_metrics(tenant_a.org_id)
        _grant_and_enable_email_wa(tenant_a.org_id)
        _grant_and_enable_stitch(tenant_a.org_id)
        _grant_and_enable_kryx_bookings(tenant_a.org_id)
        login_as(client, tenant_a.staff_username)
        resp = client.get("/campaigns")
        assert resp.status_code == 200
        assert 'href="/pco-settings"' in resp.text
        assert 'href="/stitch-settings"' in resp.text
        assert 'href="/kryx-bookings-settings"' in resp.text
        assert 'href="/sme-metrics-settings"' not in resp.text
        assert 'href="/email-wa-settings"' not in resp.text

    def test_organisation_detail_page_offers_configure_links_for_every_module(self, client, login_as, tenants, superadmin_username):
        """The superadmin-only replacement path for the five links hidden
        above - regression coverage for the gap where Stitch/Kryx Bookings
        had no Configure link here at all (only PCO/SME Metrics/
        Email-to-WhatsApp did), which would have left a superadmin with no
        way to reach either page once the Admin-dropdown links were hidden."""
        tenant_a, _tenant_b = tenants
        _grant_and_enable_stitch(tenant_a.org_id)
        _grant_and_enable_kryx_bookings(tenant_a.org_id)
        login_as(client, superadmin_username)
        resp = client.get(f"/organisations/{tenant_a.org_id}")
        assert resp.status_code == 200
        assert f'href="/stitch-settings?org_id={tenant_a.org_id}"' in resp.text
        assert f'href="/kryx-bookings-settings?org_id={tenant_a.org_id}"' in resp.text

"""platform_email_settings - platform-wide outbound SMTP credentials
(currently Mailtrap), used for transactional email (signup email
verification). Superadmin-only, singleton, no per-tenant scoping to
attack - per this project's own testing convention for that shape of page
(see TestWabaUsageView in test_cross_org_isolation.py and
TestMetaSettingsPageAccess in test_meta_apps.py), the isolation property
under test here is just "a non-superadmin can't reach it".
"""


class TestPlatformEmailSettingsPageAccess:
    def test_plain_staff_cannot_reach_platform_email_settings(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.staff_username)
        resp = client.get("/platform-email-settings")
        assert resp.status_code == 403

    def test_org_admin_cannot_reach_platform_email_settings(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/platform-email-settings")
        assert resp.status_code == 403

    def test_superadmin_can_save_and_view(self, client, login_as, superadmin_username):
        login_as(client, superadmin_username)
        resp = client.post(
            "/platform-email-settings/save",
            data={
                "smtp_host": "smtp.mailtrap.io",
                "smtp_port": "2525",
                "smtp_username": "platform-user",
                "smtp_password": "topsecret",
                "from_address": "noreply@kryx.co.za",
            },
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)

        resp = client.get("/platform-email-settings")
        assert resp.status_code == 200
        assert "smtp.mailtrap.io" in resp.text
        assert "platform-user" in resp.text
        # The secret itself must never be rendered back.
        assert "topsecret" not in resp.text

    def test_superadmin_save_keeps_existing_password_when_blank(self, client, login_as, superadmin_username):
        login_as(client, superadmin_username)
        client.post(
            "/platform-email-settings/save",
            data={
                "smtp_host": "smtp.mailtrap.io",
                "smtp_port": "2525",
                "smtp_username": "platform-user",
                "smtp_password": "topsecret",
                "from_address": "noreply@kryx.co.za",
            },
            follow_redirects=False,
        )
        # Re-save with a new host and a blank password - the existing
        # password must survive, not be wiped out.
        resp = client.post(
            "/platform-email-settings/save",
            data={
                "smtp_host": "smtp2.mailtrap.io",
                "smtp_port": "2525",
                "smtp_username": "platform-user",
                "smtp_password": "",
                "from_address": "noreply@kryx.co.za",
            },
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)

        resp = client.get("/platform-email-settings")
        assert resp.status_code == 200
        assert "smtp2.mailtrap.io" in resp.text
        # Placeholder confirms a password is still on file (masked, not blank).
        assert "leave blank to keep" in resp.text.lower()

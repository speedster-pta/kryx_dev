"""meta_apps - extra Meta Apps whose webhook signatures /webhooks/whatsapp
should also accept, alongside the meta_platform_settings singleton (see
schema.py's meta_apps table docstring for why a WABA still tied to another
Tech Provider/BSP needs a second app to receive live events at all).

Platform-wide, not tenant-scoped (same as meta_platform_settings itself),
so - per this project's own testing convention for a superadmin-only page
with no per-tenant scoping to attack (see TestWabaUsageView in
test_cross_org_isolation.py) - the isolation property under test here is
just "a non-superadmin can't reach it", not a cross-org data leak. The
actual security-relevant behaviour is the webhook signature verification
trying every known secret, covered below against the real HTTP route.
"""
import hashlib
import hmac

from autosend.admin_models import MetaApp, engine
from sqlalchemy.orm import Session


def _create_meta_app(app_id: str, app_secret: str, label: str | None = None) -> None:
    from datetime import datetime, timezone

    with Session(engine) as session:
        session.add(
            MetaApp(
                app_id=app_id,
                # MetaApp.app_secret is an EncryptedString - it encrypts on
                # write automatically, so this must be plaintext, not
                # pre-encrypted (see admin_models.EncryptedString).
                app_secret=app_secret,
                label=label,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        )
        session.commit()


class TestMetaSettingsPageAccess:
    """Meta Platform Settings and Meta Apps are one merged superadmin-only
    page (admin_pages.MetaSettingsView) - the Meta Apps card list is
    rendered on /meta-settings itself, with /meta-apps/{id} and
    /meta-apps/new as its detail/create sub-pages, rather than a separate
    /meta-apps/list SQLAdmin CRUD screen."""

    def test_plain_staff_cannot_reach_meta_settings(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.staff_username)
        resp = client.get("/meta-settings")
        assert resp.status_code == 403

    def test_org_admin_cannot_reach_meta_settings(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/meta-settings")
        assert resp.status_code == 403

    def test_superadmin_can_create_and_list(self, client, login_as, superadmin_username):
        login_as(client, superadmin_username)
        resp = client.post(
            "/meta-apps",
            data={"app_id": "123456789", "app_secret": "topsecret", "label": "Chatwoot bridge app"},
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)

        resp = client.get("/meta-settings")
        assert resp.status_code == 200
        assert "Chatwoot bridge app" in resp.text
        # The secret itself must never be rendered back.
        assert "topsecret" not in resp.text


class TestWhatsAppWebhookMultiSecretVerification:
    """integrations/webhooks.py::whatsapp_webhook_event must accept a
    signature computed with ANY known app_secret - the primary
    meta_platform_settings one or any meta_apps row - not just the first
    one ever configured."""

    def _set_primary_secret(self, secret: str) -> None:
        from datetime import datetime, timezone
        from autosend.admin_models import MetaPlatformSettings

        with Session(engine) as session:
            settings = session.query(MetaPlatformSettings).first()
            if settings is None:
                session.add(
                    MetaPlatformSettings(
                        app_id="primary-app",
                        app_secret=secret,
                        config_id="cfg-1",
                        webhook_verify_token=None,
                        created_at=datetime.now(timezone.utc).isoformat(),
                    )
                )
            else:
                settings.app_secret = secret
            session.commit()

    def _sign(self, secret: str, body: bytes) -> str:
        return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    def test_accepts_signature_from_secondary_meta_app_secret(self, client):
        self._set_primary_secret("primary-secret")
        _create_meta_app("second-app-id", "secondary-secret", label="Chatwoot bridge app")

        body = b'{"entry": []}'
        signature = self._sign("secondary-secret", body)
        resp = client.post(
            "/webhooks/whatsapp",
            content=body,
            headers={"X-Hub-Signature-256": signature, "Content-Type": "application/json"},
        )
        assert resp.status_code == 200

    def test_rejects_signature_from_unknown_secret(self, client):
        self._set_primary_secret("primary-secret-2")
        _create_meta_app("third-app-id", "known-secret", label="Another bridge app")

        body = b'{"entry": []}'
        signature = self._sign("some-other-secret", body)
        resp = client.post(
            "/webhooks/whatsapp",
            content=body,
            headers={"X-Hub-Signature-256": signature, "Content-Type": "application/json"},
        )
        assert resp.status_code == 401

    def test_still_accepts_primary_secret_when_extra_apps_exist(self, client):
        self._set_primary_secret("primary-secret-3")
        _create_meta_app("fourth-app-id", "unrelated-secret", label="Unrelated bridge app")

        body = b'{"entry": []}'
        signature = self._sign("primary-secret-3", body)
        resp = client.post(
            "/webhooks/whatsapp",
            content=body,
            headers={"X-Hub-Signature-256": signature, "Content-Type": "application/json"},
        )
        assert resp.status_code == 200

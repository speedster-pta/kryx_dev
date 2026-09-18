"""smb_message_echoes - Coexistence-onboarded numbers (a native WhatsApp
Business App/Web session still linked alongside the Cloud API) echo back
messages sent from that device so they still show up in the Inbox - see
integrations/webhooks.py::_handle_message_echoes and
storage/conversations.py::record_outbound_echo.

Ported from the single-org parent project (dev-whatsapp); this only
exercises the idempotency property (Meta can redeliver this webhook field
too, same as `messages`), following this project's convention of testing
webhook handling against the real HTTP route rather than calling storage
functions directly.
"""
import hashlib
import hmac
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from autosend import storage
from autosend.admin_models import MetaPlatformSettings, engine


def _set_primary_secret(secret: str) -> None:
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


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _echo_envelope(phone_number_id: str, wa_id: str, wamid: str, text: str) -> bytes:
    import json

    return json.dumps(
        {
            "entry": [
                {
                    "id": "waba-id",
                    "changes": [
                        {
                            "field": "smb_message_echoes",
                            "value": {
                                "metadata": {"phone_number_id": phone_number_id},
                                "message_echoes": [
                                    {
                                        "id": wamid,
                                        "to": wa_id,
                                        "type": "text",
                                        "text": {"body": text},
                                    }
                                ],
                            },
                        }
                    ],
                }
            ]
        }
    ).encode()


class TestSmbMessageEchoes:
    def test_device_sent_message_appears_in_inbox(self, client, tenants):
        tenant_a, _tenant_b = tenants
        _set_primary_secret("echo-secret")

        number = storage.get_whatsapp_number_by_id(tenant_a.number_id)
        body = _echo_envelope(number["phone_number_id"], "27820000001", "wamid.echo-1", "Hi from the phone")
        signature = _sign("echo-secret", body)

        resp = client.post(
            "/webhooks/whatsapp",
            content=body,
            headers={"X-Hub-Signature-256": signature, "Content-Type": "application/json"},
        )
        assert resp.status_code == 200

        conversations = storage.list_conversations([tenant_a.unit_id])
        assert len(conversations) == 1
        conversation = conversations[0]
        assert conversation["contact_wa_id"] == "27820000001"
        assert conversation["last_message_preview"] == "Hi from the phone"

        messages = storage.list_messages(conversation["id"])
        assert len(messages) == 1
        assert messages[0]["direction"] == "out"
        assert messages[0]["sender_type"] == "device"
        assert messages[0]["wamid"] == "wamid.echo-1"
        assert messages[0]["body"] == "Hi from the phone"

    def test_redelivered_echo_is_idempotent_on_wamid(self, client, tenants):
        tenant_a, _tenant_b = tenants
        _set_primary_secret("echo-secret-2")

        number = storage.get_whatsapp_number_by_id(tenant_a.number_id)
        body = _echo_envelope(number["phone_number_id"], "27820000002", "wamid.echo-2", "Redelivered echo")
        signature = _sign("echo-secret-2", body)

        for _ in range(2):
            resp = client.post(
                "/webhooks/whatsapp",
                content=body,
                headers={"X-Hub-Signature-256": signature, "Content-Type": "application/json"},
            )
            assert resp.status_code == 200

        conversations = storage.list_conversations([tenant_a.unit_id], whatsapp_number_id=tenant_a.number_id)
        assert len(conversations) == 1
        messages = storage.list_messages(conversations[0]["id"])
        assert len(messages) == 1
        assert messages[0]["wamid"] == "wamid.echo-2"

"""Tests for sending an approved template from the Inbox once a
conversation's 24h session window has closed (web/conversations_router.py's
POST /api/conversations/{id}/reply, extended to accept type="template"
alongside the existing freeform type="text" path) - ported from the
sibling single-tenant project (shofar-cloud's dev-whatsapp/api/
shofar_automation/web/conversations_router.py), whose single reply
endpoint dispatches on the same `type` field.

clients.get_whatsapp_client_for_number is monkeypatched to a fake client
(same technique as test_kryx_bookings_send.py's FakeWhatsAppClient)
rather than exercising the real WhatsApp Graph API client, whose
access_token in the tenants fixture is a placeholder that can't make a
real HTTP call.
"""
from autosend import clients, storage


class FakeWhatsAppClient:
    def __init__(self):
        self.sent_templates = []

    async def send_template(self, to_phone_e164, template_name, *parameters, header_image_url=None,
                             button_values=None, language="en"):
        self.sent_templates.append({
            "to": to_phone_e164, "template_name": template_name, "parameters": parameters,
            "language": language,
        })
        return {"messages": [{"id": "wamid.fake_template"}]}

    async def send_text(self, to_phone_e164, text):
        raise AssertionError("send_text should not be called for a closed-window template send")


def test_template_send_succeeds_when_session_window_closed(client, login_as, monkeypatch, tenants):
    tenant_a, _tenant_b = tenants
    conversation = storage.get_or_create_conversation(
        unit_id=tenant_a.unit_id, whatsapp_number_id=tenant_a.number_id, contact_wa_id="27000000201",
    )
    assert not storage.is_session_window_open(conversation)

    fake_client = FakeWhatsAppClient()
    monkeypatch.setattr(clients, "get_whatsapp_client_for_number", lambda number: fake_client)

    login_as(client, tenant_a.staff_username)
    resp = client.post(
        f"/api/conversations/{conversation['id']}/reply",
        json={
            "type": "template", "template_name": "booking_confirmed", "language": "en_US",
            "variables": ["Alex"], "text": "Hi Alex, your booking is confirmed.",
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["wamid"] == "wamid.fake_template"

    assert fake_client.sent_templates == [{
        "to": "27000000201", "template_name": "booking_confirmed",
        "parameters": ("Alex",), "language": "en_US",
    }]

    messages = storage.list_messages(conversation["id"])
    assert len(messages) == 1
    assert messages[0]["message_type"] == "template"
    assert messages[0]["template_name"] == "booking_confirmed"
    assert messages[0]["status"] == "sent"
    assert messages[0]["body"] == "Hi Alex, your booking is confirmed."

    updated_conversation = storage.get_conversation(conversation["id"])
    assert updated_conversation["last_message_preview"] == "Hi Alex, your booking is confirmed."


def test_freeform_reply_still_400s_when_session_window_closed(client, login_as, monkeypatch, tenants):
    """The exact same conversation state that makes a template send
    succeed above must still reject a freeform text reply - Meta simply
    doesn't allow business-initiated free text outside the session
    window, template or not."""
    tenant_a, _tenant_b = tenants
    conversation = storage.get_or_create_conversation(
        unit_id=tenant_a.unit_id, whatsapp_number_id=tenant_a.number_id, contact_wa_id="27000000202",
    )
    assert not storage.is_session_window_open(conversation)

    fake_client = FakeWhatsAppClient()
    monkeypatch.setattr(clients, "get_whatsapp_client_for_number", lambda number: fake_client)

    login_as(client, tenant_a.staff_username)
    resp = client.post(f"/api/conversations/{conversation['id']}/reply", json={"text": "hi there"})

    assert resp.status_code == 400
    assert storage.list_messages(conversation["id"]) == []


def test_template_send_requires_template_name(client, login_as, monkeypatch, tenants):
    tenant_a, _tenant_b = tenants
    conversation = storage.get_or_create_conversation(
        unit_id=tenant_a.unit_id, whatsapp_number_id=tenant_a.number_id, contact_wa_id="27000000203",
    )

    fake_client = FakeWhatsAppClient()
    monkeypatch.setattr(clients, "get_whatsapp_client_for_number", lambda number: fake_client)

    login_as(client, tenant_a.staff_username)
    resp = client.post(f"/api/conversations/{conversation['id']}/reply", json={"type": "template"})

    assert resp.status_code == 400
    assert fake_client.sent_templates == []

"""Tests for storage.mirror_outbound_to_inbox and its wiring into the
automated send paths (services/registration_poller.py,
services/form_response.py, services/serving_reminder.py,
web/campaign_runner.py) - previously these sends were recorded only in
send_log/campaign_recipients, which neither the Inbox thread view
(web/conversations_router.py::api_list_messages) nor the AI auto-reply's
conversation-history builder (services/ai_reply.py::_recent_history) ever
read from, so a contact's registration confirmation, serving reminder, or
campaign blast was structurally invisible to both even though it was
actually sent and delivered.

No async test plugin (pytest-asyncio/anyio) is installed in this repo -
driven via asyncio.run() in plain sync test functions, same as
test_registration_poller_ical.py.
"""
import asyncio
import uuid

from autosend import storage
from autosend.services import ai_reply, registration_poller
from autosend.services.registration_poller import _process_registration
from autosend.template_variables import render_template_body


class FakePcoClient:
    def __init__(self, phone):
        self._phone = phone

    async def get_registration_detail(self, registration_id):
        return {
            "data": {"attributes": {"total_due_cents": 0}},
            "included": [
                {
                    "type": "Person",
                    "id": "person-1",
                    "attributes": {"first_name": "Alex", "last_name": "Test"},
                }
            ],
        }

    async def get_person_phone(self, person_id):
        return self._phone


class FakeWhatsAppClient:
    def __init__(self, number):
        self.number = number
        self.sent_calls = []

    async def send_free_acknowledgment_template(self, **kwargs):
        self.sent_calls.append(kwargs)
        # A fresh wamid per call - conversation_messages.wamid is UNIQUE,
        # same as real Meta wamids would be across distinct sends.
        return {"messages": [{"id": f"wamid.fake-{uuid.uuid4().hex[:8]}"}]}


def _signup():
    tag = uuid.uuid4().hex[:8]
    return {"id": f"signup-{tag}", "name": "Youth Camp", "is_paid": False, "times": []}


class TestRegistrationConfirmationMirroring:
    def test_successful_confirmation_mirrors_into_inbox_with_rendered_body(self, monkeypatch, tenants):
        tenant_a, _ = tenants
        unit = {"id": tenant_a.unit_id, "org_id": tenant_a.org_id, "slug": tenant_a.unit_name}
        signup = _signup()
        storage.upsert_registration_template(
            unit["id"], "free_acknowledgment", "free_ack_tmpl",
            ["first_name", "event_name"], None, None, None, True,
        )
        number = storage.get_whatsapp_number_by_id(tenant_a.number_id)
        pco_client = FakePcoClient(phone="+27821230001")
        wa_client = FakeWhatsAppClient(number)
        monkeypatch.setattr(registration_poller, "get_pco_client", lambda unit: pco_client)
        monkeypatch.setattr(registration_poller, "resolve_whatsapp_client", lambda unit, template: wa_client)
        monkeypatch.setattr(
            registration_poller, "get_template_body_text",
            lambda token, waba_id, template_name, cache: "Hi {{1}}, thanks for registering for {{2}}!",
        )

        asyncio.run(_process_registration(unit, "reg-1", signup, {}))

        conversation = storage.get_or_create_conversation(
            unit_id=tenant_a.unit_id, whatsapp_number_id=tenant_a.number_id, contact_wa_id="27821230001",
        )
        messages = storage.list_messages(conversation["id"])
        assert len(messages) == 1
        message = messages[0]
        assert message["direction"] == "out"
        assert message["sender_type"] == "automation"
        assert message["status"] == "sent"
        assert message["template_name"] == "free_ack_tmpl"
        assert message["body"] == "Hi Alex, thanks for registering for Youth Camp!"
        assert message["wamid"] and message["wamid"].startswith("wamid.fake-")

    def test_failed_confirmation_does_not_mirror(self, monkeypatch, tenants):
        """A registration with no phone on file raises before any send is
        attempted - _process_registration_inner's ValueError propagates
        through _process_registration, whose only DB write on that path is
        the send_log 'failed' row. Nothing should ever reach the Inbox for
        an attempt that never reached the contact."""
        tenant_a, _ = tenants
        unit = {"id": tenant_a.unit_id, "org_id": tenant_a.org_id, "slug": tenant_a.unit_name}
        signup = _signup()
        storage.upsert_registration_template(
            unit["id"], "free_acknowledgment", "free_ack_tmpl",
            ["first_name", "event_name"], None, None, None, True,
        )
        number = storage.get_whatsapp_number_by_id(tenant_a.number_id)
        pco_client = FakePcoClient(phone=None)  # No phone on file.
        wa_client = FakeWhatsAppClient(number)
        monkeypatch.setattr(registration_poller, "get_pco_client", lambda unit: pco_client)
        monkeypatch.setattr(registration_poller, "resolve_whatsapp_client", lambda unit, template: wa_client)

        try:
            asyncio.run(_process_registration(unit, "reg-2", signup, {}))
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError for missing phone")

        assert wa_client.sent_calls == []
        # No conversation should ever have been created for a send that
        # never happened.
        conversations = storage.list_conversations([tenant_a.unit_id])
        assert conversations == []

    def test_mirrored_message_feeds_ai_reply_history(self, monkeypatch, tenants):
        """The whole point of mirroring: services/ai_reply.py's
        _recent_history (used to build the RAG prompt's conversation
        context) must actually pick up an automated send, not just the
        Inbox UI - both read storage.list_messages, so this is really
        confirming the same fix from a different caller."""
        tenant_a, _ = tenants
        unit = {"id": tenant_a.unit_id, "org_id": tenant_a.org_id, "slug": tenant_a.unit_name}
        signup = _signup()
        storage.upsert_registration_template(
            unit["id"], "free_acknowledgment", "free_ack_tmpl",
            ["first_name", "event_name"], None, None, None, True,
        )
        number = storage.get_whatsapp_number_by_id(tenant_a.number_id)
        pco_client = FakePcoClient(phone="+27821230002")
        wa_client = FakeWhatsAppClient(number)
        monkeypatch.setattr(registration_poller, "get_pco_client", lambda unit: pco_client)
        monkeypatch.setattr(registration_poller, "resolve_whatsapp_client", lambda unit, template: wa_client)
        monkeypatch.setattr(
            registration_poller, "get_template_body_text",
            lambda token, waba_id, template_name, cache: None,  # force the joined-values fallback
        )

        asyncio.run(_process_registration(unit, "reg-3", signup, {}))

        conversation = storage.get_or_create_conversation(
            unit_id=tenant_a.unit_id, whatsapp_number_id=tenant_a.number_id, contact_wa_id="27821230002",
        )
        history = ai_reply._recent_history(conversation["id"])
        assert len(history) == 1
        assert history[0]["role"] == "assistant"
        assert "Alex" in history[0]["content"]


def test_usage_does_not_double_count_automation_mirrored_sends(tenants):
    """storage/usage.py's UNION only counts conversation_messages rows
    with sender_type='staff' - an 'automation' row (see
    mirror_outbound_to_inbox) must be excluded from that branch, since the
    same send is already counted via its send_log row. Both rows existing
    for one conceptual send must therefore total 1, not 2."""
    tenant_a, _ = tenants
    storage.record_send(
        unit_id=tenant_a.unit_id, source="registration_poller", status="sent",
        whatsapp_number_id=tenant_a.number_id, recipient_phone="+27821230003",
        template_name="free_ack_tmpl", wamid="wamid.usage-test",
    )
    storage.mirror_outbound_to_inbox(
        tenant_a.unit_id, tenant_a.number_id, "+27821230003",
        template_name="free_ack_tmpl", body="Hi Alex, thanks for registering!", wamid="wamid.usage-test",
    )

    totals = {row["whatsapp_number_id"]: row["message_count"] for row in storage.send_totals_by_number(days=1)}
    assert totals.get(tenant_a.number_id) == 1


def test_render_template_body_substitutes_and_preserves_missing_placeholders():
    """Ports web/static/inbox.js's renderTemplateBody regex/fallback
    exactly: a present value replaces {{n}}; a missing/blank one leaves
    the placeholder untouched rather than rendering an empty gap."""
    rendered = render_template_body(
        "Hi {{1}}, your {{2}} reservation is confirmed. Ref: {{3}}", ["Alex", "Youth Camp"],
    )
    assert rendered == "Hi Alex, your Youth Camp reservation is confirmed. Ref: {{3}}"

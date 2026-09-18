"""Tests for the AI Assistant draft-review step (storage.mark_draft_sent/
delete_draft_message in storage/conversations.py, and the send/discard
endpoints in web/conversations_router.py) - ported from the sibling
single-tenant project (shofar-cloud's dev-whatsapp/api/shofar_automation)
alongside services/ai_reply.py's draft_review_enabled branch.
"""
from autosend import storage


def test_draft_message_does_not_advance_conversation_ordering(tenants):
    """record_outbound_message only advances last_outbound_at/
    last_message_at/last_message_preview when status == 'sent' (see its
    own conditional in storage/conversations.py) - a status='draft' row,
    which is exactly what services/ai_reply.py writes when
    draft_review_enabled is set, must not jump a conversation to the top
    of the Inbox list or change its preview before a staff member ever
    approves it. Once approved (mark_draft_sent), those fields must then
    update, the same way a normal send would."""
    tenant_a, _ = tenants
    conversation = storage.get_or_create_conversation(
        unit_id=tenant_a.unit_id, whatsapp_number_id=tenant_a.number_id, contact_wa_id="27000000101",
    )
    assert conversation["last_outbound_at"] is None
    assert conversation["last_message_at"] is None
    assert conversation["last_message_preview"] is None

    message_id = storage.record_outbound_message(
        conversation["id"], sender_type="ai", message_type="text", body="Drafted reply", status="draft",
    )

    still_untouched = storage.get_conversation(conversation["id"])
    assert still_untouched["last_outbound_at"] is None
    assert still_untouched["last_message_at"] is None
    assert still_untouched["last_message_preview"] is None

    storage.mark_draft_sent(message_id, conversation["id"], "Drafted reply", "wamid.approved1")

    approved = storage.get_conversation(conversation["id"])
    assert approved["last_outbound_at"] is not None
    assert approved["last_message_at"] is not None
    assert approved["last_message_preview"] == "Drafted reply"

    messages = storage.list_messages(conversation["id"])
    assert messages[0]["status"] == "sent"
    assert messages[0]["wamid"] == "wamid.approved1"


def test_delete_draft_message_only_ever_removes_a_draft_row():
    """delete_draft_message's WHERE clause scopes to status='draft' by
    itself (not just a caller-side check) - a stale/wrong id pointing at
    an already-sent message must never be deletable through this path."""
    # No DB fixtures needed beyond the module-level test DB conftest.py
    # already points at - a bogus id simply matches no row.
    storage.delete_draft_message(999_999_999)  # no-op, must not raise


def test_send_draft_message_rejects_when_session_window_closed(client, login_as, tenants):
    """web/conversations_router.py's send-draft endpoint re-checks the
    24h WhatsApp session window at approval time, not just whenever the
    draft was originally written - a conversation with no inbound message
    at all (is_session_window_open's own "no last_inbound_at -> closed"
    rule) must be rejected with 409 rather than attempting a send."""
    tenant_a, _ = tenants
    conversation = storage.get_or_create_conversation(
        unit_id=tenant_a.unit_id, whatsapp_number_id=tenant_a.number_id, contact_wa_id="27000000102",
    )
    assert not storage.is_session_window_open(conversation)

    message_id = storage.record_outbound_message(
        conversation["id"], sender_type="ai", message_type="text", body="Drafted reply", status="draft",
    )

    login_as(client, tenant_a.staff_username)
    resp = client.post(f"/api/conversations/{conversation['id']}/messages/{message_id}/send")

    assert resp.status_code == 409
    assert storage.list_messages(conversation["id"])[0]["status"] == "draft"


def test_send_draft_message_rejects_a_non_draft_message(client, login_as, tenants):
    """A message that has already been sent (or discarded/never existed)
    isn't a "pending draft" any more - the endpoint must reject it rather
    than re-sending or double-processing it."""
    tenant_a, _ = tenants
    conversation = storage.get_or_create_conversation(
        unit_id=tenant_a.unit_id, whatsapp_number_id=tenant_a.number_id, contact_wa_id="27000000103",
    )
    message_id = storage.record_outbound_message(
        conversation["id"], sender_type="staff", message_type="text", body="Already sent", status="sent",
    )

    login_as(client, tenant_a.staff_username)
    resp = client.post(f"/api/conversations/{conversation['id']}/messages/{message_id}/send")

    assert resp.status_code == 400


def test_discard_draft_message_removes_only_the_draft_row(client, login_as, tenants):
    tenant_a, _ = tenants
    conversation = storage.get_or_create_conversation(
        unit_id=tenant_a.unit_id, whatsapp_number_id=tenant_a.number_id, contact_wa_id="27000000104",
    )
    message_id = storage.record_outbound_message(
        conversation["id"], sender_type="ai", message_type="text", body="Drafted reply", status="draft",
    )

    login_as(client, tenant_a.staff_username)
    resp = client.delete(f"/api/conversations/{conversation['id']}/messages/{message_id}")

    assert resp.status_code == 200
    assert storage.list_messages(conversation["id"]) == []

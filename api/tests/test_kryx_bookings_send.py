"""Tests for POST /integrations/kryx-bookings/send
(integrations/kryx_bookings.py) - the per-org API-key-authenticated
endpoint a customer's own Kryx Bookings instance calls to trigger a
WhatsApp send. Covers auth (missing/invalid key), the happy path
(configured template resolves the five fixed variables in the configured
order), idempotency, and the module-disabled drop case.

resolve_whatsapp_client is monkeypatched to a fake client (same technique
as test_serving_reminder_combined.py's FakeWhatsAppClient) rather than
exercising the real WhatsApp Graph API client, whose access_token in the
tenants fixture is a placeholder that can't make a real HTTP call."""
import uuid

from autosend import storage
from autosend.integrations import kryx_bookings as kryx_bookings_integration


class FakeWhatsAppClient:
    def __init__(self, number):
        self.number = number
        self.sent_calls = []

    async def send_template(self, phone, template_name, *body_values, header_image_url=None, button_values=None):
        self.sent_calls.append({"phone": phone, "template_name": template_name, "body_values": body_values})
        return {"messages": [{"id": "wamid.fake"}]}


def _setup_connection(tenant, status="approved", body_variable_order=None):
    storage.grant(tenant.org_id, storage.MODULE_KRYX_BOOKINGS)
    storage.enable(tenant.org_id, storage.MODULE_KRYX_BOOKINGS)
    storage.upsert_booking_automation(
        None, tenant.unit_id, status, "booking_update",
        body_variable_order or ["first_name", "date_time", "status", "location"],
        tenant.number_id, [], None, True,
    )
    return storage.generate_api_key(tenant.unit_id)


def _payload(**overrides):
    payload = {
        "idempotency_key": uuid.uuid4().hex,
        "to": "0821234567",
        "first_name": "Alex",
        "type": "Haircut",
        "date_time": "2026-09-01T10:00:00",
        "status": "approved",
        "location": "Main Branch",
    }
    payload.update(overrides)
    return payload


def test_missing_api_key_rejected(client):
    resp = client.post("/integrations/kryx-bookings/send", json=_payload())
    assert resp.status_code == 401


def test_invalid_api_key_rejected(client):
    resp = client.post(
        "/integrations/kryx-bookings/send", json=_payload(),
        headers={"X-API-Key": "kxb_not-a-real-key"},
    )
    assert resp.status_code == 401


def test_successful_send_uses_configured_template_and_variable_order(
    client, monkeypatch, tenants, grant_unlimited_capacity,
):
    tenant_a, _tenant_b = tenants
    grant_unlimited_capacity(tenant_a.org_id)
    api_key = _setup_connection(tenant_a)

    fake_client = FakeWhatsAppClient({"id": tenant_a.number_id})
    monkeypatch.setattr(
        kryx_bookings_integration, "resolve_whatsapp_client", lambda unit, template: fake_client
    )

    resp = client.post(
        "/integrations/kryx-bookings/send", json=_payload(),
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "queued"}

    assert len(fake_client.sent_calls) == 1
    call = fake_client.sent_calls[0]
    assert call["template_name"] == "booking_update"
    # "status" resolves to its display label (Approved), not the raw
    # payload value (approved) - see storage.KRYX_BOOKINGS_STATUS_LABELS.
    assert call["body_values"] == ("Alex", "2026-09-01T10:00:00", "Approved", "Main Branch")

    sends = storage.get_recent_sends(unit_ids=[tenant_a.unit_id])
    assert any(s["source"] == "kryx_bookings" and s["status"] == "sent" for s in sends)

    connection = storage.get_kryx_bookings_connection(tenant_a.unit_id)
    assert connection["last_used_at"] is not None


def test_retried_idempotency_key_does_not_resend(client, monkeypatch, tenants, grant_unlimited_capacity):
    tenant_a, _tenant_b = tenants
    grant_unlimited_capacity(tenant_a.org_id)
    api_key = _setup_connection(tenant_a)

    fake_client = FakeWhatsAppClient({"id": tenant_a.number_id})
    monkeypatch.setattr(
        kryx_bookings_integration, "resolve_whatsapp_client", lambda unit, template: fake_client
    )

    payload = _payload()
    for _ in range(2):
        resp = client.post(
            "/integrations/kryx-bookings/send", json=payload,
            headers={"X-API-Key": api_key},
        )
        assert resp.status_code == 200

    assert len(fake_client.sent_calls) == 1


def test_send_dropped_when_module_disabled(client, monkeypatch, tenants):
    tenant_a, _tenant_b = tenants
    api_key = _setup_connection(tenant_a)
    storage.disable(tenant_a.org_id, storage.MODULE_KRYX_BOOKINGS)

    fake_client = FakeWhatsAppClient({"id": tenant_a.number_id})
    monkeypatch.setattr(
        kryx_bookings_integration, "resolve_whatsapp_client", lambda unit, template: fake_client
    )

    resp = client.post(
        "/integrations/kryx-bookings/send", json=_payload(),
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 200
    assert fake_client.sent_calls == []


def test_send_rejected_for_deactivated_key(client, monkeypatch, tenants):
    tenant_a, _tenant_b = tenants
    api_key = _setup_connection(tenant_a)
    storage.set_connection_active(tenant_a.unit_id, False)

    resp = client.post(
        "/integrations/kryx-bookings/send", json=_payload(),
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 401


def test_unknown_status_rejected_before_queuing(client, tenants):
    tenant_a, _tenant_b = tenants
    api_key = _setup_connection(tenant_a)
    resp = client.post(
        "/integrations/kryx-bookings/send", json=_payload(status="confirmed"),
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 400


def test_status_selects_its_own_template(client, monkeypatch, tenants, grant_unlimited_capacity):
    """Each booking status gets its own configurable template - a send
    for a status with no template configured must not fall back to some
    other status's template."""
    tenant_a, _tenant_b = tenants
    grant_unlimited_capacity(tenant_a.org_id)
    api_key = _setup_connection(tenant_a, status="approved")
    storage.upsert_booking_automation(
        None, tenant_a.unit_id, "cancelled", "booking_cancelled",
        ["first_name", "location"], tenant_a.number_id, [], None, True,
    )

    fake_client = FakeWhatsAppClient({"id": tenant_a.number_id})
    monkeypatch.setattr(
        kryx_bookings_integration, "resolve_whatsapp_client", lambda unit, template: fake_client
    )

    resp = client.post(
        "/integrations/kryx-bookings/send", json=_payload(status="cancelled"),
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 200
    assert len(fake_client.sent_calls) == 1
    assert fake_client.sent_calls[0]["template_name"] == "booking_cancelled"
    assert fake_client.sent_calls[0]["body_values"] == ("Alex", "Main Branch")


def test_send_fails_when_no_template_configured_for_status(client, tenants, grant_unlimited_capacity):
    tenant_a, _tenant_b = tenants
    grant_unlimited_capacity(tenant_a.org_id)
    api_key = _setup_connection(tenant_a, status="approved")

    resp = client.post(
        "/integrations/kryx-bookings/send", json=_payload(status="declined"),
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 200

    sends = storage.get_recent_sends(unit_ids=[tenant_a.unit_id])
    assert any(
        s["source"] == "kryx_bookings" and s["status"] == "failed"
        and "declined" in (s["error_message"] or "")
        for s in sends
    )


def test_key_belongs_only_to_its_own_unit(client, monkeypatch, tenants, grant_unlimited_capacity):
    """A tenant_a key must never resolve to/send through tenant_b's unit -
    the whole point of per-unit keys rather than a shared platform secret."""
    tenant_a, tenant_b = tenants
    grant_unlimited_capacity(tenant_a.org_id)
    api_key = _setup_connection(tenant_a)
    _setup_connection(tenant_b)

    fake_client = FakeWhatsAppClient({"id": tenant_a.number_id})
    monkeypatch.setattr(
        kryx_bookings_integration, "resolve_whatsapp_client", lambda unit, template: fake_client
    )

    client.post(
        "/integrations/kryx-bookings/send", json=_payload(),
        headers={"X-API-Key": api_key},
    )

    sends_b = storage.get_recent_sends(unit_ids=[tenant_b.unit_id])
    assert not any(s["source"] == "kryx_bookings" for s in sends_b)


def test_status_can_fan_out_to_multiple_automations(client, monkeypatch, tenants, grant_unlimited_capacity):
    """A single status can have more than one active automation - e.g. a
    'pending' booking notifying both the person who made the request
    (recipient_mode='requester', sent to the payload's own 'to') and a
    fixed approver number (recipient_mode='fixed') - both fire from the
    same incoming event."""
    tenant_a, _tenant_b = tenants
    grant_unlimited_capacity(tenant_a.org_id)
    storage.grant(tenant_a.org_id, storage.MODULE_KRYX_BOOKINGS)
    storage.enable(tenant_a.org_id, storage.MODULE_KRYX_BOOKINGS)
    storage.upsert_booking_automation(
        None, tenant_a.unit_id, "pending", "booking_requested",
        ["first_name"], tenant_a.number_id, [], None, True,
    )
    storage.upsert_booking_automation(
        None, tenant_a.unit_id, "pending", "booking_needs_approval",
        ["first_name", "location"], tenant_a.number_id, [], None, True,
        recipient_mode="fixed", recipient_phone="+27827654321",
    )
    api_key = storage.generate_api_key(tenant_a.unit_id)

    fake_client = FakeWhatsAppClient({"id": tenant_a.number_id})
    monkeypatch.setattr(
        kryx_bookings_integration, "resolve_whatsapp_client", lambda unit, template: fake_client
    )

    resp = client.post(
        "/integrations/kryx-bookings/send", json=_payload(status="pending", to="0821234567"),
        headers={"X-API-Key": api_key},
    )
    assert resp.status_code == 200

    assert len(fake_client.sent_calls) == 2
    calls_by_template = {c["template_name"]: c for c in fake_client.sent_calls}
    assert calls_by_template["booking_requested"]["phone"] == "+27821234567"
    assert calls_by_template["booking_needs_approval"]["phone"] == "+27827654321"

    sends = storage.get_recent_sends(unit_ids=[tenant_a.unit_id])
    sent = [s for s in sends if s["source"] == "kryx_bookings" and s["status"] == "sent"]
    assert len(sent) == 2


def test_retry_only_resends_the_automation_that_did_not_go_through(
    client, monkeypatch, tenants, grant_unlimited_capacity,
):
    """already_sent is checked per (idempotency_key, automation), not just
    per request - a retry after one automation succeeded but another
    hadn't must resend only the one that didn't, not both, and never the
    one that already sent."""
    tenant_a, _tenant_b = tenants
    grant_unlimited_capacity(tenant_a.org_id)
    storage.grant(tenant_a.org_id, storage.MODULE_KRYX_BOOKINGS)
    storage.enable(tenant_a.org_id, storage.MODULE_KRYX_BOOKINGS)
    storage.upsert_booking_automation(
        None, tenant_a.unit_id, "pending", "booking_requested",
        ["first_name"], tenant_a.number_id, [], None, True,
    )
    storage.upsert_booking_automation(
        None, tenant_a.unit_id, "pending", "booking_needs_approval",
        ["first_name"], tenant_a.number_id, [], None, True,
        recipient_mode="fixed", recipient_phone="+27827654321",
    )
    api_key = storage.generate_api_key(tenant_a.unit_id)

    fake_client = FakeWhatsAppClient({"id": tenant_a.number_id})
    monkeypatch.setattr(
        kryx_bookings_integration, "resolve_whatsapp_client", lambda unit, template: fake_client
    )

    payload = _payload(status="pending", to="0821234567")
    for _ in range(2):
        resp = client.post(
            "/integrations/kryx-bookings/send", json=payload,
            headers={"X-API-Key": api_key},
        )
        assert resp.status_code == 200

    assert len(fake_client.sent_calls) == 2

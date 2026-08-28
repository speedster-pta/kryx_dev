"""Tests for the calendar_link_suffix variable that
services/registration_poller.py::_process_registration_inner offers on
free_acknowledgment/payment_reminder templates once a signup has PCO
signup_times attached and the org's ical module is enabled - mirrors
services/serving_reminder.py's existing calendar-link integration (see
test_serving_reminder_combined.py) but for registration confirmations
rather than serving reminders.

No async test plugin (pytest-asyncio/anyio) is installed in this repo -
driven via asyncio.run() in plain sync test functions, same as
test_serving_reminder_combined.py."""
import asyncio
import uuid

import pytest

from autosend import storage
from autosend.services import registration_poller
from autosend.services.registration_poller import _process_registration_inner


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
    def __init__(self):
        self.number = {"id": 1, "default_region": "ZA"}
        self.sent_calls = []

    async def send_free_acknowledgment_template(self, **kwargs):
        self.sent_calls.append(kwargs)
        return {"messages": [{"id": "wamid.fake"}]}

    async def send_payment_template(self, **kwargs):
        self.sent_calls.append(kwargs)
        return {"messages": [{"id": "wamid.fake"}]}


def _unit_and_signup(is_paid=False, times=None):
    tag = uuid.uuid4().hex[:8]
    unit = {"id": abs(hash(tag)) % 100000 + 1, "org_id": abs(hash(tag + "org")) % 100000 + 1, "slug": f"unit-{tag}"}
    if times is None:
        times = [{"id": f"time-{tag}", "starts_at": "2026-09-01T15:00:00Z", "ends_at": "2026-09-01T18:00:00Z"}]
    signup = {
        "id": f"signup-{tag}",
        "name": "Youth Camp",
        "is_paid": is_paid,
        "times": times,
        "location": "Main Hall",
    }
    return unit, signup


@pytest.fixture
def enable_ical():
    enabled_orgs = []

    def _enable(org_id):
        if not storage.is_granted(org_id, storage.MODULE_ICAL):
            storage.grant(org_id, storage.MODULE_ICAL)
        storage.enable(org_id, storage.MODULE_ICAL)
        enabled_orgs.append(org_id)

    yield _enable
    for org_id in enabled_orgs:
        storage.disable(org_id, storage.MODULE_ICAL)


def _patch_clients(monkeypatch, pco_client, wa_client):
    monkeypatch.setattr(registration_poller, "get_pco_client", lambda unit: pco_client)
    monkeypatch.setattr(registration_poller, "resolve_whatsapp_client", lambda unit, template: wa_client)


class TestFreeAcknowledgmentCalendarLink:
    def test_calendar_link_suffix_attached_when_ical_enabled(self, monkeypatch, enable_ical):
        unit, signup = _unit_and_signup(is_paid=False)
        enable_ical(unit["org_id"])
        storage.upsert_registration_template(
            unit["id"], "free_acknowledgment", "free_ack_tmpl",
            ["first_name", "event_name"], None, ["calendar_link_suffix"], None, True,
        )
        pco_client = FakePcoClient(phone="+27821234567")
        wa_client = FakeWhatsAppClient()
        _patch_clients(monkeypatch, pco_client, wa_client)

        ctx = {"phone": None, "template_name": None, "whatsapp_number_id": None}
        asyncio.run(_process_registration_inner(unit, "reg-1", signup, ctx))

        call = wa_client.sent_calls[0]
        token = call["button_values"][0]
        assert token and not token.endswith(".ics")

        link = storage.get_ical_link_with_events(token)
        assert len(link["events"]) == 1
        assert link["events"][0]["title"] == "Youth Camp"
        assert link["events"][0]["location"] == "Main Hall"

    def test_no_calendar_link_when_ical_module_not_enabled(self, monkeypatch):
        unit, signup = _unit_and_signup(is_paid=False)
        # Deliberately not enabling storage.MODULE_ICAL for this org.
        storage.upsert_registration_template(
            unit["id"], "free_acknowledgment", "free_ack_tmpl",
            ["first_name", "event_name"], None, ["calendar_link_suffix"], None, True,
        )
        pco_client = FakePcoClient(phone="+27821234568")
        wa_client = FakeWhatsAppClient()
        _patch_clients(monkeypatch, pco_client, wa_client)

        ctx = {"phone": None, "template_name": None, "whatsapp_number_id": None}
        asyncio.run(_process_registration_inner(unit, "reg-2", signup, ctx))

        call = wa_client.sent_calls[0]
        # Button configured but its variable never resolved -> skipped, not a crash.
        assert call["button_values"] == [None]

    def test_no_calendar_link_when_signup_has_no_scheduled_time(self, monkeypatch, enable_ical):
        unit, signup = _unit_and_signup(is_paid=False, times=[])
        enable_ical(unit["org_id"])
        storage.upsert_registration_template(
            unit["id"], "free_acknowledgment", "free_ack_tmpl",
            ["first_name", "event_name"], None, ["calendar_link_suffix"], None, True,
        )
        pco_client = FakePcoClient(phone="+27821234569")
        wa_client = FakeWhatsAppClient()
        _patch_clients(monkeypatch, pco_client, wa_client)

        ctx = {"phone": None, "template_name": None, "whatsapp_number_id": None}
        asyncio.run(_process_registration_inner(unit, "reg-3", signup, ctx))

        call = wa_client.sent_calls[0]
        assert call["button_values"] == [None]


    def test_signup_with_multiple_dates_bundles_one_vevent_per_date(self, monkeypatch, enable_ical):
        # A signup with two distinct sessions (e.g. Friday evening AND
        # Sunday morning) must produce two separate calendar entries
        # bundled onto the one link, not a single event spanning the gap
        # between them.
        unit, signup = _unit_and_signup(
            is_paid=False,
            times=[
                {"id": "t1", "starts_at": "2026-09-04T18:00:00Z", "ends_at": "2026-09-04T20:00:00Z"},
                {"id": "t2", "starts_at": "2026-09-06T08:00:00Z", "ends_at": "2026-09-06T10:00:00Z"},
            ],
        )
        enable_ical(unit["org_id"])
        storage.upsert_registration_template(
            unit["id"], "free_acknowledgment", "free_ack_tmpl",
            ["first_name", "event_name"], None, ["calendar_link_suffix"], None, True,
        )
        pco_client = FakePcoClient(phone="+27821234572")
        wa_client = FakeWhatsAppClient()
        _patch_clients(monkeypatch, pco_client, wa_client)

        ctx = {"phone": None, "template_name": None, "whatsapp_number_id": None}
        asyncio.run(_process_registration_inner(unit, "reg-7", signup, ctx))

        token = wa_client.sent_calls[0]["button_values"][0]
        link = storage.get_ical_link_with_events(token)
        assert len(link["events"]) == 2
        starts = sorted(e["starts_at"] for e in link["events"])
        assert starts[0].startswith("2026-09-04")
        assert starts[1].startswith("2026-09-06")


class TestPaymentReminderCalendarLink:
    def test_calendar_link_suffix_available_for_paid_signups_too(self, monkeypatch, enable_ical):
        unit, signup = _unit_and_signup(is_paid=True)
        enable_ical(unit["org_id"])
        storage.upsert_registration_template(
            unit["id"], "payment_reminder", "payment_tmpl",
            ["first_name", "event_name", "total_due", "reference"], None,
            ["calendar_link_suffix"], None, True,
        )
        pco_client = FakePcoClient(phone="+27821234570")
        wa_client = FakeWhatsAppClient()
        _patch_clients(monkeypatch, pco_client, wa_client)

        ctx = {"phone": None, "template_name": None, "whatsapp_number_id": None}
        asyncio.run(_process_registration_inner(unit, "reg-4", signup, ctx))

        call = wa_client.sent_calls[0]
        token = call["button_values"][0]
        assert token and not token.endswith(".ics")

    def test_reused_link_token_across_registrations_for_same_signup(self, monkeypatch, enable_ical):
        # Two different registrants for the SAME signup, same phone (edge
        # case), should reuse the same link token rather than minting a
        # fresh one each time - mirrors get_or_create_ical_link's
        # idempotency-per-(link_key, recipient_phone) contract.
        unit, signup = _unit_and_signup(is_paid=False)
        enable_ical(unit["org_id"])
        storage.upsert_registration_template(
            unit["id"], "free_acknowledgment", "free_ack_tmpl",
            ["first_name", "event_name"], None, ["calendar_link_suffix"], None, True,
        )
        pco_client = FakePcoClient(phone="+27821234571")
        wa_client_1 = FakeWhatsAppClient()
        _patch_clients(monkeypatch, pco_client, wa_client_1)
        ctx = {"phone": None, "template_name": None, "whatsapp_number_id": None}
        asyncio.run(_process_registration_inner(unit, "reg-5", signup, ctx))
        token_1 = wa_client_1.sent_calls[0]["button_values"][0]

        wa_client_2 = FakeWhatsAppClient()
        _patch_clients(monkeypatch, pco_client, wa_client_2)
        ctx2 = {"phone": None, "template_name": None, "whatsapp_number_id": None}
        asyncio.run(_process_registration_inner(unit, "reg-6", signup, ctx2))
        token_2 = wa_client_2.sent_calls[0]["button_values"][0]

        assert token_1 == token_2

"""Regression tests for the "first registrant is silently eaten" bug in
services/registration_poller.py::_poll_signup: on the very first poll of a
brand-new signup (no watermark yet), the poller used to always treat every
existing registration as pre-existing backlog and advance the watermark past
the newest one without sending - correct when the signup has been open for a
while, but wrong when the signup and its genuine first registrant both
appear in the very same poll cycle, since that registrant then never gets a
confirmation. The fix splits first-sight registrations by created_at against
a grace-period cutoff instead of unconditionally treating "no watermark yet"
as "nothing but backlog".

No async test plugin (pytest-asyncio/anyio) is installed in this repo -
driven via asyncio.run() in plain sync test functions, same as
test_registration_poller_ical.py."""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from autosend import storage
from autosend.services import registration_poller
from autosend.services.registration_poller import _poll_signup


class FakePcoClient:
    def __init__(self, registrations, phone="+27821234567"):
        self._registrations = registrations
        self._phone = phone

    async def get_registrations_for_signup(self, signup_id, stop_at_registration_id):
        if stop_at_registration_id is None:
            return list(self._registrations)
        idx = next(
            (i for i, r in enumerate(self._registrations) if r["id"] == stop_at_registration_id),
            -1,
        )
        return list(self._registrations[idx + 1:])

    async def get_registration_detail(self, registration_id):
        return {
            "data": {"attributes": {"total_due_cents": 0}},
            "included": [
                {
                    "type": "Person",
                    "id": f"person-{registration_id}",
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


def _reg(reg_id, created_at):
    return {"id": reg_id, "attributes": {"created_at": created_at}}


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def _unit_and_signup():
    tag = uuid.uuid4().hex[:8]
    unit = {
        "id": abs(hash(tag)) % 100000 + 1,
        "org_id": abs(hash(tag + "org")) % 100000 + 1,
        "slug": f"unit-{tag}",
    }
    signup = {"id": f"signup-{tag}", "name": "Youth Camp", "is_paid": False, "times": [], "location": None}
    return unit, signup


def _patch_clients(monkeypatch, pco_client, wa_client):
    monkeypatch.setattr(registration_poller, "get_pco_client", lambda unit: pco_client)
    monkeypatch.setattr(registration_poller, "resolve_whatsapp_client", lambda unit, template: wa_client)


def _configure_template(unit):
    storage.upsert_registration_template(
        unit["id"], "free_acknowledgment", "free_ack_tmpl",
        ["first_name", "event_name"], None, [], None, True,
    )


def test_first_poll_sends_registration_within_grace_period(monkeypatch):
    unit, signup = _unit_and_signup()
    _configure_template(unit)
    fresh = _reg("reg-new", _iso(datetime.now(timezone.utc)))
    pco_client = FakePcoClient([fresh])
    wa_client = FakeWhatsAppClient()
    _patch_clients(monkeypatch, pco_client, wa_client)

    assert storage.get_signup_watermark(signup["id"]) is None
    asyncio.run(_poll_signup(unit, signup))

    assert len(wa_client.sent_calls) == 1
    assert storage.is_processed("reg-new")
    assert storage.get_signup_watermark(signup["id"]) == "reg-new"


def test_first_poll_skips_old_backlog_without_sending(monkeypatch):
    unit, signup = _unit_and_signup()
    _configure_template(unit)
    old = datetime.now(timezone.utc) - timedelta(days=30)
    backlog_reg = _reg("reg-old", _iso(old))
    pco_client = FakePcoClient([backlog_reg])
    wa_client = FakeWhatsAppClient()
    _patch_clients(monkeypatch, pco_client, wa_client)

    asyncio.run(_poll_signup(unit, signup))

    assert wa_client.sent_calls == []
    assert not storage.is_processed("reg-old")
    assert storage.get_signup_watermark(signup["id"]) == "reg-old"


def test_first_poll_with_mixed_backlog_and_fresh_sends_only_fresh(monkeypatch):
    unit, signup = _unit_and_signup()
    _configure_template(unit)
    old = datetime.now(timezone.utc) - timedelta(days=30)
    backlog_reg = _reg("reg-old-mixed", _iso(old))
    fresh_reg = _reg("reg-new-mixed", _iso(datetime.now(timezone.utc)))
    pco_client = FakePcoClient([backlog_reg, fresh_reg])
    wa_client = FakeWhatsAppClient()
    _patch_clients(monkeypatch, pco_client, wa_client)

    asyncio.run(_poll_signup(unit, signup))

    assert len(wa_client.sent_calls) == 1
    assert not storage.is_processed("reg-old-mixed")
    assert storage.is_processed("reg-new-mixed")
    assert storage.get_signup_watermark(signup["id"]) == "reg-new-mixed"


def test_missing_created_at_treated_as_backlog_not_sent(monkeypatch):
    unit, signup = _unit_and_signup()
    _configure_template(unit)
    unparseable_reg = {"id": "reg-unparseable", "attributes": {}}
    pco_client = FakePcoClient([unparseable_reg])
    wa_client = FakeWhatsAppClient()
    _patch_clients(monkeypatch, pco_client, wa_client)

    asyncio.run(_poll_signup(unit, signup))

    assert wa_client.sent_calls == []
    assert not storage.is_processed("reg-unparseable")
    assert storage.get_signup_watermark(signup["id"]) == "reg-unparseable"


def test_subsequent_poll_after_watermark_set_uses_normal_path(monkeypatch):
    # Sanity check the second (watermark-not-None) branch is untouched by
    # the baseline fix: a registration arriving after an already-set
    # watermark is fetched and sent exactly as before.
    unit, signup = _unit_and_signup()
    _configure_template(unit)
    storage.set_signup_watermark(signup["id"], "reg-already-seen")
    new_reg = _reg("reg-after-watermark", _iso(datetime.now(timezone.utc)))
    pco_client = FakePcoClient([
        _reg("reg-already-seen", _iso(datetime.now(timezone.utc) - timedelta(days=30))),
        new_reg,
    ])
    wa_client = FakeWhatsAppClient()
    _patch_clients(monkeypatch, pco_client, wa_client)

    asyncio.run(_poll_signup(unit, signup))

    assert len(wa_client.sent_calls) == 1
    assert storage.is_processed("reg-after-watermark")
    assert storage.get_signup_watermark(signup["id"]) == "reg-after-watermark"

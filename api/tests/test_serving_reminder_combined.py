"""Tests for the days_ahead (monthly-digest) combined-send path in
services/serving_reminder.py::_run_days_ahead_combined - one message per
person covering every plan they're scheduled on in the run, with one
calendar link bundling all of their events, rather than one message per
(plan, person) pair.

No async test plugin (pytest-asyncio/anyio) is installed in this repo -
driven via asyncio.run() in plain sync test functions instead of adding
a new dev dependency for this one module."""
import asyncio
import uuid

import pytest

from autosend import storage
from autosend.services.serving_reminder import _run_days_ahead_combined


class FakePcoClient:
    def __init__(self, team_members_by_plan, people_by_id):
        self._team_members_by_plan = team_members_by_plan
        self._people_by_id = people_by_id

    async def get_plan_team_members(self, service_type_id, plan_id):
        return self._team_members_by_plan.get(plan_id, [])

    async def get_person(self, person_id):
        return self._people_by_id[person_id]

    async def get_person_phone(self, person_id):
        return self._people_by_id[person_id]["_phone"]


class FakeWhatsAppClient:
    def __init__(self):
        self.number = {"default_region": "ZA"}
        self.sent_calls = []

    async def send_template(self, phone, template_name, *body_values, header_image_url=None, button_values=None):
        self.sent_calls.append({
            "phone": phone, "body_values": body_values, "button_values": button_values,
        })
        return {"messages": [{"id": "wamid.fake"}]}


def _person(person_id, first_name, phone):
    return {
        "data": {"attributes": {"first_name": first_name, "last_name": "Test", "name": f"{first_name} Test"}},
        "_phone": phone,
    }


def _unit_and_rule():
    tag = uuid.uuid4().hex[:8]
    unit = {"id": 1, "org_id": 1, "slug": f"unit-{tag}"}
    rule = {
        "id": 1, "pco_service_type_id": "st1", "pco_service_type_name": "Sunday Morning",
        "template_name": "monthly_digest", "header_image_url": None,
        "body_variable_order": ["first_name", "schedule_summary"],
        "button_variables": ["calendar_link_suffix"],
    }
    return unit, rule


@pytest.fixture(autouse=True)
def _enable_ical_module():
    if not storage.is_granted(1, storage.MODULE_ICAL):
        storage.grant(1, storage.MODULE_ICAL)
    storage.enable(1, storage.MODULE_ICAL)
    yield
    storage.disable(1, storage.MODULE_ICAL)


class TestCombinedSend:
    def test_person_scheduled_on_multiple_plans_gets_one_combined_message(self):
        unit, rule = _unit_and_rule()
        plans = [
            {"id": "plan-1", "title": "Sun 6 Sep", "dates": "Sun, 6 Sep", "sort_date": "2026-09-06T07:00:00Z"},
            {"id": "plan-2", "title": "Sun 13 Sep", "dates": "Sun, 13 Sep", "sort_date": "2026-09-13T07:00:00Z"},
        ]
        team_members_by_plan = {
            "plan-1": [{"person_id": "p1", "status": "C", "team_position_name": "Sound"}],
            "plan-2": [{"person_id": "p1", "status": "C", "team_position_name": "Usher"}],
        }
        people = {"p1": _person("p1", "Alex", "+27821234567")}
        pco_client = FakePcoClient(team_members_by_plan, people)
        wa_client = FakeWhatsAppClient()

        sent, skipped, failed, plan_summaries = asyncio.run(_run_days_ahead_combined(
            pco_client, wa_client, whatsapp_number_id=1, unit=unit, rule=rule,
            plans=plans, allowed_statuses={"C", "U"},
        ))

        assert (sent, skipped, failed) == (1, 0, 0)
        assert len(wa_client.sent_calls) == 1  # ONE message, not two
        call = wa_client.sent_calls[0]
        assert call["phone"] == "+27821234567"
        assert "Sound" in call["body_values"][1] and "Usher" in call["body_values"][1]
        token = call["button_values"][0]
        assert token and not token.endswith(".ics")

        # Both plan assignments for this person are marked sent, not just one.
        assert storage.is_serving_reminder_sent(rule["id"], "plan-1", "p1")
        assert storage.is_serving_reminder_sent(rule["id"], "plan-2", "p1")

        # The combined link bundles both events.
        link = storage.get_ical_link_with_events(token)
        assert len(link["events"]) == 2

    def test_rerun_does_not_resend_to_already_fully_sent_person(self):
        unit, rule = _unit_and_rule()
        rule["id"] = 2  # isolate from the previous test's log rows
        plans = [{"id": "plan-3", "title": "Sun 20 Sep", "dates": "Sun, 20 Sep", "sort_date": "2026-09-20T07:00:00Z"}]
        team_members_by_plan = {"plan-3": [{"person_id": "p2", "status": "C", "team_position_name": "Sound"}]}
        people = {"p2": _person("p2", "Sam", "+27827654321")}
        pco_client = FakePcoClient(team_members_by_plan, people)
        wa_client = FakeWhatsAppClient()

        asyncio.run(_run_days_ahead_combined(
            pco_client, wa_client, whatsapp_number_id=1, unit=unit, rule=rule,
            plans=plans, allowed_statuses={"C", "U"},
        ))
        assert len(wa_client.sent_calls) == 1

        sent, skipped, failed, _ = asyncio.run(_run_days_ahead_combined(
            pco_client, wa_client, whatsapp_number_id=1, unit=unit, rule=rule,
            plans=plans, allowed_statuses={"C", "U"},
        ))
        assert (sent, skipped, failed) == (0, 1, 0)
        assert len(wa_client.sent_calls) == 1  # still just the one send from before

    def test_reused_link_token_on_rerun_with_same_plan_set(self):
        unit, rule = _unit_and_rule()
        rule["id"] = 3
        plans = [{"id": "plan-4", "title": "Sun 27 Sep", "dates": "Sun, 27 Sep", "sort_date": "2026-09-27T07:00:00Z"}]
        team_members_by_plan = {"plan-4": [{"person_id": "p3", "status": "U", "team_position_name": "Sound"}]}
        people = {"p3": _person("p3", "Jo", "+27829876543")}
        pco_client = FakePcoClient(team_members_by_plan, people)

        wa_client_1 = FakeWhatsAppClient()
        asyncio.run(_run_days_ahead_combined(
            pco_client, wa_client_1, whatsapp_number_id=1, unit=unit, rule=rule,
            plans=plans, allowed_statuses={"C", "U"},
        ))
        token_1 = wa_client_1.sent_calls[0]["button_values"][0]

        # Manually mark the send as deferred to simulate a legitimate
        # retry scenario without needing to fake MessagingLimitExceeded -
        # the token should still be reused, not regenerated, since the
        # plan set (link_key) is unchanged.
        storage.mark_serving_reminder(rule["id"], "plan-4", "p3", "deferred")
        wa_client_2 = FakeWhatsAppClient()
        asyncio.run(_run_days_ahead_combined(
            pco_client, wa_client_2, whatsapp_number_id=1, unit=unit, rule=rule,
            plans=plans, allowed_statuses={"C", "U"},
        ))
        token_2 = wa_client_2.sent_calls[0]["button_values"][0]
        assert token_1 == token_2

"""Regression test for the serving-reminder calendar-link UTC offset bug:
services/serving_reminder.py used to build ical events straight from PCO's
Plan `sort_date`, which is PCO Services' own wall-clock time mislabelled
with a "Z" suffix rather than a true UTC instant - for a SAST (UTC+2) org
this rendered every "add to calendar" link two hours off. The fix fetches
the Plan's actual PlanTime objects (a true UTC instant) via
PlanningCenterClient.get_plan_times and uses that instead.

Drives _run_for_plan directly (the next_event/single-plan send path;
retry_deferred_plan and run_serving_reminder_rule both funnel through it
too), the same way test_serving_reminder_combined.py drives
_run_days_ahead_combined - via asyncio.run() since no async test plugin is
installed in this repo."""
import asyncio
import uuid

from autosend import storage
from autosend.services.serving_reminder import _run_for_plan


class FakePcoClient:
    def __init__(self, team_members, plan_times, people_by_id):
        self._team_members = team_members
        self._plan_times = plan_times
        self._people_by_id = people_by_id

    async def get_plan_team_members(self, service_type_id, plan_id):
        return self._team_members

    async def get_plan_times(self, service_type_id, plan_id):
        return self._plan_times

    async def get_person(self, person_id):
        return self._people_by_id[person_id]

    async def get_person_phone(self, person_id):
        return self._people_by_id[person_id]["_phone"]


class FakeWhatsAppClient:
    def __init__(self):
        self.number = {"default_region": "ZA"}
        self.sent_calls = []

    async def send_template(self, phone, template_name, *body_values, header_image_url=None, button_values=None, language="en"):
        self.sent_calls.append({"phone": phone, "body_values": body_values, "button_values": button_values})
        # A fresh wamid per call - conversation_messages.wamid is UNIQUE,
        # same as real Meta wamids would be across distinct sends.
        return {"messages": [{"id": f"wamid.fake-{uuid.uuid4().hex[:8]}"}]}


def _person(person_id, first_name, phone):
    return {
        "data": {"attributes": {"first_name": first_name, "last_name": "Test", "name": f"{first_name} Test"}},
        "_phone": phone,
    }


def _unit_and_rule():
    # A real organisations/units row (not just a plain dict) - needed
    # since _run_for_plan now also mirrors a successful send into the
    # Inbox (storage.mirror_outbound_to_inbox), which resolves/creates a
    # conversations row via a query that joins against a real units row.
    tag = uuid.uuid4().hex[:8]
    org = storage.create_organisation(f"Org {tag}", f"org-{tag}")
    unit_id = storage.get_unit_ids_for_org(org.id)[0]
    if not storage.is_granted(org.id, storage.MODULE_ICAL):
        storage.grant(org.id, storage.MODULE_ICAL)
    storage.enable(org.id, storage.MODULE_ICAL)
    unit = {"id": unit_id, "org_id": org.id, "slug": f"unit-{tag}"}
    rule = {
        "id": 1, "pco_service_type_id": "st1", "pco_service_type_name": "Sunday Morning",
        "template_name": "serving_reminder", "header_image_url": None,
        "body_variable_order": ["first_name"],
        "button_variables": ["calendar_link_suffix"],
    }
    return unit, rule


class TestServingReminderIcalUsesRealPlanTime:
    def test_calendar_event_uses_plan_time_not_sort_date(self):
        """A SAST (UTC+2) org's plan has sort_date "18:00:00Z" (PCO's own
        wall-clock time mislabelled as UTC) but a true PlanTime starts_at
        of "16:00:00Z" - the actual UTC instant for 18:00 SAST. Under the
        old code (which built the ical event from sort_date directly), the
        calendar link would have been created for 18:00 UTC - two hours
        later than the real service time. The fix must use the PlanTime's
        starts_at instead."""
        unit, rule = _unit_and_rule()
        plan = {
            "id": "plan-offset-1", "title": "Sunday Service",
            "dates": "Sun, 6 Sep", "sort_date": "2026-09-06T18:00:00Z",
        }
        true_starts_at = "2026-09-06T16:00:00Z"
        plan_times = [
            {"id": "pt-1", "starts_at": true_starts_at, "ends_at": "2026-09-06T18:00:00Z", "time_type": "service"},
        ]
        team_members = [{"person_id": "p1", "status": "C", "team_position_name": "Sound"}]
        people = {"p1": _person("p1", "Alex", "+27821234567")}
        pco_client = FakePcoClient(team_members, plan_times, people)
        wa_client = FakeWhatsAppClient()

        sent, skipped, failed, limit_hit, error = asyncio.run(_run_for_plan(
            pco_client, wa_client, whatsapp_number_id=1, unit=unit, rule=rule, plan=plan,
            allowed_statuses={"C", "U"}, limit_hit=False,
        ))

        assert (sent, skipped, failed, error) == (1, 0, 0, None)
        token = wa_client.sent_calls[0]["button_values"][0]
        link = storage.get_ical_link_with_events(token)
        assert len(link["events"]) == 1
        assert link["events"][0]["starts_at"] == true_starts_at
        assert link["events"][0]["starts_at"] != plan["sort_date"]

    def test_prefers_service_time_type_over_rehearsal(self):
        """When a plan has several PlanTimes (e.g. a rehearsal block plus
        the actual service), the reminder's calendar link must point at
        the time_type="service" entry, not whichever happens to sort
        earliest overall."""
        unit, rule = _unit_and_rule()
        rule["id"] = 2
        plan = {
            "id": "plan-offset-2", "title": "Sunday Service",
            "dates": "Sun, 13 Sep", "sort_date": "2026-09-13T18:00:00Z",
        }
        rehearsal_starts_at = "2026-09-13T14:00:00Z"
        service_starts_at = "2026-09-13T16:00:00Z"
        plan_times = [
            {"id": "pt-rehearsal", "starts_at": rehearsal_starts_at, "ends_at": "2026-09-13T15:00:00Z", "time_type": "rehearsal"},
            {"id": "pt-service", "starts_at": service_starts_at, "ends_at": "2026-09-13T18:00:00Z", "time_type": "service"},
        ]
        team_members = [{"person_id": "p2", "status": "C", "team_position_name": "Usher"}]
        people = {"p2": _person("p2", "Sam", "+27827654321")}
        pco_client = FakePcoClient(team_members, plan_times, people)
        wa_client = FakeWhatsAppClient()

        asyncio.run(_run_for_plan(
            pco_client, wa_client, whatsapp_number_id=1, unit=unit, rule=rule, plan=plan,
            allowed_statuses={"C", "U"}, limit_hit=False,
        ))

        token = wa_client.sent_calls[0]["button_values"][0]
        link = storage.get_ical_link_with_events(token)
        assert link["events"][0]["starts_at"] == service_starts_at

    def test_no_plan_time_omits_calendar_link_without_crashing(self):
        """A plan with no PlanTime at all (PCO hasn't attached a scheduled
        time yet) should omit the calendar_link_suffix field entirely -
        same "just isn't available this time" handling as any other
        optional field - rather than failing the send outright.
        `button_variables` still lists calendar_link_suffix here, so with
        button_values built via `if key else None` this asserts the
        button slot resolves to a falsy value, not that the field is
        silently dropped from a strict lookup."""
        unit, rule = _unit_and_rule()
        rule["id"] = 3
        rule["button_variables"] = []  # avoid a hard failure on the missing field
        plan = {"id": "plan-offset-3", "title": "Sunday Service", "dates": "Sun, 20 Sep", "sort_date": "2026-09-20T18:00:00Z"}
        team_members = [{"person_id": "p3", "status": "C", "team_position_name": "Sound"}]
        people = {"p3": _person("p3", "Jo", "+27829876543")}
        pco_client = FakePcoClient(team_members, [], people)
        wa_client = FakeWhatsAppClient()

        sent, skipped, failed, limit_hit, error = asyncio.run(_run_for_plan(
            pco_client, wa_client, whatsapp_number_id=1, unit=unit, rule=rule, plan=plan,
            allowed_statuses={"C", "U"}, limit_hit=False,
        ))

        assert (sent, skipped, failed, error) == (1, 0, 0, None)

"""Tests for plan_selection_mode='next_calendar_month' - the calendar-
aligned serving-reminder window (services/serving_reminder.py::
_next_calendar_month_range), as opposed to 'days_ahead's rolling
fixed-day-offset window.

_next_calendar_month_range is a pure function (no PCO/network calls), so
it's tested directly rather than through run_serving_reminder_rule -
same reasoning as test_serving_reminder_combined.py driving
_run_days_ahead_combined directly instead of the full rule-run path."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from autosend import storage
from autosend.services.serving_reminder import _next_calendar_month_range


class TestNextCalendarMonthRange:
    def test_mid_month_returns_first_of_next_month_through_first_of_month_after(self):
        # 15 Nov 2026, 12:00 UTC = 14:00 SAST (Africa/Johannesburg, UTC+2,
        # no DST) - well inside November, so the window must start at the
        # beginning of December, not drift from "15 days ahead".
        now_utc = datetime(2026, 11, 15, 12, 0, 0, tzinfo=timezone.utc)
        start, end = _next_calendar_month_range(now_utc, "Africa/Johannesburg")

        # 1 Dec 2026 00:00 SAST == 30 Nov 2026 22:00 UTC
        assert start == datetime(2026, 11, 30, 22, 0, 0, tzinfo=timezone.utc)
        # 1 Jan 2027 00:00 SAST == 31 Dec 2026 22:00 UTC
        assert end == datetime(2026, 12, 31, 22, 0, 0, tzinfo=timezone.utc)

    def test_end_of_month_still_targets_next_month_not_the_remainder_of_this_one(self):
        # Running late on the very last day of November (still 30 Nov,
        # 23:30 SAST) must still skip the rest of November entirely - a
        # fixed 30-day offset from "now" would land in early January
        # instead of covering all of December.
        now_utc = datetime(2026, 11, 30, 21, 30, 0, tzinfo=timezone.utc)
        start, end = _next_calendar_month_range(now_utc, "Africa/Johannesburg")

        assert start == datetime(2026, 11, 30, 22, 0, 0, tzinfo=timezone.utc)
        assert end == datetime(2026, 12, 31, 22, 0, 0, tzinfo=timezone.utc)

    def test_covers_the_full_next_month_regardless_of_its_length(self):
        # December (31 days) rolling into January (31 days) and February
        # 2027 (28 days, non-leap) rolling into March (31 days) - the
        # window width must track whichever month is actually next, not
        # a fixed day count.
        december_run = _next_calendar_month_range(
            datetime(2026, 12, 10, tzinfo=timezone.utc), "Africa/Johannesburg",
        )
        assert (december_run[1] - december_run[0]).days == 31  # January

        january_run = _next_calendar_month_range(
            datetime(2027, 1, 10, tzinfo=timezone.utc), "Africa/Johannesburg",
        )
        assert (january_run[1] - january_run[0]).days == 28  # February 2027

    def test_december_run_wraps_into_january_of_the_next_year(self):
        now_utc = datetime(2026, 12, 5, tzinfo=timezone.utc)
        start, end = _next_calendar_month_range(now_utc, "Africa/Johannesburg")

        # 1 Jan 2027 00:00 SAST and 1 Feb 2027 00:00 SAST, as UTC instants.
        start_local = start.astimezone(ZoneInfo("Africa/Johannesburg"))
        end_local = end.astimezone(ZoneInfo("Africa/Johannesburg"))
        assert (start_local.year, start_local.month, start_local.day) == (2027, 1, 1)
        assert (end_local.year, end_local.month, end_local.day) == (2027, 2, 1)

    def test_falls_back_to_utc_for_an_unrecognized_timezone(self):
        now_utc = datetime(2026, 11, 15, 12, 0, 0, tzinfo=timezone.utc)
        start, end = _next_calendar_month_range(now_utc, "Not/ARealZone")

        assert start == datetime(2026, 12, 1, 0, 0, 0, tzinfo=timezone.utc)
        assert end == datetime(2027, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


class TestNextCalendarMonthModeAccepted:
    def test_plan_selection_modes_includes_next_calendar_month(self):
        assert "next_calendar_month" in storage.SERVING_PLAN_SELECTION_MODES

    def test_upsert_serving_rule_accepts_next_calendar_month_without_days_ahead(self, tenants):
        tenant_a, _tenant_b = tenants
        rule_id = storage.upsert_serving_rule(
            rule_id=None, unit_id=tenant_a.unit_id, pco_service_type_id="svc-cal",
            pco_service_type_name="Sunday Service", send_day_of_week="sun",
            send_time="08:00", timezone_name="Africa/Johannesburg",
            status_filter="confirmed_only", template_name="calendar_month_template",
            body_variable_order=[], whatsapp_number_id=None, button_variables=[],
            header_image_url=None, active=True,
            plan_selection_mode="next_calendar_month",
        )
        rule = storage.get_serving_rule_by_id(rule_id)
        assert rule["plan_selection_mode"] == "next_calendar_month"
        assert rule["days_ahead"] is None
        storage.delete_serving_rule(rule_id)

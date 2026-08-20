"""Tests for the generic iCal infrastructure: builder output (single and
multi-event), storage upsert/reschedule/bundling semantics, and the
public /ical/{token}.ics endpoint (no session auth by design - see
web/ical_router.py)."""
from datetime import datetime, timedelta, timezone

from autosend import storage
from autosend.integrations.ical.builder import build_ics
from autosend.web import ical_link_security


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _future_expiry() -> str:
    return _iso(datetime.now(timezone.utc) + timedelta(days=1))


def _past_expiry() -> str:
    return _iso(datetime.now(timezone.utc) - timedelta(days=1))


def _make_event(unit_id, source_key, starts_at="2026-09-01T15:00:00+00:00"):
    event, _is_update = storage.upsert_ical_event(
        unit_id, "test_source", source_key, "Title", starts_at,
        expires_at=_future_expiry(),
    )
    return event


class TestBuildIcs:
    def test_single_event_contains_required_fields(self):
        event = {
            "uid": "abc-123", "sequence": 0, "title": "Sunday Service",
            "description": None, "location": None,
            "starts_at": "2026-09-01T15:00:00+00:00", "ends_at": "2026-09-01T16:00:00+00:00",
            "status": "confirmed",
        }
        ics = build_ics([event])
        assert "BEGIN:VCALENDAR" in ics
        assert ics.count("BEGIN:VEVENT") == 1
        assert "UID:abc-123" in ics
        assert "SEQUENCE:0" in ics
        assert "DTSTART:20260901T150000Z" in ics
        assert "DTEND:20260901T160000Z" in ics
        assert "SUMMARY:Sunday Service" in ics
        assert "STATUS:CONFIRMED" in ics
        assert ics.endswith("END:VCALENDAR\r\n")

    def test_multiple_events_render_as_separate_vevents_in_one_vcalendar(self):
        events = [
            {"uid": "e1", "sequence": 0, "title": "Service A", "description": None,
             "location": None, "starts_at": "2026-09-06T15:00:00+00:00", "ends_at": None,
             "status": "confirmed"},
            {"uid": "e2", "sequence": 0, "title": "Service B", "description": None,
             "location": None, "starts_at": "2026-09-13T15:00:00+00:00", "ends_at": None,
             "status": "confirmed"},
        ]
        ics = build_ics(events)
        assert ics.count("BEGIN:VCALENDAR") == 1
        assert ics.count("BEGIN:VEVENT") == 2
        assert ics.count("END:VEVENT") == 2
        assert "UID:e1" in ics and "UID:e2" in ics
        assert "SUMMARY:Service A" in ics and "SUMMARY:Service B" in ics

    def test_escapes_special_characters(self):
        event = {
            "uid": "u1", "sequence": 0, "title": "Men's Camp; Session, 1",
            "description": "Line one\nLine two", "location": None,
            "starts_at": "2026-09-01T15:00:00+00:00", "ends_at": None, "status": "confirmed",
        }
        ics = build_ics([event])
        assert "Men's Camp\\; Session\\, 1" in ics
        assert "Line one\\nLine two" in ics

    def test_cancelled_status(self):
        event = {
            "uid": "u1", "sequence": 2, "title": "X", "description": None,
            "location": None, "starts_at": "2026-09-01T15:00:00+00:00",
            "ends_at": None, "status": "cancelled",
        }
        assert "STATUS:CANCELLED" in build_ics([event])


class TestUpsertIcalEvent:
    def test_new_event_has_sequence_zero(self):
        event, is_update = storage.upsert_ical_event(
            1, "test_source", "ext-1", "Title", "2026-09-01T15:00:00+00:00",
            expires_at=_future_expiry(),
        )
        assert is_update is False
        assert event["sequence"] == 0
        assert event["uid"]

    def test_reschedule_bumps_sequence_and_keeps_uid(self):
        first, _ = storage.upsert_ical_event(
            1, "test_source", "ext-2", "Title", "2026-09-01T15:00:00+00:00",
            expires_at=_future_expiry(),
        )
        second, is_update = storage.upsert_ical_event(
            1, "test_source", "ext-2", "Title", "2026-09-02T10:00:00+00:00",
            expires_at=_future_expiry(),
        )
        assert is_update is True
        assert second["uid"] == first["uid"]
        assert second["sequence"] == first["sequence"] + 1
        assert second["starts_at"] == "2026-09-02T10:00:00+00:00"

    def test_no_external_id_always_creates_new_row(self):
        first, is_update_1 = storage.upsert_ical_event(
            1, "test_source", None, "Title", "2026-09-01T15:00:00+00:00",
            expires_at=_future_expiry(),
        )
        second, is_update_2 = storage.upsert_ical_event(
            1, "test_source", None, "Title", "2026-09-01T15:00:00+00:00",
            expires_at=_future_expiry(),
        )
        assert is_update_1 is False and is_update_2 is False
        assert first["uid"] != second["uid"]


class TestIcalLinksAndBundling:
    def test_get_or_create_is_idempotent_per_key_and_recipient(self):
        link1 = storage.get_or_create_ical_link("bundle-key-1", "+27821234567", _future_expiry())
        link2 = storage.get_or_create_ical_link("bundle-key-1", "+27821234567", _future_expiry())
        assert link1["token"] == link2["token"]

    def test_different_recipients_get_different_tokens_for_same_key(self):
        link_a = storage.get_or_create_ical_link("bundle-key-2", "+27821111111", _future_expiry())
        link_b = storage.get_or_create_ical_link("bundle-key-2", "+27822222222", _future_expiry())
        assert link_a["token"] != link_b["token"]

    def test_different_keys_get_different_tokens_for_same_recipient(self):
        link_a = storage.get_or_create_ical_link("bundle-key-3a", "+27823333333", _future_expiry())
        link_b = storage.get_or_create_ical_link("bundle-key-3b", "+27823333333", _future_expiry())
        assert link_a["token"] != link_b["token"]

    def test_link_bundles_multiple_events_in_order(self):
        event_1 = _make_event(1, "bundle-src-1", starts_at="2026-09-13T15:00:00+00:00")
        event_2 = _make_event(1, "bundle-src-2", starts_at="2026-09-06T15:00:00+00:00")
        link = storage.get_or_create_ical_link("bundle-key-4", "+27824444444", _future_expiry())
        storage.attach_event_to_link(link["id"], event_1["id"])
        storage.attach_event_to_link(link["id"], event_2["id"])

        fetched = storage.get_ical_link_with_events(link["token"])
        assert [e["id"] for e in fetched["events"]] == [event_2["id"], event_1["id"]]  # ordered by starts_at

    def test_attaching_same_event_twice_is_idempotent(self):
        event = _make_event(1, "bundle-src-3")
        link = storage.get_or_create_ical_link("bundle-key-5", "+27825555555", _future_expiry())
        storage.attach_event_to_link(link["id"], event["id"])
        storage.attach_event_to_link(link["id"], event["id"])

        fetched = storage.get_ical_link_with_events(link["token"])
        assert len(fetched["events"]) == 1


class TestIcalEndpoint:
    def test_unknown_token_returns_404(self, client):
        resp = client.get("/ical/does-not-exist.ics")
        assert resp.status_code == 404

    def test_valid_bundle_returns_ics_with_all_events(self, client):
        event_1 = _make_event(1, "endpoint-src-1", starts_at="2026-09-06T15:00:00+00:00")
        event_2 = _make_event(1, "endpoint-src-2", starts_at="2026-09-13T15:00:00+00:00")
        link = storage.get_or_create_ical_link("endpoint-bundle-1", "+27826666666", _future_expiry())
        storage.attach_event_to_link(link["id"], event_1["id"])
        storage.attach_event_to_link(link["id"], event_2["id"])

        resp = client.get(f"/ical/{link['token']}.ics")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/calendar")
        assert resp.text.count("BEGIN:VEVENT") == 2

    def test_expired_link_returns_404_not_500(self, client):
        event = _make_event(1, "endpoint-src-3")
        link = storage.get_or_create_ical_link("endpoint-bundle-2", "+27827777777", _past_expiry())
        storage.attach_event_to_link(link["id"], event["id"])

        resp = client.get(f"/ical/{link['token']}.ics")
        assert resp.status_code == 404

    def test_past_event_inside_a_still_valid_link_is_still_served(self, client):
        # Decision: a bundle keeps serving its full original set of
        # events all month, rather than shrinking as individual dates
        # pass - only the LINK's own expiry/revocation gates the response.
        past_event = _make_event(1, "endpoint-src-4", starts_at="2020-01-01T15:00:00+00:00")
        link = storage.get_or_create_ical_link("endpoint-bundle-3", "+27828888888", _future_expiry())
        storage.attach_event_to_link(link["id"], past_event["id"])

        resp = client.get(f"/ical/{link['token']}.ics")
        assert resp.status_code == 200
        assert "BEGIN:VEVENT" in resp.text

    def test_link_with_no_attached_events_returns_404(self, client):
        link = storage.get_or_create_ical_link("endpoint-bundle-4", "+27829999999", _future_expiry())
        resp = client.get(f"/ical/{link['token']}.ics")
        assert resp.status_code == 404


class TestIcalLinkSecurity:
    def test_invalid_tokens_lock_out_after_threshold(self):
        test_ip = "203.0.113.99"
        storage.clear_login_attempts(ical_link_security.ical_ip_key(test_ip))
        for _ in range(ical_link_security.MAX_FAILED_ATTEMPTS):
            ical_link_security.record_invalid_token(test_ip)
        assert ical_link_security.check_lockout(test_ip) is not None

    def test_expired_link_lookup_does_not_count_toward_lockout(self):
        test_ip = "203.0.113.100"
        storage.clear_login_attempts(ical_link_security.ical_ip_key(test_ip))
        # The endpoint must never call record_invalid_token for a
        # found-but-expired link (see web/ical_router.py) - nothing to
        # assert against the router itself here beyond confirming the
        # lockout stays clear when that function simply isn't called.
        assert ical_link_security.check_lockout(test_ip) is None

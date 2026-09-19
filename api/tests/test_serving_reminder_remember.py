"""Tests for ServingRuleIn.remember (web/automations_router.py) - a
one-time "send now" serving reminder (schedule_type='immediate') normally
leaves its rule row behind forever, cluttering the Automations list.
remember=False deletes the rule (and its now-pointless serving_reminder_log
dedup rows, which - unlike the schema's decorative ON DELETE CASCADE
annotation - storage.delete_serving_rule now removes explicitly, since
this codebase's sqlite connections never turn PRAGMA foreign_keys on)
right after the send completes, while send_log/History keeps showing what
was actually sent, since send_log has no FK back to the rule at all.

Drives this through the real HTTP endpoint (TestClient), per this repo's
tenant-boundary testing convention, with
autosend.services.serving_reminder.run_serving_reminder_rule monkeypatched
to a fake that mimics a real send's storage side-effects (marking the
dedup log, recording to send_log) without needing a real PCO/WhatsApp
client."""
import autosend.services.serving_reminder as serving_reminder_module
from autosend import storage


def _fake_run_serving_reminder_rule_factory(captured_rule_ids: list):
    """Returns an async fake with the same (rule_id) -> summary-dict
    contract as the real run_serving_reminder_rule, mimicking its two
    storage side-effects (serving_reminder_log dedup row + send_log entry)
    so the remember=False cleanup path has something real to clean up.
    Appends the rule_id it was called with to captured_rule_ids so the
    test can check the dedup log by that exact id afterward, without
    needing the (deleted, in the remember=False case) rule row itself."""
    async def _fake(rule_id: int) -> dict:
        rule = storage.get_serving_rule_by_id(rule_id)
        captured_rule_ids.append(rule_id)
        storage.mark_serving_reminder(rule_id, "plan-remember-test", "person-remember-test", "sent")
        storage.record_send(
            unit_id=rule["unit_id"], source="serving_reminder", status="sent",
            template_name=rule["template_name"], recipient_phone="+27821234567",
        )
        return {"plans": [{"id": "plan-remember-test", "sent": 1, "skipped": 0, "failed": 0, "error": None}],
                "sent": 1, "skipped": 0, "failed": 0}
    return _fake


def _immediate_payload(tenant, *, remember: bool, template_name: str) -> dict:
    return {
        "id": None,
        "unit_id": tenant.unit_id,
        "pco_service_type_id": "svc-remember",
        "pco_service_type_name": "Sunday Service",
        "schedule_type": "immediate",
        "status_filter": "confirmed_only",
        "template_name": template_name,
        "body_variable_order": [],
        "whatsapp_number_id": tenant.number_id,
        "button_variables": [],
        "active": True,
        "plan_selection_mode": "next_event",
        "remember": remember,
    }


class TestRememberFlag:
    def test_remember_false_deletes_rule_and_dedup_log_but_keeps_send_log(self, client, login_as, tenants, monkeypatch):
        tenant_a, _tenant_b = tenants
        storage.grant(tenant_a.org_id, storage.MODULE_PCO)
        storage.enable(tenant_a.org_id, storage.MODULE_PCO)
        captured_rule_ids: list = []
        monkeypatch.setattr(
            serving_reminder_module, "run_serving_reminder_rule",
            _fake_run_serving_reminder_rule_factory(captured_rule_ids),
        )

        login_as(client, tenant_a.org_admin_username)
        res = client.post(
            "/api/automations/serving-rules",
            json=_immediate_payload(tenant_a, remember=False, template_name="forget_me_template"),
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["remembered"] is False
        assert body["id"] is None
        assert body["send_result"]["sent"] == 1

        # The rule row (and its synthetic whatsapp_templates row) is gone.
        rules = storage.list_serving_rules([tenant_a.unit_id])
        assert not any(r["template_name"] == "forget_me_template" for r in rules)

        # Its dedup log row is gone too - the real run's mark_serving_reminder
        # call above would otherwise leave "person-remember-test" recorded
        # as sent forever against a rule id that no longer exists.
        assert len(captured_rule_ids) == 1
        sent_rule_id = captured_rule_ids[0]
        assert not storage.is_serving_reminder_sent(sent_rule_id, "plan-remember-test", "person-remember-test")

        # send_log still shows the send happened - History has no FK back
        # to the now-deleted rule.
        sends = storage.get_recent_sends(unit_ids=[tenant_a.unit_id], limit=50)
        assert any(s["template_name"] == "forget_me_template" and s["status"] == "sent" for s in sends)

    def test_remember_true_keeps_the_rule_after_sending(self, client, login_as, tenants, monkeypatch):
        tenant_a, _tenant_b = tenants
        storage.grant(tenant_a.org_id, storage.MODULE_PCO)
        storage.enable(tenant_a.org_id, storage.MODULE_PCO)
        monkeypatch.setattr(
            serving_reminder_module, "run_serving_reminder_rule",
            _fake_run_serving_reminder_rule_factory([]),
        )

        login_as(client, tenant_a.org_admin_username)
        res = client.post(
            "/api/automations/serving-rules",
            json=_immediate_payload(tenant_a, remember=True, template_name="remember_me_template"),
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["remembered"] is True
        assert body["id"] is not None

        rules = storage.list_serving_rules([tenant_a.unit_id])
        matching = [r for r in rules if r["template_name"] == "remember_me_template"]
        assert len(matching) == 1
        assert matching[0]["id"] == body["id"]

        storage.delete_serving_rule(body["id"])


class TestDeleteServingRuleCleansUpDedupLog:
    """Storage-level check that delete_serving_rule (called both by the
    remember=False path above and the plain DELETE endpoint) explicitly
    removes serving_reminder_log rows - the schema's ON DELETE CASCADE on
    that FK is never actually enforced in this codebase (no connection
    ever runs PRAGMA foreign_keys = ON), so without this explicit delete
    the dedup rows would silently outlive the rule they belong to."""

    def test_dedup_log_rows_removed_when_rule_is_deleted(self, tenants):
        tenant_a, _tenant_b = tenants
        rule_id = storage.upsert_serving_rule(
            rule_id=None, unit_id=tenant_a.unit_id, pco_service_type_id="svc-cleanup",
            pco_service_type_name="Sunday Service", send_day_of_week="sun",
            send_time="08:00", timezone_name="Africa/Johannesburg",
            status_filter="confirmed_only", template_name="cleanup_template",
            body_variable_order=[], whatsapp_number_id=None, button_variables=[],
            header_image_url=None, active=True,
        )
        storage.mark_serving_reminder(rule_id, "plan-cleanup", "person-cleanup", "sent")
        assert storage.is_serving_reminder_sent(rule_id, "plan-cleanup", "person-cleanup")

        storage.delete_serving_rule(rule_id)

        assert storage.get_serving_rule_by_id(rule_id) is None
        assert not storage.is_serving_reminder_sent(rule_id, "plan-cleanup", "person-cleanup")

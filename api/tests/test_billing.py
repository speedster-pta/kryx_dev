"""Unit-level coverage for billing/engine.py's compute_total_cents (plan +
add-on + coupon arithmetic - no HTTP/DB side effects worth exercising
through a real request) and storage.billing.is_org_current (the choke
point every send-triggering call site now also checks alongside
storage.is_org_active - see integrations/webhooks.py,
web/campaign_runner.py, scheduler.py, services/registration_poller.py,
services/serving_reminder.py, services/sme_metrics.py,
services/email_wa.py).

Uses the `tenants` fixture (two independent orgs) from conftest.py for
is_org_current, same as every other cross-org test in this suite -
plans/add-ons/coupons are inserted directly via storage/_db._connect()
since there's no admin-model fixture for the billing catalogue tables."""
from datetime import datetime, timedelta, timezone

import pytest

from autosend import storage
from autosend.billing.engine import compute_total_cents
from autosend.storage._db import _connect


def _insert_plan(key: str, price_cents: int) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO billing_plans (key, name, price_cents) VALUES (?, ?, ?)",
            (key, key, price_cents),
        )


def _insert_addon(key: str, price_cents: int) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO billing_addons (key, name, price_cents) VALUES (?, ?, ?)",
            (key, key, price_cents),
        )


def _insert_coupon(code: str, kind: str, amount: int, *, expires_at: str | None = None,
                    max_redemptions: int | None = None, redemption_count: int = 0) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO coupons (code, kind, amount, expires_at, max_redemptions, redemption_count)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (code, kind, amount, expires_at, max_redemptions, redemption_count),
        )


class TestComputeTotalCents:
    def test_plan_only(self):
        _insert_plan("plan-basic", 10_000)
        assert compute_total_cents("plan-basic", [], None) == 10_000

    def test_plan_plus_addons(self):
        _insert_plan("plan-plus-addons", 10_000)
        _insert_addon("addon-a", 2_000)
        _insert_addon("addon-b", 3_000)
        total = compute_total_cents("plan-plus-addons", ["addon-a", "addon-b"], None)
        assert total == 15_000

    def test_percent_coupon(self):
        _insert_plan("plan-percent", 10_000)
        _insert_coupon("PERCENT10", "percent", 10)
        total = compute_total_cents("plan-percent", [], "PERCENT10")
        assert total == 9_000

    def test_fixed_coupon(self):
        _insert_plan("plan-fixed", 10_000)
        _insert_coupon("FIXED500", "fixed", 500)
        total = compute_total_cents("plan-fixed", [], "FIXED500")
        assert total == 9_500

    def test_fixed_coupon_floors_at_zero(self):
        _insert_plan("plan-fixed-floor", 1_000)
        _insert_coupon("FIXEDBIG", "fixed", 5_000)
        total = compute_total_cents("plan-fixed-floor", [], "FIXEDBIG")
        assert total == 0

    def test_expired_coupon_raises(self):
        _insert_plan("plan-expired", 10_000)
        expired_at = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        _insert_coupon("EXPIRED", "percent", 10, expires_at=expired_at)
        with pytest.raises(ValueError):
            compute_total_cents("plan-expired", [], "EXPIRED")

    def test_exhausted_coupon_raises(self):
        _insert_plan("plan-exhausted", 10_000)
        _insert_coupon("EXHAUSTED", "percent", 10, max_redemptions=1, redemption_count=1)
        with pytest.raises(ValueError):
            compute_total_cents("plan-exhausted", [], "EXHAUSTED")

    def test_unknown_plan_raises(self):
        with pytest.raises(ValueError):
            compute_total_cents("no-such-plan", [], None)

    def test_unknown_addon_raises(self):
        _insert_plan("plan-unknown-addon", 10_000)
        with pytest.raises(ValueError):
            compute_total_cents("plan-unknown-addon", ["no-such-addon"], None)

    def test_unknown_coupon_raises(self):
        _insert_plan("plan-unknown-coupon", 10_000)
        with pytest.raises(ValueError):
            compute_total_cents("plan-unknown-coupon", [], "NO-SUCH-COUPON")


class TestIsOrgCurrent:
    def test_no_subscription_is_not_current(self, tenants):
        tenant_a, _tenant_b = tenants
        assert storage.is_org_current(tenant_a.org_id) is False

    def test_superadmin_context_is_always_current(self):
        assert storage.is_org_current(None) is True

    def test_pending_payment_subscription_is_not_current(self, tenants):
        tenant_a, _tenant_b = tenants
        storage.create_subscription(tenant_a.org_id, plan_id=None, status="pending_payment")
        assert storage.is_org_current(tenant_a.org_id) is False

    def test_active_subscription_is_current(self, tenants):
        tenant_a, _tenant_b = tenants
        storage.create_subscription(tenant_a.org_id, plan_id=None, status="active")
        assert storage.is_org_current(tenant_a.org_id) is True

    def test_past_due_subscription_is_not_current(self, tenants):
        tenant_a, _tenant_b = tenants
        sub_id = storage.create_subscription(tenant_a.org_id, plan_id=None, status="active")
        storage.update_subscription(sub_id, status="past_due")
        assert storage.is_org_current(tenant_a.org_id) is False

    def test_cancelled_subscription_is_not_current(self, tenants):
        tenant_a, _tenant_b = tenants
        sub_id = storage.create_subscription(tenant_a.org_id, plan_id=None, status="active")
        storage.update_subscription(sub_id, status="cancelled")
        assert storage.is_org_current(tenant_a.org_id) is False

    def test_other_org_is_unaffected(self, tenants):
        tenant_a, tenant_b = tenants
        storage.create_subscription(tenant_a.org_id, plan_id=None, status="active")
        assert storage.is_org_current(tenant_b.org_id) is False


def _insert_plan_with_quota(key: str, message_quota: int, quota_period_days: int = 30) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO billing_plans (key, name, price_cents, message_quota, quota_period_days) "
            "VALUES (?, ?, ?, ?, ?)",
            (key, key, 0, message_quota, quota_period_days),
        )
        return cur.lastrowid


def _insert_messages_addon(key: str) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO billing_addons (key, name, price_cents, kind, capacity_key) "
            "VALUES (?, ?, ?, 'capacity', 'messages')",
            (key, key, 4_900),
        )
        return cur.lastrowid


def _insert_seat_addon(key: str) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO billing_addons (key, name, price_cents, kind, capacity_key) "
            "VALUES (?, ?, ?, 'capacity', 'seat')",
            (key, key, 4_900),
        )
        return cur.lastrowid


class TestMessageQuotaAndAddonBalance:
    """billing/entitlements.py::check_message_quota and get_org_message_usage
    - the plan's rolling-window allocation, and the separately-tracked,
    non-expiring purchased 'messages' add-on balance that's only drawn down
    once the plan allocation is exhausted (see check_message_quota's own
    docstring)."""

    def test_within_plan_quota_does_not_touch_addon_balance(self, tenants):
        from autosend.billing import entitlements

        tenant_a, _tenant_b = tenants
        plan_id = _insert_plan_with_quota("plan-quota-2", message_quota=2)
        storage.create_subscription(tenant_a.org_id, plan_id=plan_id, status="active")

        entitlements.check_message_quota(tenant_a.org_id)
        usage = entitlements.get_org_message_usage(tenant_a.org_id)
        assert usage["plan_quota"] == 2
        assert usage["addon_consumed"] == 0

    def test_exhausted_plan_quota_with_no_addon_raises(self, tenants):
        from autosend.billing import entitlements

        tenant_a, _tenant_b = tenants
        plan_id = _insert_plan_with_quota("plan-quota-0", message_quota=0)
        storage.create_subscription(tenant_a.org_id, plan_id=plan_id, status="active")

        with pytest.raises(entitlements.LimitExceeded):
            entitlements.check_message_quota(tenant_a.org_id)

    def test_exhausted_plan_quota_draws_from_addon_balance(self, tenants):
        from autosend.billing import entitlements

        tenant_a, _tenant_b = tenants
        plan_id = _insert_plan_with_quota("plan-quota-0-addon", message_quota=0)
        sub_id = storage.create_subscription(tenant_a.org_id, plan_id=plan_id, status="active")
        storage.credit_addon_messages_purchased(sub_id, 1000)

        usage_before = entitlements.get_org_message_usage(tenant_a.org_id)
        assert usage_before["addon_purchased"] == 1000
        assert usage_before["addon_remaining"] == 1000

        entitlements.check_message_quota(tenant_a.org_id)  # should not raise - draws from add-on balance

        usage_after = entitlements.get_org_message_usage(tenant_a.org_id)
        assert usage_after["addon_consumed"] == 1
        assert usage_after["addon_remaining"] == 999

    def test_addon_balance_never_expires_across_calls_until_exhausted(self, tenants):
        from autosend.billing import entitlements

        tenant_a, _tenant_b = tenants
        plan_id = _insert_plan_with_quota("plan-quota-0-small-addon", message_quota=0)
        sub_id = storage.create_subscription(tenant_a.org_id, plan_id=plan_id, status="active")
        storage.credit_addon_messages_purchased(sub_id, 1000)

        # Manually pre-consume 999 of the 1000-message block so only one
        # remains, then confirm the next call succeeds and the one after
        # that raises - the balance is a running total, not reset by time.
        storage.increment_addon_messages_consumed(sub_id, count=999)

        entitlements.check_message_quota(tenant_a.org_id)  # last remaining add-on message
        usage = entitlements.get_org_message_usage(tenant_a.org_id)
        assert usage["addon_remaining"] == 0

        with pytest.raises(entitlements.LimitExceeded):
            entitlements.check_message_quota(tenant_a.org_id)

    def test_purchased_balance_survives_unrelated_addon_changes(self, tenants):
        """The purchased balance is a persisted running total
        (subscriptions.addon_messages_purchased), not derived from active
        subscription_items - so adding/removing some other, unrelated
        capacity add-on (e.g. an extra seat) must never affect it."""
        from autosend.billing import entitlements

        tenant_a, _tenant_b = tenants
        plan_id = _insert_plan_with_quota("plan-quota-0-unrelated-addon", message_quota=0)
        sub_id = storage.create_subscription(tenant_a.org_id, plan_id=plan_id, status="active")
        storage.credit_addon_messages_purchased(sub_id, 1000)

        seat_addon_id = _insert_seat_addon("seat-addon-unrelated")
        storage.add_subscription_item(sub_id, seat_addon_id)
        storage.remove_subscription_item(sub_id, seat_addon_id)

        usage = entitlements.get_org_message_usage(tenant_a.org_id)
        assert usage["addon_purchased"] == 1000
        assert usage["addon_remaining"] == 1000

    def test_other_org_addon_balance_is_unaffected(self, tenants):
        from autosend.billing import entitlements

        tenant_a, tenant_b = tenants
        plan_id = _insert_plan_with_quota("plan-quota-0-cross-org", message_quota=0)
        sub_a = storage.create_subscription(tenant_a.org_id, plan_id=plan_id, status="active")
        sub_b = storage.create_subscription(tenant_b.org_id, plan_id=plan_id, status="active")
        storage.credit_addon_messages_purchased(sub_a, 1000)
        storage.credit_addon_messages_purchased(sub_b, 1000)

        entitlements.check_message_quota(tenant_a.org_id)

        usage_a = entitlements.get_org_message_usage(tenant_a.org_id)
        usage_b = entitlements.get_org_message_usage(tenant_b.org_id)
        assert usage_a["addon_consumed"] == 1
        assert usage_b["addon_consumed"] == 0


class TestMessagesAddonExcludedFromRecurringFlow:
    """The 'messages' capacity add-on is a one-time top-up
    (billing/engine.py::purchase_message_addon), not a recurring
    subscription_items add-on like seat/number/unit - add_addon and
    start_subscription must both refuse to add it via the generic
    recurring-add-on path, so it can never accidentally become a monthly
    charge."""

    def test_add_addon_rejects_messages_addon(self, tenants):
        from autosend.billing import engine

        tenant_a, _tenant_b = tenants
        plan_id = _insert_plan_with_quota("plan-reject-add-addon", message_quota=1000)
        storage.create_subscription(tenant_a.org_id, plan_id=plan_id, status="active")
        _insert_messages_addon("messages-addon-reject-add")

        with pytest.raises(ValueError):
            engine.add_addon(tenant_a.org_id, "messages-addon-reject-add")

    def test_purchase_message_addon_requires_saved_payment_method(self, tenants):
        import asyncio

        from autosend.billing import engine

        tenant_a, _tenant_b = tenants
        plan_id = _insert_plan_with_quota("plan-reject-purchase", message_quota=1000)
        storage.create_subscription(tenant_a.org_id, plan_id=plan_id, status="active")
        _insert_messages_addon("messages-addon-reject-purchase")

        with pytest.raises(ValueError):
            asyncio.run(engine.purchase_message_addon(tenant_a.org_id))

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

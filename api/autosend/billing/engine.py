"""
billing/engine.py

Billing business logic: computing totals (plan + add-ons + coupon),
driving a subscription through Paystack's checkout flow, plan
changes (immediate upgrade / deferred downgrade), recurring billing,
and superadmin manual comp overrides.

Talks to storage.billing (imported into `storage` by hand, per
storage/__init__.py's own convention - so this calls storage.get_plan_by_key
etc, not storage.billing.get_plan_by_key) for persistence, and to a
module-level PaystackProvider instance for anything that needs to call
out to Paystack - never imports autosend.billing.paystack internals
directly elsewhere, so a future provider swap only has to change
_provider here.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from autosend import storage
from autosend.billing.paystack import PaystackProvider
from autosend.utils.logging import get_logger

logger = get_logger(__name__)

_provider = PaystackProvider()

RECURRING_PERIOD_DAYS = 30


def _apply_addon_module_effect(org_id: int, addon: dict, active: bool) -> None:
    """An add-on can be tied to a storage.modules module key
    (billing_addons.module_key - e.g. the "Planning Center Integration"
    add-on's module_key='pco') - buying it needs to actually
    grant+enable that module, not just add a line item to the
    subscription, or the org pays for it and nothing happens. Not every
    add-on maps to a module (module_key is nullable - e.g. "Extra
    Unit/Campus" doesn't), so this is a no-op for those.

    Removing the add-on disables (not revokes) the module - a superadmin
    grant is a separate entitlement tier from this org-admin-facing
    enable/disable, and re-adding the same add-on later shouldn't need a
    superadmin to re-grant it."""
    module_key = addon.get("module_key")
    if not module_key:
        return
    if active:
        if not storage.is_granted(org_id, module_key):
            storage.grant(org_id, module_key)
        storage.enable(org_id, module_key)
    else:
        storage.disable(org_id, module_key)


def _plan_key_for_id(plan_id: int | None) -> str | None:
    if plan_id is None:
        return None
    for plan in storage.list_plans(active_only=False):
        if plan["id"] == plan_id:
            return plan["key"]
    return None


def _plan_price_cents(plan_id: int | None) -> int:
    if plan_id is None:
        return 0
    for plan in storage.list_plans(active_only=False):
        if plan["id"] == plan_id:
            return plan["price_cents"]
    return 0


def compute_total_cents(plan_key: str, addon_keys: list[str], coupon_code: str | None) -> int:
    """Plan + active add-ons' price_cents, then coupon discount applied
    (percent-off or fixed-off), floored at 0. Raises ValueError on a
    missing/inactive plan or add-on, or an invalid/expired/exhausted
    coupon - callers should surface that message to whoever is checking
    out rather than silently ignoring a bad coupon code."""
    plan = storage.get_plan_by_key(plan_key)
    if plan is None or not plan["active"]:
        raise ValueError(f"Unknown or inactive plan: {plan_key!r}")

    total = plan["price_cents"]
    for addon_key in addon_keys:
        addon = storage.get_addon_by_key(addon_key)
        if addon is None or not addon["active"]:
            raise ValueError(f"Unknown or inactive add-on: {addon_key!r}")
        total += addon["price_cents"]

    if coupon_code:
        coupon = storage.get_coupon_by_code(coupon_code)
        if coupon is None or not coupon["active"]:
            raise ValueError(f"Invalid coupon code: {coupon_code!r}")
        if coupon["expires_at"] and coupon["expires_at"] < datetime.now(timezone.utc).isoformat():
            raise ValueError(f"Coupon {coupon_code!r} has expired")
        if coupon["max_redemptions"] is not None and coupon["redemption_count"] >= coupon["max_redemptions"]:
            raise ValueError(f"Coupon {coupon_code!r} has reached its redemption limit")

        if coupon["kind"] == "percent":
            total -= total * coupon["amount"] // 100
        else:  # 'fixed'
            total -= coupon["amount"]
        total = max(total, 0)

    return total


async def start_subscription(
    org_id: int,
    plan_key: str,
    addon_keys: list[str],
    coupon_code: str | None,
    email: str,
    callback_url: str,
) -> str:
    """Creates the subscription + subscription_items rows (status
    'pending_payment'), computes the total (raises ValueError on a bad
    coupon/plan/add-on before anything is created), then hands off to
    Paystack to get a checkout URL. Returns that URL for the caller to
    redirect the browser to.

    The initial billing_transactions row is logged with
    provider_reference=None (Paystack doesn't hand us a reference until
    the payer actually completes checkout) - confirm_payment() below
    finds this row back by picking the newest pending 'initial'
    transaction with no reference yet for this subscription, then fills
    the reference in."""
    existing = storage.get_subscription(org_id)
    if existing is not None and existing.status == "active":
        raise ValueError("This organisation already has an active subscription.")

    plan = storage.get_plan_by_key(plan_key)
    if plan is None:
        raise ValueError(f"Unknown plan: {plan_key!r}")

    total_cents = compute_total_cents(plan_key, addon_keys, coupon_code)

    coupon = storage.get_coupon_by_code(coupon_code) if coupon_code else None

    # create_subscription upserts on org_id, so a retried checkout (prior
    # attempt failed after this point - e.g. Paystack rejected the
    # initialize call below, or the payer just abandoned the tab) reuses
    # the same row instead of hitting subscriptions.org_id's UNIQUE
    # constraint. clear_subscription_items drops whatever a previous,
    # incomplete attempt already recorded so a changed add-on selection
    # on retry doesn't leave stale items active alongside the new ones.
    subscription_id = storage.create_subscription(
        org_id, plan["id"], status="pending_payment", billing_email=email
    )
    storage.clear_subscription_items(subscription_id)
    if coupon:
        storage.update_subscription(subscription_id, coupon_id=coupon["id"])

    for addon_key in addon_keys:
        addon = storage.get_addon_by_key(addon_key)
        storage.add_subscription_item(subscription_id, addon["id"])

    customer_code = await _provider.create_customer(email, org_id)
    storage.update_subscription(subscription_id, paystack_customer_code=customer_code)

    checkout_url = await _provider.initialize_transaction(email, total_cents, callback_url)

    storage.log_transaction(
        org_id=org_id,
        subscription_id=subscription_id,
        provider="paystack",
        provider_reference=None,
        amount_cents=total_cents,
        status="pending",
        kind="initial",
    )

    if coupon:
        storage.increment_coupon_redemption(coupon["id"])

    return checkout_url


def add_addon(org_id: int, addon_key: str) -> None:
    """Org-admin self-service add-on add (web/billing_router.py's
    POST /billing/addons/{addon_key}/add calls this rather than touching
    storage directly), so the module-grant side effect
    (_apply_addon_module_effect) always happens alongside the billing
    record - it must be applied here even for an already-active
    subscription, unlike start_subscription's checkout-time add which
    defers the effect until confirm_payment."""
    subscription = storage.get_subscription(org_id)
    if subscription is None:
        raise ValueError(f"No subscription found for org {org_id}")

    addon = storage.get_addon_by_key(addon_key)
    if addon is None or not addon["active"]:
        raise ValueError(f"Unknown or inactive add-on: {addon_key!r}")

    if addon_key in storage.list_active_addons_for_subscription(subscription.id):
        raise ValueError("Add-on already active")

    storage.add_subscription_item(subscription.id, addon["id"])
    _apply_addon_module_effect(org_id, addon, active=True)


def remove_addon(org_id: int, addon_key: str) -> None:
    subscription = storage.get_subscription(org_id)
    if subscription is None:
        raise ValueError(f"No subscription found for org {org_id}")

    addon = storage.get_addon_by_key(addon_key)
    if addon is None:
        raise ValueError(f"Unknown add-on: {addon_key!r}")

    storage.remove_subscription_item(subscription.id, addon["id"])
    _apply_addon_module_effect(org_id, addon, active=False)


async def confirm_payment(reference: str) -> None:
    """Called from both the browser-redirect callback route and the
    Paystack webhook handler - safe to call twice for the same reference
    (re-checks status before flipping), since both paths can race to
    confirm the same payment."""
    existing = storage.get_transaction_by_reference(reference)
    if existing is not None and existing["status"] == "success":
        logger.info("confirm_payment: reference=%s already confirmed, skipping", reference)
        return

    result = await _provider.verify_transaction(reference)
    if not result.success:
        logger.warning("confirm_payment: verification failed for reference=%s", reference)
        return

    if existing is not None:
        transaction_id, subscription_id = existing["id"], existing["subscription_id"]
    else:
        # First time we've seen this reference - claim the newest pending
        # 'initial' transaction that hasn't been assigned a reference yet
        # (written by start_subscription) and stamp it with this one.
        claimed = storage.claim_pending_initial_transaction(reference)
        if claimed is None:
            logger.warning("confirm_payment: no matching pending transaction found for reference=%s", reference)
            return
        transaction_id, subscription_id = claimed

    if subscription_id is None:
        logger.warning("confirm_payment: no subscription associated with reference=%s", reference)
        return

    subscription = storage.get_subscription_by_id(subscription_id)
    if subscription is None:
        logger.warning("confirm_payment: subscription %s not found for reference=%s", subscription_id, reference)
        return

    if subscription.status == "active":
        # Already confirmed by the other path (webhook vs. browser
        # callback racing each other) - idempotent no-op.
        return

    period_end = (datetime.now(timezone.utc) + timedelta(days=RECURRING_PERIOD_DAYS)).isoformat()
    storage.update_subscription(
        subscription_id,
        status="active",
        paystack_authorization_code=result.authorization_code,
        current_period_end=period_end,
    )
    # A paying org shouldn't also need a manual superadmin activation
    # step before it can send - is_org_active and is_org_current are two
    # independent gates (see storage.organisations.is_org_active's own
    # docstring), but a successful first payment is exactly the signal
    # that should flip this one. Idempotent (activate_organisation is a
    # plain UPDATE), and a superadmin can still deactivate an org
    # afterwards as an override (e.g. policy violation) regardless of
    # billing status.
    storage.activate_organisation(subscription.org_id)
    # Module-linked add-ons only take effect once payment is actually
    # confirmed, not at start_subscription() checkout time - otherwise an
    # abandoned/failed checkout would still have granted the module.
    active_addon_keys = storage.list_active_addons_for_subscription(subscription_id)
    for addon_key in active_addon_keys:
        addon = storage.get_addon_by_key(addon_key)
        if addon is not None:
            _apply_addon_module_effect(subscription.org_id, addon, active=True)
    # Finalizes the claimed row in place rather than inserting a new one -
    # one successful payment produces exactly one billing_transactions
    # row, not two (see storage.billing.finalize_initial_transaction).
    storage.finalize_initial_transaction(
        transaction_id,
        status="success",
        amount_cents=result.amount_cents,
        raw_payload=json.dumps(result.raw),
    )


def change_plan(org_id: int, new_plan_key: str) -> None:
    """Upgrade (new plan costs the same or more): applied immediately.
    Downgrade (new plan costs less): deferred until the current billing
    period ends, via pending_downgrade_plan_id/
    pending_downgrade_effective_at - apply_pending_downgrades() (run daily
    by the scheduler) is what actually flips plan_id once that date
    arrives."""
    subscription = storage.get_subscription(org_id)
    if subscription is None:
        raise ValueError(f"No subscription found for org {org_id}")

    new_plan = storage.get_plan_by_key(new_plan_key)
    if new_plan is None or not new_plan["active"]:
        raise ValueError(f"Unknown or inactive plan: {new_plan_key!r}")

    current_price = _plan_price_cents(subscription.plan_id)

    if new_plan["price_cents"] >= current_price:
        storage.update_subscription(subscription.id, plan_id=new_plan["id"])
    else:
        storage.update_subscription(
            subscription.id,
            pending_downgrade_plan_id=new_plan["id"],
            pending_downgrade_effective_at=subscription.current_period_end,
        )
        # Registers the one-shot job right now rather than waiting for
        # the next app restart's reload_pending_downgrades() sweep - a
        # downgrade requested mid-session still needs to fire on its own
        # effective date, not whenever the app next happens to restart.
        from autosend import scheduler

        scheduler.schedule_pending_downgrade(storage.get_subscription_by_id(subscription.id))


def cancel_pending_downgrade(org_id: int) -> None:
    subscription = storage.get_subscription(org_id)
    if subscription is None:
        raise ValueError(f"No subscription found for org {org_id}")
    storage.update_subscription(
        subscription.id,
        pending_downgrade_plan_id=None,
        pending_downgrade_effective_at=None,
    )
    from autosend import scheduler

    scheduler.cancel_pending_downgrade_job(subscription.id)


def apply_pending_downgrades() -> int:
    """Run daily by the scheduler (see scheduler.py::reload_pending_downgrades),
    plus one one-shot DateTrigger job per subscription registered at its
    exact effective date, so a downgrade doesn't have to wait for the
    next daily sweep."""
    now = datetime.now(timezone.utc).isoformat()
    applied = 0
    for subscription in storage.list_subscriptions_with_pending_downgrade():
        if subscription.pending_downgrade_effective_at and subscription.pending_downgrade_effective_at <= now:
            storage.update_subscription(
                subscription.id,
                plan_id=subscription.pending_downgrade_plan_id,
                pending_downgrade_plan_id=None,
                pending_downgrade_effective_at=None,
            )
            applied += 1
    return applied


def cancel_subscription(org_id: int) -> None:
    """Marks the subscription for cancellation at the end of the current
    paid period (cancel_at = current_period_end) rather than cancelling
    immediately - same "stays active until period end" reasoning as
    pending downgrades, and the org already paid for this period.
    apply_pending_cancellations (run by the scheduler) is what actually
    flips status to 'cancelled' once that date arrives."""
    subscription = storage.get_subscription(org_id)
    if subscription is None:
        raise ValueError(f"No subscription found for org {org_id}")
    if subscription.status != "active":
        raise ValueError(f"Cannot cancel a subscription with status {subscription.status!r}")
    if subscription.current_period_end is None:
        raise ValueError("Subscription has no current billing period to cancel at the end of")
    storage.update_subscription(subscription.id, cancel_at=subscription.current_period_end)
    # Registers the one-shot job right now, same reasoning as
    # change_plan's downgrade branch - don't wait for the next app
    # restart's reload_pending_cancellations() sweep.
    from autosend import scheduler

    scheduler.schedule_pending_cancellation(storage.get_subscription_by_id(subscription.id))


def cancel_pending_cancellation(org_id: int) -> None:
    """The org-admin-facing "keep my subscription" undo - clears cancel_at
    so run_recurring_billing picks the subscription back up for its next
    renewal instead of apply_pending_cancellations cancelling it."""
    subscription = storage.get_subscription(org_id)
    if subscription is None:
        raise ValueError(f"No subscription found for org {org_id}")
    storage.update_subscription(subscription.id, cancel_at=None)
    from autosend import scheduler

    scheduler.cancel_pending_cancellation_job(subscription.id)


def apply_pending_cancellations() -> int:
    """Run daily by the scheduler (see scheduler.py::reload_pending_cancellations),
    plus one one-shot DateTrigger job per subscription registered at its
    exact cancel_at date. Flips status to 'cancelled', deactivates the
    org (storage.is_org_active is a hard send-blocking gate, unlike
    subscription status alone), and disables any module-linked add-ons
    the org had active - a cancelled org shouldn't keep e.g. the PCO
    module enabled just because nothing else touched that flag."""
    now = datetime.now(timezone.utc).isoformat()
    applied = 0
    for subscription in storage.list_subscriptions_with_pending_cancellation():
        if subscription.cancel_at and subscription.cancel_at <= now:
            storage.update_subscription(subscription.id, status="cancelled", cancel_at=None)
            storage.deactivate_organisation(subscription.org_id)
            for addon_key in storage.list_active_addons_for_subscription(subscription.id):
                addon = storage.get_addon_by_key(addon_key)
                if addon is not None:
                    _apply_addon_module_effect(subscription.org_id, addon, active=False)
            applied += 1
    return applied


async def run_recurring_billing() -> None:
    """One attempt per due subscription, once a day via the scheduler's
    own daily cadence - no custom retry/dunning logic here by design (see
    this task's own non-goal); a failed charge just sets status
    'past_due' and waits for tomorrow's run to try again.

    A failed charge also deactivates the org (storage.deactivate_organisation)
    - is_org_active is the hard send-blocking gate, so a lapsed payment
    has to flip it, not just the subscription's own status column.
    Symmetrically, a later successful charge (subscription recovers from
    'past_due' back to 'active') reactivates the org automatically -
    no manual superadmin step needed, same as the very first payment.

    Coupons are NOT reapplied on renewal - deliberately: a coupon in this
    schema is a one-time signup incentive (max_redemptions/expiry track
    "times used", not "times still valid for this subscription"), so
    recomputing a discount here every period would both double-dip
    against that bookkeeping and imply an ongoing discount that was never
    actually promised at signup."""
    for subscription in storage.list_active_subscriptions_due_for_billing():
        addon_keys = storage.list_active_addons_for_subscription(subscription.id)
        current_plan_key = _plan_key_for_id(subscription.plan_id)

        if current_plan_key is None:
            logger.warning("run_recurring_billing: subscription %s has no plan, skipping", subscription.id)
            continue

        total_cents = compute_total_cents(current_plan_key, addon_keys, coupon_code=None)

        if not subscription.paystack_authorization_code or not subscription.billing_email:
            logger.warning(
                "run_recurring_billing: subscription %s missing authorization or billing_email, marking past_due",
                subscription.id,
            )
            storage.update_subscription(subscription.id, status="past_due")
            storage.deactivate_organisation(subscription.org_id)
            storage.log_transaction(
                org_id=subscription.org_id, subscription_id=subscription.id, provider="paystack",
                provider_reference=None, amount_cents=total_cents, status="failed", kind="recurring",
            )
            continue

        try:
            result = await _provider.charge_authorization(
                subscription.paystack_authorization_code, total_cents, subscription.billing_email
            )
        except Exception:
            logger.exception("run_recurring_billing: charge failed for subscription %s", subscription.id)
            storage.update_subscription(subscription.id, status="past_due")
            storage.deactivate_organisation(subscription.org_id)
            storage.log_transaction(
                org_id=subscription.org_id, subscription_id=subscription.id, provider="paystack",
                provider_reference=None, amount_cents=total_cents, status="failed", kind="recurring",
            )
            continue

        if result.success:
            new_period_end = (datetime.now(timezone.utc) + timedelta(days=RECURRING_PERIOD_DAYS)).isoformat()
            storage.update_subscription(subscription.id, status="active", current_period_end=new_period_end)
            storage.activate_organisation(subscription.org_id)
            storage.log_transaction(
                org_id=subscription.org_id, subscription_id=subscription.id, provider="paystack",
                provider_reference=result.reference, amount_cents=result.amount_cents,
                status="success", kind="recurring", raw_payload=json.dumps(result.raw),
            )
        else:
            storage.update_subscription(subscription.id, status="past_due")
            storage.deactivate_organisation(subscription.org_id)
            storage.log_transaction(
                org_id=subscription.org_id, subscription_id=subscription.id, provider="paystack",
                provider_reference=result.reference, amount_cents=result.amount_cents,
                status="failed", kind="recurring", raw_payload=json.dumps(result.raw),
            )


def comp_org(org_id: int, note: str = "") -> None:
    """Superadmin manual override - ensures an active subscription exists
    for this org (no plan required, NULL is allowed) and logs a
    kind='manual_override' transaction for audit purposes. Idempotent:
    calling this on an org that already has an active subscription just
    logs another zero-amount override transaction rather than erroring."""
    subscription = storage.get_subscription(org_id)
    if subscription is None:
        subscription_id = storage.create_subscription(org_id, plan_id=None, status="active")
    else:
        subscription_id = subscription.id
        if subscription.status != "active":
            storage.update_subscription(subscription_id, status="active")

    storage.log_transaction(
        org_id=org_id,
        subscription_id=subscription_id,
        provider="manual",
        provider_reference=None,
        amount_cents=0,
        status="success",
        kind="manual_override",
        raw_payload=json.dumps({"note": note}) if note else None,
    )

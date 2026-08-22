"""
billing/entitlements.py

Computes an organisation's current resource limits (seats, WhatsApp
numbers, units, message quota) from its subscription's plan plus any
active 'capacity' add-ons, and enforces them at the points where new
resources get created or messages get sent.

This is a separate concern from billing/engine.py's own module-linked
add-on handling (_apply_addon_module_effect) - that bucket ("integration"
add-ons: PCO, iCal, email-to-WhatsApp, SME metrics) grants/enables a
storage.modules feature flag and is untouched by anything here. This
module only cares about billing_addons rows with kind='capacity', which
have no module_key and no feature-flag side effect at all - they just
change the numbers get_org_limits() returns.

Standard subscription (no plan, or a plan with no overrides) = 1 user, 1
WhatsApp number, 1 unit, 1000 messages per rolling 30 days - see
billing/schema.py's billing_plans column defaults, which this module
mirrors as its own fallback so an org with no subscription row yet still
gets the standard entitlement rather than zero.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from autosend import storage

DEFAULT_BASE_USERS = 1
DEFAULT_BASE_NUMBERS = 1
DEFAULT_BASE_UNITS = 1
DEFAULT_MESSAGE_QUOTA = 1000
DEFAULT_QUOTA_PERIOD_DAYS = 30


class LimitExceeded(ValueError):
    """Raised by the check_* functions below when an org is at or over
    one of its plan/add-on-derived limits. A plain ValueError subclass
    (not a custom exception hierarchy) so existing call sites that already
    catch ValueError from other billing/admin validation keep working
    unchanged; callers that want to react specifically to a limit (rather
    than any other validation failure) can still catch this by name."""


def get_org_limits(org_id: int) -> dict:
    """Returns {'users', 'numbers', 'units', 'message_quota',
    'quota_period_days'} - the plan's base_* values (or the DEFAULT_*
    constants above if the org has no subscription, or its subscription
    has no plan attached - e.g. a superadmin comp via billing.engine.comp_org)
    plus one increment per active kind='capacity' add-on on the
    subscription: 'seat' adds to users, 'number' adds to numbers, 'unit'
    adds to *both* units and numbers (an extra unit "includes" a number,
    per this feature's product requirement)."""
    subscription = storage.get_subscription(org_id)

    plan = None
    if subscription is not None and subscription.plan_id is not None:
        plan = storage.get_plan_by_id(subscription.plan_id)

    limits = {
        "users": plan["base_users"] if plan else DEFAULT_BASE_USERS,
        "numbers": plan["base_numbers"] if plan else DEFAULT_BASE_NUMBERS,
        "units": plan["base_units"] if plan else DEFAULT_BASE_UNITS,
        "message_quota": plan["message_quota"] if plan else DEFAULT_MESSAGE_QUOTA,
        "quota_period_days": plan["quota_period_days"] if plan else DEFAULT_QUOTA_PERIOD_DAYS,
    }

    if subscription is None:
        return limits

    for addon_key in storage.list_active_addons_for_subscription(subscription.id):
        addon = storage.get_addon_by_key(addon_key)
        if addon is None or addon.get("kind") != "capacity":
            continue
        capacity_key = addon.get("capacity_key")
        if capacity_key == "seat":
            limits["users"] += 1
        elif capacity_key == "number":
            limits["numbers"] += 1
        elif capacity_key == "unit":
            limits["units"] += 1
            limits["numbers"] += 1

    return limits


def check_can_add_user(org_id: int | None) -> None:
    """No-op for org_id=None (superadmin context - mirrors
    storage.is_org_current's own org_id=None -> True bypass)."""
    if org_id is None:
        return
    limits = get_org_limits(org_id)
    current = storage.count_active_org_users(org_id)
    if current >= limits["users"]:
        raise LimitExceeded(
            f"This organisation's plan allows {limits['users']} user(s); "
            f"it already has {current}. Buy an extra seat add-on to add more."
        )


def check_can_add_unit(org_id: int | None) -> None:
    if org_id is None:
        return
    limits = get_org_limits(org_id)
    current = storage.count_units_for_org(org_id)
    if current >= limits["units"]:
        raise LimitExceeded(
            f"This organisation's plan allows {limits['units']} unit(s); "
            f"it already has {current}. Buy an extra unit add-on to add more."
        )


def check_can_add_number(org_id: int | None) -> None:
    if org_id is None:
        return
    limits = get_org_limits(org_id)
    current = storage.count_whatsapp_numbers_for_org(org_id)
    if current >= limits["numbers"]:
        raise LimitExceeded(
            f"This organisation's plan allows {limits['numbers']} WhatsApp number(s); "
            f"it already has {current}. Buy an extra number (or unit) add-on to add more."
        )


def check_message_quota(org_id: int | None) -> None:
    """Rolling window, not a calendar period - "quota_period_days days
    ago until now", recomputed fresh on every call so the window slides
    forward continuously rather than resetting on a fixed billing-cycle
    boundary."""
    if org_id is None:
        return
    limits = get_org_limits(org_id)
    since = (
        datetime.now(timezone.utc) - timedelta(days=limits["quota_period_days"])
    ).isoformat()
    sent = storage.count_sent_messages_for_org_since(org_id, since)
    if sent >= limits["message_quota"]:
        raise LimitExceeded(
            f"This organisation has sent {sent} messages in the last "
            f"{limits['quota_period_days']} days, at its plan's quota of "
            f"{limits['message_quota']}. Upgrade the plan to send more."
        )

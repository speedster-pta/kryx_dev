"""
storage/billing.py

Raw sqlite3 CRUD for platform-level org subscription billing (see
billing/schema.py for the tables). Mirrors storage/organisations.py's
style: frozen dataclasses + _row_to_x mappers for the "main" row types
(Subscription), plain dicts for everything else - matching how the rest
of this package mixes both, e.g. storage/send_log.py returns dicts while
storage/organisations.py returns a dataclass.

is_org_current() is the choke-point function billing enforcement is
wired through - see its own docstring below.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from autosend.storage._db import _connect as get_conn


@dataclass(frozen=True)
class Subscription:
    id: int
    org_id: int
    plan_id: int | None
    status: str
    paystack_customer_code: str | None
    paystack_authorization_code: str | None
    pending_downgrade_plan_id: int | None
    pending_downgrade_effective_at: str | None
    current_period_end: str | None
    coupon_id: int | None
    cancel_at: str | None
    billing_email: str | None
    created_at: str
    updated_at: str


_SUBSCRIPTION_COLUMNS = [
    "id", "org_id", "plan_id", "status", "paystack_customer_code",
    "paystack_authorization_code", "pending_downgrade_plan_id",
    "pending_downgrade_effective_at", "current_period_end", "coupon_id",
    "cancel_at", "billing_email", "created_at", "updated_at",
]


def _row_to_subscription(row: sqlite3.Row) -> Subscription:
    return Subscription(**dict(zip(_SUBSCRIPTION_COLUMNS, row)))


# ---------------------------------------------------------------------------
# Catalogue: plans / add-ons / coupons - superadmin-managed, read here.
# ---------------------------------------------------------------------------

_PLAN_COLUMNS = [
    "id", "key", "name", "price_cents", "interval", "active",
    "base_users", "base_numbers", "base_units", "message_quota", "quota_period_days",
]


def get_plan_by_key(key: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT {', '.join(_PLAN_COLUMNS)} FROM billing_plans WHERE key = ?",
            (key,),
        ).fetchone()
    if row is None:
        return None
    return dict(zip(_PLAN_COLUMNS, row))


def get_plan_by_id(plan_id: int) -> dict | None:
    """Mirrors get_plan_by_key - billing/entitlements.py looks a plan up
    by the subscription's plan_id (an int FK), not its key."""
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT {', '.join(_PLAN_COLUMNS)} FROM billing_plans WHERE id = ?",
            (plan_id,),
        ).fetchone()
    if row is None:
        return None
    return dict(zip(_PLAN_COLUMNS, row))


def list_plans(active_only: bool = True) -> list[dict]:
    query = f"SELECT {', '.join(_PLAN_COLUMNS)} FROM billing_plans"
    if active_only:
        query += " WHERE active = 1"
    query += " ORDER BY price_cents"
    with get_conn() as conn:
        rows = conn.execute(query).fetchall()
    return [dict(zip(_PLAN_COLUMNS, r)) for r in rows]


_ADDON_COLUMNS = ["id", "key", "name", "price_cents", "active", "module_key", "kind", "capacity_key"]


def get_addon_by_key(key: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT {', '.join(_ADDON_COLUMNS)} FROM billing_addons WHERE key = ?",
            (key,),
        ).fetchone()
    if row is None:
        return None
    return dict(zip(_ADDON_COLUMNS, row))


def list_addons(active_only: bool = True) -> list[dict]:
    query = f"SELECT {', '.join(_ADDON_COLUMNS)} FROM billing_addons"
    if active_only:
        query += " WHERE active = 1"
    query += " ORDER BY price_cents"
    with get_conn() as conn:
        rows = conn.execute(query).fetchall()
    return [dict(zip(_ADDON_COLUMNS, r)) for r in rows]


def get_coupon_by_code(code: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT id, code, kind, amount, expires_at, max_redemptions,
                   redemption_count, active
            FROM coupons WHERE code = ?
            """,
            (code,),
        ).fetchone()
    if row is None:
        return None
    cols = ["id", "code", "kind", "amount", "expires_at", "max_redemptions", "redemption_count", "active"]
    return dict(zip(cols, row))


def increment_coupon_redemption(coupon_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE coupons SET redemption_count = redemption_count + 1 WHERE id = ?",
            (coupon_id,),
        )


def list_coupons() -> list[dict]:
    """Used by admin_org_pages.BillingCatalogueView - unlike
    get_coupon_by_code (a single lookup by code, used at checkout), this
    lists every coupon for the superadmin catalogue page regardless of
    active/expired status, so an expired or exhausted coupon doesn't just
    silently disappear from view."""
    cols = ["id", "code", "kind", "amount", "expires_at", "max_redemptions", "redemption_count", "active"]
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT {', '.join(cols)} FROM coupons ORDER BY created_at DESC"
        ).fetchall()
    return [dict(zip(cols, r)) for r in rows]


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------

def create_subscription(
    org_id: int, plan_id: int | None, status: str = "pending_payment", billing_email: str | None = None
) -> int:
    """Upsert on org_id (subscriptions.org_id is UNIQUE) rather than a
    plain INSERT - a checkout attempt that fails partway through the
    provider call (Paystack rejects, browser tab closed mid-checkout,
    etc.) has to be retryable from the same /billing/subscribe form, and
    a plain INSERT would raise a UNIQUE-constraint IntegrityError on that
    retry instead of just starting a fresh attempt. Resets the
    provider-specific and pending-downgrade fields since this always
    represents a brand new checkout, never a resume of prior billing
    state - engine.start_subscription is responsible for refusing to
    call this at all against an already-'active' subscription.

    billing_email is stored (not just passed to Paystack at checkout
    time and forgotten) because billing.engine.run_recurring_billing
    needs the exact same email later to call Paystack's
    charge_authorization - Paystack rejects that call if the email
    doesn't match the one tied to the authorization code, so a
    forgotten/placeholder email there is a real charge failure, not a
    cosmetic detail."""
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO subscriptions (org_id, plan_id, status, billing_email)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(org_id) DO UPDATE SET
                plan_id = excluded.plan_id,
                status = excluded.status,
                billing_email = excluded.billing_email,
                paystack_customer_code = NULL,
                paystack_authorization_code = NULL,
                pending_downgrade_plan_id = NULL,
                pending_downgrade_effective_at = NULL,
                coupon_id = NULL,
                updated_at = datetime('now')
            """,
            (org_id, plan_id, status, billing_email),
        )
        row = conn.execute("SELECT id FROM subscriptions WHERE org_id = ?", (org_id,)).fetchone()
        return row[0]


def clear_subscription_items(subscription_id: int) -> None:
    """Marks every currently-active add-on on this subscription as
    removed - called at the start of a fresh checkout attempt (see
    create_subscription's upsert) so a retried /billing/subscribe with a
    different add-on selection doesn't leave stale items from an earlier,
    failed attempt still active alongside the new ones."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE subscription_items SET removed_at = datetime('now') "
            "WHERE subscription_id = ? AND removed_at IS NULL",
            (subscription_id,),
        )


def get_subscription(org_id: int) -> Subscription | None:
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT {', '.join(_SUBSCRIPTION_COLUMNS)} FROM subscriptions WHERE org_id = ?",
            (org_id,),
        ).fetchone()
    return _row_to_subscription(row) if row else None


def get_subscription_by_id(subscription_id: int) -> Subscription | None:
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT {', '.join(_SUBSCRIPTION_COLUMNS)} FROM subscriptions WHERE id = ?",
            (subscription_id,),
        ).fetchone()
    return _row_to_subscription(row) if row else None


# Columns update_subscription() is allowed to set - explicit allowlist
# rather than a magic pass-through dict, per this task's own guidance to
# keep this simple and match real column names.
_UPDATABLE_SUBSCRIPTION_FIELDS = {
    "plan_id", "status", "paystack_customer_code", "paystack_authorization_code",
    "pending_downgrade_plan_id", "pending_downgrade_effective_at",
    "current_period_end", "coupon_id", "cancel_at",
}


def update_subscription(subscription_id: int, **fields) -> None:
    unknown = set(fields) - _UPDATABLE_SUBSCRIPTION_FIELDS
    if unknown:
        raise ValueError(f"Cannot update unknown subscription field(s): {sorted(unknown)}")
    if not fields:
        return
    set_clause = ", ".join(f"{name} = ?" for name in fields)
    params = list(fields.values()) + [subscription_id]
    with get_conn() as conn:
        conn.execute(
            f"UPDATE subscriptions SET {set_clause}, updated_at = datetime('now') WHERE id = ?",
            params,
        )


def add_subscription_item(subscription_id: int, addon_id: int) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO subscription_items (subscription_id, addon_id) VALUES (?, ?)",
            (subscription_id, addon_id),
        )
        return cur.lastrowid


def remove_subscription_item(subscription_id: int, addon_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE subscription_items SET removed_at = datetime('now')
            WHERE subscription_id = ? AND addon_id = ? AND removed_at IS NULL
            """,
            (subscription_id, addon_id),
        )


def remove_one_subscription_item(subscription_id: int, addon_id: int) -> bool:
    """Removes a single active instance of this add-on (the most
    recently added one) rather than every active row - used for
    'capacity' add-ons (extra seat/number/unit), which can be bought in
    multiples via repeated rows (see subscription_items' own docstring),
    so removing one instance must decrement by one, not clear all of
    them the way remove_subscription_item does for a plain on/off
    'integration' add-on. Returns False if there was no active instance
    to remove."""
    with get_conn() as conn:
        cur = conn.execute(
            """
            UPDATE subscription_items SET removed_at = datetime('now')
            WHERE id = (
                SELECT id FROM subscription_items
                WHERE subscription_id = ? AND addon_id = ? AND removed_at IS NULL
                ORDER BY added_at DESC LIMIT 1
            )
            """,
            (subscription_id, addon_id),
        )
        return cur.rowcount > 0


def list_active_addons_for_subscription(subscription_id: int) -> list[str]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT ba.key FROM subscription_items si
            JOIN billing_addons ba ON ba.id = si.addon_id
            WHERE si.subscription_id = ? AND si.removed_at IS NULL
            """,
            (subscription_id,),
        ).fetchall()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# Transactions - append-only, same "insert every attempt" philosophy as
# storage/send_log.py::record_send.
# ---------------------------------------------------------------------------

def log_transaction(
    org_id: int,
    subscription_id: int | None,
    provider: str,
    provider_reference: str | None,
    amount_cents: int,
    status: str,
    kind: str,
    raw_payload: str | None = None,
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO billing_transactions (
                org_id, subscription_id, provider, provider_reference,
                amount_cents, status, kind, raw_payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (org_id, subscription_id, provider, provider_reference, amount_cents, status, kind, raw_payload),
        )
        return cur.lastrowid


def claim_pending_initial_transaction(provider_reference: str) -> tuple[int, int] | None:
    """Called from billing.engine.confirm_payment the first time a given
    reference is seen: finds the newest still-pending 'initial'
    transaction with no reference yet (written by
    billing.engine.start_subscription, which doesn't get a reference back
    from Paystack until checkout completes) and stamps it with this
    reference - leaving status as 'pending' still, since this only marks
    the row as claimed (so a concurrent call can't claim the same row
    twice); the caller finalizes it to 'success'/'failed' via
    finalize_initial_transaction once verify_transaction has actually
    confirmed the payment. Returns (transaction_id, subscription_id), or
    None if no such row exists (e.g. a replayed/unknown reference)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, subscription_id FROM billing_transactions "
            "WHERE kind = 'initial' AND status = 'pending' AND provider_reference IS NULL "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        transaction_id, subscription_id = row
        conn.execute(
            "UPDATE billing_transactions SET provider_reference = ? WHERE id = ?",
            (provider_reference, transaction_id),
        )
        return transaction_id, subscription_id


def finalize_initial_transaction(
    transaction_id: int, status: str, amount_cents: int, raw_payload: str | None
) -> None:
    """Completes the row claim_pending_initial_transaction stamped -
    updates it in place rather than inserting a second row, so one
    successful payment produces exactly one 'success' billing_transactions
    row instead of two (the earlier shape did both: claim flipped the
    original row to 'success' AND confirm_payment then log_transaction'd
    a fresh one for the same event)."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE billing_transactions SET status = ?, amount_cents = ?, raw_payload = ? WHERE id = ?",
            (status, amount_cents, raw_payload, transaction_id),
        )


def get_transaction_by_reference(provider_reference: str) -> dict | None:
    cols = [
        "id", "org_id", "subscription_id", "provider", "provider_reference",
        "amount_cents", "status", "kind", "raw_payload", "created_at",
    ]
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT {', '.join(cols)} FROM billing_transactions WHERE provider_reference = ? "
            "ORDER BY id DESC LIMIT 1",
            (provider_reference,),
        ).fetchone()
    return dict(zip(cols, row)) if row else None


# ---------------------------------------------------------------------------
# Scheduler support queries
# ---------------------------------------------------------------------------

def list_subscriptions_with_pending_downgrade() -> list[Subscription]:
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT {', '.join(_SUBSCRIPTION_COLUMNS)} FROM subscriptions "
            "WHERE pending_downgrade_plan_id IS NOT NULL AND pending_downgrade_effective_at IS NOT NULL"
        ).fetchall()
    return [_row_to_subscription(r) for r in rows]


def list_active_subscriptions_due_for_billing() -> list[Subscription]:
    """Includes 'past_due', not just 'active' - a subscription that
    failed its last charge attempt still needs tomorrow's sweep to try
    again (billing.engine.run_recurring_billing's "one attempt per day,
    no custom retry logic" design only works if a past_due row keeps
    showing up here; excluding it would mean a failed charge is retried
    exactly once, ever, ignoring current_period_end's staleness rather
    than being retried daily until it either succeeds or is cancelled)."""
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT {', '.join(_SUBSCRIPTION_COLUMNS)} FROM subscriptions "
            "WHERE status IN ('active', 'past_due') AND current_period_end IS NOT NULL "
            "AND current_period_end <= datetime('now') AND cancel_at IS NULL"
        ).fetchall()
    return [_row_to_subscription(r) for r in rows]


def list_subscriptions_with_pending_cancellation() -> list[Subscription]:
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT {', '.join(_SUBSCRIPTION_COLUMNS)} FROM subscriptions "
            "WHERE cancel_at IS NOT NULL AND status = 'active'"
        ).fetchall()
    return [_row_to_subscription(r) for r in rows]


# ---------------------------------------------------------------------------
# Choke point - every send-triggering / feature-gating call site that
# needs "is this org's subscription currently in good standing" goes
# through here, mirroring storage.organisations.is_org_active's own
# docstring and bypass rule (org_id=None -> True, a superadmin context
# with no owning org). Deliberately a separate function from
# is_org_active - active/inactive is the older, superadmin-controlled
# provisioning switch; is_org_current is the newer, billing-driven signal
# - an org can be active (provisioned, allowed to exist) but not current
# (payment lapsed) at the same time, and callers that care about billing
# should check both rather than assuming one implies the other.
# ---------------------------------------------------------------------------

def is_org_current(org_id: int | None, conn: sqlite3.Connection | None = None) -> bool:
    if org_id is None:
        return True
    if conn is not None:
        row = conn.execute(
            "SELECT status FROM subscriptions WHERE org_id = ?", (org_id,)
        ).fetchone()
        return row is not None and row[0] == "active"
    with get_conn() as c:
        row = c.execute(
            "SELECT status FROM subscriptions WHERE org_id = ?", (org_id,)
        ).fetchone()
    return row is not None and row[0] == "active"

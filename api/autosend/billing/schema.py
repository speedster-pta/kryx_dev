"""
billing/schema.py

Platform-level org subscription billing tables. Unlike the tenant/unit-
scoped tables in storage/schema.py, these are new tables that key
directly off org_id (not unit_id) - per storage/schema.py's own
documented column-scoping convention: org_id lives authoritatively on
organisations, organisation_modules, organisation_module_grants, units,
users - and (as an exception noted there) any other table that
genuinely needs a direct, one-row(ish)-per-org relationship, which is
exactly the case for subscriptions/billing_transactions here.

Called once from core/db_init.py::init_db(), after core schema (these
tables FK into organisations(id)), mirroring how
integrations/pco/schema.py's init_pco_schema() is called on the same
connection.

Fresh project, no existing database to evolve - see storage/schema.py's
docstring for why there's no migration scaffolding here. Everything below
is CREATE TABLE IF NOT EXISTS, run idempotently on every startup.
"""

from __future__ import annotations


def init_billing_schema(conn) -> None:
    _create_billing_plans(conn)
    _create_billing_addons(conn)
    # module_key: added after the original table shape - links an add-on
    # to a storage.modules module key (e.g. 'pco') so buying it actually
    # grants+enables that module, not just a line item on an invoice. Not
    # every add-on maps to a module (e.g. "Extra Unit/Campus" doesn't), so
    # this stays nullable. Additive nullable column via the sanctioned
    # ALTER TABLE exception (see storage/schema.py::_add_column_if_missing's
    # own docstring), not a rename/recreate - this table already exists on
    # kryx-dev with real rows.
    from autosend.storage.schema import _add_column_if_missing
    _add_column_if_missing(conn, "billing_addons", "module_key", "module_key TEXT")
    _create_coupons(conn)
    _create_subscriptions(conn)
    # cancel_at: added after the original table shape - a cancellation
    # requested mid-period takes effect at the end of the current paid
    # period (same "stays active until period end" reasoning as the
    # existing pending_downgrade_* fields), so this just marks the
    # effective date without touching `status` until that date arrives
    # (see billing.engine.apply_pending_cancellations). Additive nullable
    # column via the same sanctioned ALTER TABLE exception as module_key
    # above.
    _add_column_if_missing(conn, "subscriptions", "cancel_at", "cancel_at TEXT")
    # billing_email: the email address Paystack tied to this
    # subscription's authorization_code at checkout time - recurring
    # charges (billing.engine.run_recurring_billing) must reuse this
    # exact email or Paystack's charge_authorization call rejects the
    # charge outright (confirmed against the live Paystack test API,
    # not a hypothetical - see billing/paystack.py's charge_authorization
    # docstring). Additive nullable column, same exception as above.
    _add_column_if_missing(conn, "subscriptions", "billing_email", "billing_email TEXT")
    _create_subscription_items(conn)
    _create_billing_transactions(conn)


# ---------------------------------------------------------------------------
# Catalogue tables - superadmin-managed via admin CRUD screens (see
# admin_views.py's BillingPlanAdmin/BillingAddonAdmin/CouponAdmin). Not
# tenant-scoped at all - these are the platform's own product catalogue,
# the same for every organisation.
# ---------------------------------------------------------------------------

def _create_billing_plans(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS billing_plans (
            id INTEGER PRIMARY KEY,
            key TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            price_cents INTEGER NOT NULL,
            interval TEXT NOT NULL DEFAULT 'monthly',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )


def _create_billing_addons(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS billing_addons (
            id INTEGER PRIMARY KEY,
            key TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            price_cents INTEGER NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )


def _create_coupons(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS coupons (
            id INTEGER PRIMARY KEY,
            code TEXT UNIQUE NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('percent','fixed')),
            amount INTEGER NOT NULL,
            expires_at TEXT,
            max_redemptions INTEGER,
            redemption_count INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )


# ---------------------------------------------------------------------------
# subscriptions - one row per org (org_id UNIQUE), direct org_id column per
# the exception noted in storage/schema.py's docstring (this is a genuinely
# org-level, not unit-scoped, concept). plan_id/pending_downgrade_plan_id
# are nullable so a superadmin manual comp (billing.engine.comp_org) can
# activate a subscription with no specific plan attached.
# ---------------------------------------------------------------------------

def _create_subscriptions(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY,
            org_id INTEGER NOT NULL UNIQUE REFERENCES organisations(id),
            plan_id INTEGER REFERENCES billing_plans(id),
            status TEXT NOT NULL CHECK(status IN ('pending_payment','active','past_due','cancelled')) DEFAULT 'pending_payment',
            paystack_customer_code TEXT,
            paystack_authorization_code TEXT,
            pending_downgrade_plan_id INTEGER REFERENCES billing_plans(id),
            pending_downgrade_effective_at TEXT,
            current_period_end TEXT,
            coupon_id INTEGER REFERENCES coupons(id),
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )


def _create_subscription_items(conn) -> None:
    # Add-ons currently attached to a subscription. removed_at (rather than
    # a hard delete) keeps a full history of what was ever added/removed -
    # list_active_addons_for_subscription() filters on removed_at IS NULL.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS subscription_items (
            id INTEGER PRIMARY KEY,
            subscription_id INTEGER NOT NULL REFERENCES subscriptions(id),
            addon_id INTEGER NOT NULL REFERENCES billing_addons(id),
            added_at TEXT NOT NULL DEFAULT (datetime('now')),
            removed_at TEXT
        )
        """
    )


def _create_billing_transactions(conn) -> None:
    # org_id is denormalised here (not just reachable via subscription_id)
    # so a transaction row still makes sense even for a kind='manual_override'
    # comp that may not always be tied to a fully-formed subscription
    # lifecycle, and so reporting/audit queries don't need a join for the
    # common case of "every transaction for this org".
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS billing_transactions (
            id INTEGER PRIMARY KEY,
            org_id INTEGER NOT NULL,
            subscription_id INTEGER REFERENCES subscriptions(id),
            provider TEXT NOT NULL,
            provider_reference TEXT,
            amount_cents INTEGER NOT NULL,
            status TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('initial','recurring','manual_override')),
            raw_payload TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )

"""
storage/modules.py

CRUD over organisation_modules. This is the single source of truth for
"is module X enabled for org Y" — integrations/pco (and future modules)
call is_enabled() rather than caching or re-deriving that answer
themselves, so toggling a module takes effect immediately everywhere
(scheduler, webhooks, admin nav) without a restart.
"""

from __future__ import annotations

import sqlite3
from typing import Iterable

from autosend.storage._db import _connect as get_conn

# Canonical module-key registry. A single shared (key, label) list rather
# than a string literal re-typed in admin_pages.py/scheduler.py/webhooks.py -
# adding a future module means adding one entry here, not hunting down every
# place "pco" was hardcoded.
#
# MODULE_SME_METRICS used to be MODULE_EMAIL_WA ("email_wa") - the
# Email-to-WhatsApp module originally had exactly one provider
# (smeMetrics), so "Email-to-WhatsApp" and "SME Metrics" were the same
# thing wearing two names. It's since been split into its own,
# permanently pre-configured integration (own module key, own settings/
# automations pages - see integrations/sme_metrics/), freeing "email_wa"
# for a genuinely generic, provider-agnostic Email-to-WhatsApp module
# built from scratch (see integrations/email_wa/). See
# _migrate_legacy_email_wa_module_key below for how already-provisioned
# orgs carried over to the new key without a manual data fix.
MODULE_PCO = "pco"
MODULE_SME_METRICS = "sme_metrics"
MODULE_EMAIL_WA = "email_wa"
MODULE_ICAL = "ical"
# Stitch Money used to be offered unconditionally to every org (see the
# now-outdated comment this replaced in admin_pages.py::TemplatesView) -
# it's now a real per-org module toggle like every other integration
# here, so an org's preferred payment provider is a deliberate choice
# (and a billable one, via a billing_addons row with module_key='stitch'),
# not just "on because credentials happen to exist for one unit".
MODULE_STITCH = "stitch"
AVAILABLE_MODULES: list[tuple[str, str]] = [
    # Alphabetical by label - this list drives the org detail page's
    # module grant/toggle checkboxes (admin_org_pages._module_rows_for_org)
    # and the superadmin nav, so its order is UI order, not declaration
    # order. Keep new entries in alphabetical position by hand.
    (MODULE_ICAL, "Calendar Invites (iCal)"),
    (MODULE_EMAIL_WA, "Email-to-WhatsApp"),
    (MODULE_PCO, "Planning Center Online"),
    (MODULE_SME_METRICS, "SME Metrics"),
    (MODULE_STITCH, "Stitch Payments"),
]


def migrate_legacy_email_wa_module_key(conn: sqlite3.Connection) -> None:
    """One-time data fix, not a recurring schema migration (see
    storage/schema.py's docstring on why this app has neither an ALTER
    TABLE story nor a migration-history table): back when MODULE_SME_METRICS
    didn't exist and "email_wa" meant smeMetrics specifically, any org
    granted/enabled for it stored module_key='email_wa'. Now that
    'email_wa' has been repurposed for the new generic integration, those
    old rows have to become 'sme_metrics' instead, or the org would
    silently lose access to what it actually paid for.

    Guarded by a single-row marker table (not a general migrations
    mechanism - just enough to make this one historical rename safe to
    call on every startup) so this can never re-fire after the first
    successful run. That matters because, going forward, a real org can
    legitimately be granted/enabled for the *new* 'email_wa' module - if
    this ran unconditionally on every startup, it would keep wrongly
    renaming those brand-new rows to 'sme_metrics' too. Call once, from
    core/db_init.py, after storage.schema.init_core_schema (the owning
    tables must already exist)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS _sme_metrics_rename_migration (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            applied_at TEXT NOT NULL
        )
        """
    )
    already_applied = conn.execute("SELECT 1 FROM _sme_metrics_rename_migration WHERE id = 1").fetchone()
    if already_applied:
        return
    conn.execute(
        "UPDATE organisation_module_grants SET module_key = ? WHERE module_key = 'email_wa'",
        (MODULE_SME_METRICS,),
    )
    conn.execute(
        "UPDATE organisation_modules SET module_key = ? WHERE module_key = 'email_wa'",
        (MODULE_SME_METRICS,),
    )
    conn.execute(
        "INSERT INTO _sme_metrics_rename_migration (id, applied_at) VALUES (1, datetime('now'))"
    )


def is_enabled(org_id: int, module_key: str, conn: sqlite3.Connection | None = None) -> bool:
    if conn is not None:
        row = conn.execute(
            """
            SELECT 1 FROM organisation_modules
            WHERE org_id = ? AND module_key = ? AND disabled_at IS NULL
            """,
            (org_id, module_key),
        ).fetchone()
        return row is not None

    with get_conn() as c:
        row = c.execute(
            """
            SELECT 1 FROM organisation_modules
            WHERE org_id = ? AND module_key = ? AND disabled_at IS NULL
            """,
            (org_id, module_key),
        ).fetchone()
        return row is not None


def is_granted(org_id: int, module_key: str, conn: sqlite3.Connection | None = None) -> bool:
    query = "SELECT 1 FROM organisation_module_grants WHERE org_id = ? AND module_key = ?"
    if conn is not None:
        return conn.execute(query, (org_id, module_key)).fetchone() is not None
    with get_conn() as c:
        return c.execute(query, (org_id, module_key)).fetchone() is not None


def grant(org_id: int, module_key: str) -> None:
    """Superadmin-only entitlement: makes module_key available for this
    org to enable, per its payment tier/agreement. Must happen before
    enable() will succeed — see enable()'s check below."""
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO organisation_module_grants (org_id, module_key, granted_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(org_id, module_key) DO NOTHING
            """,
            (org_id, module_key),
        )


def revoke(org_id: int, module_key: str) -> None:
    """Also disables the module if it was enabled — a revoked entitlement
    must not leave the module silently still running."""
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM organisation_module_grants WHERE org_id = ? AND module_key = ?",
            (org_id, module_key),
        )
        conn.execute(
            "UPDATE organisation_modules SET disabled_at = datetime('now') "
            "WHERE org_id = ? AND module_key = ? AND disabled_at IS NULL",
            (org_id, module_key),
        )


def granted_modules_for_org(org_id: int) -> list[str]:
    """Used by the Modules admin page to know which enable/disable
    checkboxes an org (or its org-admin staff) is even allowed to see."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT module_key FROM organisation_module_grants WHERE org_id = ?",
            (org_id,),
        ).fetchall()
        return [r[0] for r in rows]


def enable(org_id: int, module_key: str) -> None:
    with get_conn() as conn:
        if not is_granted(org_id, module_key, conn=conn):
            raise ValueError(
                f"org_id={org_id} is not granted module '{module_key}' — grant it first."
            )
        conn.execute(
            """
            INSERT INTO organisation_modules (org_id, module_key, enabled_at, disabled_at)
            VALUES (?, ?, datetime('now'), NULL)
            ON CONFLICT(org_id, module_key)
            DO UPDATE SET enabled_at = datetime('now'), disabled_at = NULL
            """,
            (org_id, module_key),
        )


def disable(org_id: int, module_key: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE organisation_modules
            SET disabled_at = datetime('now')
            WHERE org_id = ? AND module_key = ?
            """,
            (org_id, module_key),
        )


def orgs_with_module_enabled(module_key: str) -> list[int]:
    """
    Used by integration schedulers at startup (and on their own recheck
    interval) to decide whether their APScheduler job needs to exist at
    all — e.g. integrations/pco/scheduler.py only registers
    recheck_deferred_serving_reminders if this is non-empty.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT org_id FROM organisation_modules WHERE module_key = ? AND disabled_at IS NULL",
            (module_key,),
        ).fetchall()
        return [r[0] for r in rows]


def enabled_modules_for_org(org_id: int) -> list[str]:
    """Used by admin UI nav rendering to decide which module sections to show."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT module_key FROM organisation_modules WHERE org_id = ? AND disabled_at IS NULL",
            (org_id,),
        ).fetchall()
        return [r[0] for r in rows]

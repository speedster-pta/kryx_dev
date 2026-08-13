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

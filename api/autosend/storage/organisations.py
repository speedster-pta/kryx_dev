"""
storage/organisations.py

CRUD for the organisations table. Platform-admin-facing (org
provisioning) rather than per-org-staff-facing.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from autosend.storage._db import _connect as get_conn


@dataclass(frozen=True)
class Organisation:
    id: int
    name: str
    slug: str
    active: bool
    created_at: str


def _row_to_org(row: sqlite3.Row) -> Organisation:
    # _connect() yields plain tuple rows (no sqlite3.Row row_factory), same
    # as every other module in this package - positional, matching the
    # organisations table's column order (id, name, slug, active, created_at).
    return Organisation(
        id=row[0], name=row[1], slug=row[2], active=bool(row[3]), created_at=row[4],
    )


def create_organisation(name: str, slug: str) -> Organisation:
    """Every organisation must have at least one unit (see
    admin_views.UnitAdmin.delete_model's matching last-unit guard) - so
    the default "Main" unit is created in the same transaction as the
    org itself, rather than left to a separate call a caller might skip.
    "Main" rather than repeating the org's own name: staff who later add
    a second unit would otherwise end up with a unit confusingly named
    after the whole organisation."""
    with get_conn() as conn:
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "INSERT INTO organisations (name, slug, created_at) VALUES (?, ?, ?)",
            (name, slug, now),
        )
        org_id = cur.lastrowid
        conn.execute(
            "INSERT INTO units (org_id, slug, name, created_at) VALUES (?, ?, ?, ?)",
            (org_id, "main", "Main", now),
        )
        row = conn.execute("SELECT * FROM organisations WHERE id = ?", (org_id,)).fetchone()
        return _row_to_org(row)


def get_organisation(org_id: int) -> Organisation | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM organisations WHERE id = ?", (org_id,)).fetchone()
        return _row_to_org(row) if row else None


def get_organisation_by_slug(slug: str) -> Organisation | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM organisations WHERE slug = ?", (slug,)).fetchone()
        return _row_to_org(row) if row else None


def list_organisations(active_only: bool = True) -> list[Organisation]:
    with get_conn() as conn:
        if active_only:
            rows = conn.execute("SELECT * FROM organisations WHERE active = 1 ORDER BY name").fetchall()
        else:
            rows = conn.execute("SELECT * FROM organisations ORDER BY name").fetchall()
        return [_row_to_org(r) for r in rows]


def deactivate_organisation(org_id: int) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE organisations SET active = 0 WHERE id = ?", (org_id,))

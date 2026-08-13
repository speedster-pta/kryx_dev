"""
integrations/pco/storage.py

Data access for PCO's own tables. Allowed to import from storage._db
(the connection helper — no PCO-shaped assumptions) and core.crypto
(shared encryption). Does NOT import from other storage/*.py business
modules directly to reach across org/unit boundaries — it takes
org_id / unit_id as explicit arguments, same discipline as
storage/units.py.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from shofar_automation.storage._db import _connect as get_conn
from shofar_automation.core.crypto import encrypt, decrypt


@dataclass(frozen=True)
class PcoOrgSettings:
    org_id: int
    pco_token_id: str | None
    pco_token_secret: str | None  # decrypted


def get_org_settings(org_id: int) -> PcoOrgSettings | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM pco_organization_settings WHERE org_id = ?", (org_id,)
        ).fetchone()
        if row is None:
            return None
        return PcoOrgSettings(
            org_id=row["org_id"],
            pco_token_id=row["pco_token_id"],
            pco_token_secret=decrypt(row["pco_token_secret"]) if row["pco_token_secret"] else None,
        )


def upsert_org_settings(org_id: int, pco_token_id: str, pco_token_secret: str) -> None:
    encrypted_secret = encrypt(pco_token_secret)
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO pco_organization_settings (org_id, pco_token_id, pco_token_secret, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(org_id) DO UPDATE SET
                pco_token_id = excluded.pco_token_id,
                pco_token_secret = excluded.pco_token_secret,
                updated_at = datetime('now')
            """,
            (org_id, pco_token_id, encrypted_secret),
        )


@dataclass(frozen=True)
class PcoUnitSettings:
    unit_id: int
    pco_campus_id: str | None
    pco_webhook_secret: str | None  # decrypted


def get_unit_settings(unit_id: int) -> PcoUnitSettings | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM pco_unit_settings WHERE unit_id = ?", (unit_id,)
        ).fetchone()
        if row is None:
            return None
        return PcoUnitSettings(
            unit_id=row["unit_id"],
            pco_campus_id=row["pco_campus_id"],
            pco_webhook_secret=(
                decrypt(row["pco_webhook_secret"]) if row["pco_webhook_secret"] else None
            ),
        )


def upsert_unit_settings(unit_id: int, pco_campus_id: str, pco_webhook_secret: str) -> None:
    encrypted_secret = encrypt(pco_webhook_secret)
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO pco_unit_settings (unit_id, pco_campus_id, pco_webhook_secret)
            VALUES (?, ?, ?)
            ON CONFLICT(unit_id) DO UPDATE SET
                pco_campus_id = excluded.pco_campus_id,
                pco_webhook_secret = excluded.pco_webhook_secret
            """,
            (unit_id, pco_campus_id, encrypted_secret),
        )


def find_unit_id_by_webhook_secret(raw_secret: str) -> int | None:
    """
    Used by webhooks.py to resolve which unit a signed callback
    belongs to. Secrets are encrypted at rest, so this scans and decrypts
    rather than querying ciphertext directly — acceptable at expected
    per-org unit counts; revisit with a lookup hash column if unit
    counts grow large enough to matter.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT unit_id, pco_webhook_secret FROM pco_unit_settings "
            "WHERE pco_webhook_secret IS NOT NULL"
        ).fetchall()
    for row in rows:
        try:
            if decrypt(row["pco_webhook_secret"]) == raw_secret:
                return row["unit_id"]
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Service type cache, keyed (unit_id, cached_date)
# ---------------------------------------------------------------------------

def get_cached_service_types(unit_id: int, cached_date: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT payload_json FROM pco_service_type_cache WHERE unit_id = ? AND cached_date = ?",
            (unit_id, cached_date),
        ).fetchone()
        return row["payload_json"] if row else None


def set_cached_service_types(unit_id: int, cached_date: str, payload_json: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO pco_service_type_cache (unit_id, cached_date, payload_json, refreshed_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(unit_id, cached_date) DO UPDATE SET
                payload_json = excluded.payload_json,
                refreshed_at = datetime('now')
            """,
            (unit_id, cached_date, payload_json),
        )

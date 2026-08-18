"""
storage/email_wa.py

CRUD over email_integrations, plus the processed_inbound_emails dedup
guard. Mirrors storage/units.py's form-mapping functions and
storage/dedup.py's processed_form_submissions functions respectively.
"""

import secrets
from datetime import datetime, timezone

from ._db import _connect


def generate_local_part() -> str:
    """High-entropy, unguessable receiving-address local part. Can't be
    anything derived from unit_id/provider_key: SendGrid Inbound Parse is
    configured per MX hostname, not per address, so every local part
    under that hostname reaches the same webhook URL - this token is the
    only thing distinguishing one integration's mail from another's once
    a request reaches that URL."""
    return secrets.token_urlsafe(12)


def create_email_integration(
    unit_id: int, provider_key: str, email_type: str, whatsapp_template_id: int,
) -> dict:
    local_part = generate_local_part()
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO email_integrations
                (unit_id, provider_key, email_type, local_part, whatsapp_template_id, active, created_at)
            VALUES (?, ?, ?, ?, ?, 1, ?)
            """,
            (
                unit_id, provider_key, email_type, local_part, whatsapp_template_id,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        integration_id = cur.lastrowid
    return {"id": integration_id, "local_part": local_part}


def upsert_email_integration(
    unit_id: int, provider_key: str, email_type: str, template_name: str,
    body_variable_order: list[str], whatsapp_number_id: int | None,
    button_variables: list[str], header_image_url: str | None, active: bool,
) -> dict:
    """Creates or updates the (unit, provider, email_type) integration and
    its owning whatsapp_templates row together, under a synthetic
    per-integration template_type ("email_wa:<provider>:<email_type>") -
    same reasoning as storage/units.py's upsert_form_mapping and its
    "form:<pco_form_id>" scheme, needed because whatsapp_templates has
    UNIQUE(unit_id, template_type).

    local_part is generated once, only on first insert - the ON CONFLICT
    clause below never touches it, so re-saving an existing integration
    (e.g. to point it at a different template) can't invalidate an
    address already handed to a booking platform's notification
    settings. The generated value is only actually used when this is a
    fresh insert; SQLite still requires *a* value for the NOT NULL UNIQUE
    column even on an update that will discard it."""
    from .units import _upsert_whatsapp_template_row

    template_type = f"email_wa:{provider_key}:{email_type}"
    whatsapp_template_id = _upsert_whatsapp_template_row(
        unit_id, template_type, template_name, body_variable_order,
        whatsapp_number_id, button_variables, header_image_url, active,
    )
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO email_integrations
                (unit_id, provider_key, email_type, local_part, whatsapp_template_id, active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(unit_id, provider_key, email_type) DO UPDATE SET
                whatsapp_template_id = excluded.whatsapp_template_id,
                active = excluded.active
            """,
            (
                unit_id, provider_key, email_type, generate_local_part(), whatsapp_template_id,
                int(active), datetime.now(timezone.utc).isoformat(),
            ),
        )
        row = conn.execute(
            "SELECT id, local_part FROM email_integrations WHERE unit_id = ? AND provider_key = ? AND email_type = ?",
            (unit_id, provider_key, email_type),
        ).fetchone()
    return {"id": row[0], "local_part": row[1]}


def delete_email_integration(integration_id: int) -> None:
    """Leaves the owning whatsapp_templates row in place, same convention
    as storage/units.py's delete_form_mapping (which only removes the
    form_templates row, not the template) - the template can still be
    reused if this integration is later recreated for the same
    unit/provider/email_type, since the upsert above is keyed on that
    triple, not on the template row."""
    with _connect() as conn:
        conn.execute("DELETE FROM email_integrations WHERE id = ?", (integration_id,))


def get_email_integration_by_id(integration_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, unit_id FROM email_integrations WHERE id = ?", (integration_id,)
        ).fetchone()
        return {"id": row[0], "unit_id": row[1]} if row else None


def get_email_integration_by_local_part(local_part: str) -> dict | None:
    """Joins in the fields the ingestion pipeline needs from the owning
    unit (org_id for the module gate, slug for logging, default_region
    for phone normalisation) so services/email_wa.py doesn't need a
    second round trip per inbound email."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT e.id, e.unit_id, e.provider_key, e.email_type, e.whatsapp_template_id, e.active,
                   u.org_id, u.slug, u.default_region
            FROM email_integrations e
            JOIN units u ON u.id = e.unit_id
            WHERE e.local_part = ?
            """,
            (local_part,),
        ).fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "unit_id": row[1],
            "provider_key": row[2],
            "email_type": row[3],
            "whatsapp_template_id": row[4],
            "active": bool(row[5]),
            "org_id": row[6],
            "unit_slug": row[7],
            "default_region": row[8],
        }


def list_email_integrations(unit_ids: list[int] | None) -> list[dict]:
    """unit_ids=None means unrestricted (superadmin) - same convention as
    list_form_mappings."""
    from .scoping import unit_scope_clause

    with _connect() as conn:
        base = """
            SELECT e.id, e.unit_id, u.name AS unit_name, e.provider_key, e.email_type,
                   e.local_part, e.active, e.whatsapp_template_id, t.template_name
            FROM email_integrations e
            JOIN units u ON u.id = e.unit_id
            JOIN whatsapp_templates t ON t.id = e.whatsapp_template_id
        """
        scope = unit_scope_clause("e.unit_id", unit_ids, joiner="WHERE")
        if scope is None:
            return []
        clause, params = scope
        rows = conn.execute(
            base + clause + " ORDER BY u.name, e.provider_key, e.email_type", params
        ).fetchall()
        columns = [
            "id", "unit_id", "unit_name", "provider_key", "email_type",
            "local_part", "active", "whatsapp_template_id", "template_name",
        ]
        return [dict(zip(columns, r)) for r in rows]


def is_inbound_email_processed(dedup_key: str) -> bool:
    """Only counts as processed if it previously SENT successfully - same
    semantics as storage/dedup.py's is_form_submission_processed."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_inbound_emails WHERE dedup_key = ? AND status = 'sent'",
            (dedup_key,),
        ).fetchone()
        return row is not None


def mark_inbound_email_processed(dedup_key: str, status: str, detail: str = "") -> None:
    """status is 'sent' or 'failed'."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO processed_inbound_emails (dedup_key, processed_at, status, detail)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(dedup_key) DO UPDATE SET
                processed_at = excluded.processed_at,
                status = excluded.status,
                detail = excluded.detail
            """,
            (dedup_key, datetime.now(timezone.utc).isoformat(), status, detail),
        )

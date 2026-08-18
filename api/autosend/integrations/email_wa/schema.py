"""
integrations/email_wa/schema.py

Fresh tables only, no migration history - see storage/schema.py's
docstring for why (CREATE TABLE IF NOT EXISTS in final shape, no ALTER
TABLE except the one guarded exception used for units.default_region).

Deliberately separate tables from integrations/sme_metrics/schema.py's
email_integrations/processed_inbound_emails, even though the shape is
identical - this is a distinct module with its own org-level
grant/enable (storage.MODULE_EMAIL_WA), so its rows must never be mixed
with SME Metrics' (see integrations/email_wa/__init__.py for the full
history of why these two modules exist side by side).

Must run after storage.schema.init_core_schema() on the same connection:
email_wa_integrations references units(id) and whatsapp_templates(id).
See core/db_init.py for call order.
"""

from __future__ import annotations


def init_email_wa_schema(conn) -> None:
    _create_email_wa_integrations(conn)
    _create_processed_email_wa_inbound_emails(conn)


# ---------------------------------------------------------------------------
# email_wa_integrations - one row per (unit, provider, email_type), same
# design as integrations/sme_metrics/schema.py's email_integrations (see
# that file's own comment for the full reasoning) - kept in lockstep
# shape-wise since both exist to solve the same problem, just for a
# different, independently-gated set of providers.
# ---------------------------------------------------------------------------

def _create_email_wa_integrations(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS email_wa_integrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            unit_id INTEGER NOT NULL REFERENCES units(id) ON DELETE CASCADE,
            provider_key TEXT NOT NULL,
            email_type TEXT NOT NULL,
            local_part TEXT NOT NULL UNIQUE,
            whatsapp_template_id INTEGER NOT NULL REFERENCES whatsapp_templates(id),
            active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            UNIQUE(unit_id, provider_key, email_type)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_email_wa_integrations_local_part ON email_wa_integrations(local_part)"
    )


# ---------------------------------------------------------------------------
# processed_email_wa_inbound_emails - dedup guard, same shape/purpose as
# integrations/sme_metrics/schema.py's processed_inbound_emails.
# ---------------------------------------------------------------------------

def _create_processed_email_wa_inbound_emails(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS processed_email_wa_inbound_emails (
            dedup_key TEXT PRIMARY KEY,
            processed_at TEXT NOT NULL,
            status TEXT NOT NULL,
            detail TEXT
        )
        """
    )

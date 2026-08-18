"""
integrations/email_wa/schema.py

Fresh tables only, no migration history - see storage/schema.py's
docstring for why (CREATE TABLE IF NOT EXISTS in final shape, no ALTER
TABLE except the one guarded exception used for units.default_region).

Must run after storage.schema.init_core_schema() on the same connection:
email_integrations references units(id) and whatsapp_templates(id). See
core/db_init.py for call order.
"""

from __future__ import annotations


def init_email_wa_schema(conn) -> None:
    _create_email_integrations(conn)
    _create_processed_inbound_emails(conn)


# ---------------------------------------------------------------------------
# email_integrations - one row per (unit, provider, email_type): a single
# receiving address bound to exactly one provider's parser for exactly one
# of that provider's email sub-types (e.g. sme_metrics/booking_request vs
# sme_metrics/cancelled), sending through exactly one WhatsApp template.
# Deliberately not split into a separate "receiving address" table plus a
# "rule" table - unlike the free-regex design this replaced, there is no
# rule-priority/ambiguous-match problem here: the (unit, provider,
# email_type) triple IS the address, so an address can never resolve to
# more than one integration.
#
# provider_key/email_type are plain strings resolved against the in-code
# provider registry (integrations/email_wa/providers/), not FKs - like
# storage.modules.AVAILABLE_MODULES, providers are code, not data, so
# adding one is a deploy, not a migration.
# ---------------------------------------------------------------------------

def _create_email_integrations(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS email_integrations (
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
    # Looked up by local_part on every inbound webhook delivery (see
    # integrations/email_wa/webhook.py) - this is the hot path, not a rare
    # admin-page query, so it gets its own index rather than relying on
    # the UNIQUE constraint's implicit one alone being enough by luck.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_email_integrations_local_part ON email_integrations(local_part)"
    )


# ---------------------------------------------------------------------------
# processed_inbound_emails - dedup guard, same shape/purpose as
# processed_form_submissions in integrations/pco/schema.py. Weaker than
# that table by necessity: PCO submissions have a real resource ID from
# PCO's API, but SendGrid Inbound Parse doesn't guarantee one - dedup_key
# is the email's Message-Id header when present, falling back to a hash
# of (to, from, subject, timestamp) when it's missing.
# ---------------------------------------------------------------------------

def _create_processed_inbound_emails(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS processed_inbound_emails (
            dedup_key TEXT PRIMARY KEY,
            processed_at TEXT NOT NULL,
            status TEXT NOT NULL,
            detail TEXT
        )
        """
    )

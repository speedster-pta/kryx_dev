"""
core/db_init.py

Single startup entry point. Opens one connection and runs the core schema
(organisations, units, campaigns, ...) followed by the PCO schema
(pco_organization_settings, form_templates, serving_reminder_*, ...) and
the email_wa schema (email_integrations, processed_inbound_emails) on
that same connection/transaction.

Named db_init rather than "migrations": this is a fresh project with no
existing database, so there's no schema history to migrate through — just
first-time table creation. Runs on every startup regardless (cheap,
CREATE TABLE IF NOT EXISTS); revisit the name if/when real migrations
(schema changes against a database that already holds data) become
necessary.
"""

from __future__ import annotations

from autosend.storage._db import DB_PATH, _connect
from autosend.storage.schema import init_core_schema
from autosend.integrations.pco.schema import init_pco_schema
from autosend.integrations.email_wa.schema import init_email_wa_schema


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        init_core_schema(conn)
        init_pco_schema(conn)
        init_email_wa_schema(conn)
        conn.commit()

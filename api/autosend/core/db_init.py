"""
core/db_init.py

Single startup entry point. Opens one connection and runs the core schema
(organisations, units, campaigns, ...) followed by the PCO schema
(pco_organization_settings, form_templates, serving_reminder_*, ...), the
sme_metrics schema (email_integrations, processed_inbound_emails), and
the (separate, genuinely generic) email_wa schema
(email_wa_integrations, processed_email_wa_inbound_emails) on that same
connection/transaction - then, once organisation_modules/
organisation_module_grants exist, the one-time legacy module_key rename
(see storage/modules.py's migrate_legacy_email_wa_module_key for why this
is a guarded data fix, not a recurring migration).

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
from autosend.storage.modules import migrate_legacy_email_wa_module_key
from autosend.integrations.pco.schema import init_pco_schema
from autosend.integrations.sme_metrics.schema import init_sme_metrics_schema
from autosend.integrations.email_wa.schema import init_email_wa_schema


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        init_core_schema(conn)
        init_pco_schema(conn)
        init_sme_metrics_schema(conn)
        init_email_wa_schema(conn)
        migrate_legacy_email_wa_module_key(conn)
        conn.commit()

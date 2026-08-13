"""
integrations/pco/schema.py

PCO-specific tables only, split out from core per
shofar-multiorg-context-seed.md §6. Everything here is conditional on
organisation_modules.pco.enabled for a given org at the application/
webhook layer — init_pco_schema() itself always creates these tables
unconditionally (idempotent, cheap). The module gate controls whether the
*rest of the app* (scheduler jobs, webhook routes, UI) touches them, not
whether they exist.

Must be called after storage.schema.init_core_schema() on the same
connection, since:
  - pco_organization_settings.org_id references organisations(id)
  - form_templates / serving_reminder_rules reference units(id) and
    whatsapp_templates(id), both created by core
See core/db_init.py for the call order.

Fresh project, no existing database to evolve — see storage/schema.py's
docstring for why there's no migration scaffolding here.
"""

from __future__ import annotations


def init_pco_schema(conn) -> None:
    _create_pco_organization_settings(conn)
    _create_form_templates(conn)
    _create_serving_reminder_rules(conn)
    _create_serving_reminder_log(conn)
    _create_serving_service_type_cache(conn)
    _create_processed_registrations(conn)
    _create_signup_watermark(conn)
    _create_processed_form_submissions(conn)


# ---------------------------------------------------------------------------
# pco_organization_settings — one row per organisation that has connected
# PCO (a customer's own PCO Personal Access Token, genuinely per-org —
# unlike meta_platform_settings in core, which is the platform's own app).
# ---------------------------------------------------------------------------

def _create_pco_organization_settings(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pco_organization_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            org_id INTEGER NOT NULL UNIQUE REFERENCES organisations(id),
            pco_token_id TEXT NOT NULL,
            pco_token_secret TEXT,
            created_at TEXT NOT NULL
        )
        """
    )


# ---------------------------------------------------------------------------
# form_templates — scoped via unit_id, same org_id-via-join reasoning as
# core's unit-scoped tables.
# ---------------------------------------------------------------------------

def _create_form_templates(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS form_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            unit_id INTEGER NOT NULL REFERENCES units(id) ON DELETE CASCADE,
            pco_form_id TEXT NOT NULL,
            whatsapp_template_id INTEGER NOT NULL REFERENCES whatsapp_templates(id),
            active INTEGER DEFAULT 1,
            UNIQUE(unit_id, pco_form_id)
        )
        """
    )


# ---------------------------------------------------------------------------
# Serving Reminders (PCO Services team-member reminders).
# ---------------------------------------------------------------------------

def _create_serving_reminder_rules(conn) -> None:
    # One rule per unit per PCO service type: when to fire (day/time, in
    # that rule's own timezone), who to include (status_filter), and what
    # to send. Like form_templates, each rule owns a synthetic
    # whatsapp_templates row (template_type = f"serving:{service_type_id}")
    # so it gets its own whatsapp_number_id/body_variable_order/
    # button_variables/header_image_url via the existing template
    # machinery — no new template-config concept needed.
    #
    # No UNIQUE(unit_id, pco_service_type_id): a unit can have more than
    # one serving-reminder rule for the same PCO service type — e.g. a
    # separate Wednesday-evening reminder alongside an existing
    # Sunday-morning one — so this is intentionally NOT a
    # one-mapping-per-key table the way form_templates is.
    #
    # plan_selection_mode defaults to 'next_event'; days_ahead is only
    # meaningful (and only enforced as required) when
    # plan_selection_mode='days_ahead' — enforced by upsert_serving_rule's
    # application-level validation, not a DB constraint.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS serving_reminder_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            unit_id INTEGER NOT NULL REFERENCES units(id) ON DELETE CASCADE,
            pco_service_type_id TEXT NOT NULL,
            pco_service_type_name TEXT,
            send_day_of_week TEXT NOT NULL,
            send_time TEXT NOT NULL,
            timezone TEXT NOT NULL DEFAULT 'Africa/Johannesburg',
            status_filter TEXT NOT NULL DEFAULT 'confirmed_only',
            whatsapp_template_id INTEGER NOT NULL REFERENCES whatsapp_templates(id),
            active INTEGER DEFAULT 1,
            plan_selection_mode TEXT NOT NULL DEFAULT 'next_event',
            days_ahead INTEGER,
            created_at TEXT NOT NULL
        )
        """
    )


def _create_serving_reminder_log(conn) -> None:
    # Idempotency + audit trail for each (rule, plan, person): whether
    # fired by the scheduled job or the manual "send now" button, a person
    # already sent for a given plan under a given rule is never sent to
    # again for that same plan. Separate from send_log (core; stays the
    # append-only cross-automation history/reporting table) for the same
    # reason processed_registrations/processed_form_submissions are
    # separate from send_log — this table answers "have we already
    # notified this person about this plan", send_log answers "what
    # happened on every attempt, for reporting".
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS serving_reminder_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id INTEGER NOT NULL REFERENCES serving_reminder_rules(id) ON DELETE CASCADE,
            pco_plan_id TEXT NOT NULL,
            pco_person_id TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            status TEXT NOT NULL,
            detail TEXT,
            UNIQUE(rule_id, pco_plan_id, pco_person_id)
        )
        """
    )


def _create_serving_service_type_cache(conn) -> None:
    # One row per (unit, service type) currently in scope for that unit's
    # PCO campus, per Services v2 folder->campus scoping. Refreshed
    # wholesale (delete+reinsert) the first time a unit is selected in the
    # serving-rule editor each day, rather than incrementally —
    # cached_date is the freshness check: any row for a unit not stamped
    # with today's date is treated as stale and the whole set is re-polled
    # from PCO.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS serving_service_type_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            unit_id INTEGER NOT NULL REFERENCES units(id) ON DELETE CASCADE,
            pco_service_type_id TEXT NOT NULL,
            pco_service_type_name TEXT NOT NULL,
            cached_date TEXT NOT NULL,
            UNIQUE(unit_id, pco_service_type_id)
        )
        """
    )


# ---------------------------------------------------------------------------
# Dedup/idempotency tables for the registration poller and form-response
# webhook. No unit_id/org_id column: keyed purely on PCO's own IDs, which
# are unique within a single PCO account's own namespace, and each
# organisation using this module has its own separate PCO account/token —
# so there is no realistic cross-org collision to guard against here,
# unlike the unit-scoped tables above.
# ---------------------------------------------------------------------------

def _create_processed_registrations(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS processed_registrations (
            registration_id TEXT PRIMARY KEY,
            signup_id TEXT,
            processed_at TEXT NOT NULL,
            status TEXT NOT NULL,
            detail TEXT
        )
        """
    )


def _create_signup_watermark(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS signup_watermark (
            signup_id TEXT PRIMARY KEY,
            last_seen_registration_id TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )


def _create_processed_form_submissions(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS processed_form_submissions (
            submission_id TEXT PRIMARY KEY,
            person_id TEXT,
            processed_at TEXT NOT NULL,
            status TEXT NOT NULL,
            detail TEXT
        )
        """
    )

"""
storage/schema.py

Core schema only: organisations, organisation_modules, units, users,
and every table the WhatsApp Campaign Sender needs with zero PCO
dependency.

PCO-specific tables (pco_organization_settings, form_templates,
serving_reminder_rules, serving_reminder_log, serving_service_type_cache,
processed_registrations, signup_watermark, processed_form_submissions)
live in integrations/pco/schema.py and are initialised separately by
core/db_init.py — see that module for call order. This split exists so an
organisation with the PCO module disabled pays no schema/scheduler
overhead for tables it will never populate, per
shofar-multiorg-context-seed.md §3/§6.

This is a fresh project with no existing database to evolve, so there is
no migration history here (no renames, no ALTER TABLE, no backfills) —
just CREATE TABLE IF NOT EXISTS in final shape. IF NOT EXISTS is kept
purely as cheap idempotency (safe to run on every startup, safe if two
workers race on first boot), not because there's a prior schema shape to
guard against. If/when this schema needs to change after real data
exists, that's the point to introduce the parent project's migration
discipline (rename -> recreate -> copy -> drop, PRAGMA-guarded, never
ALTER TABLE DROP COLUMN) — not before.

Design decision worth flagging: org_id lives authoritatively on
`organisations`, `organisation_modules`, `units`, and `users`.
Every unit-scoped table below (whatsapp_numbers, whatsapp_templates,
campaigns, campaign_recipients, send_log, whatsapp_onboarding_intents,
...) scopes via its unit_id FK rather than also carrying a duplicated
org_id column. Because unit_id is NOT NULL and units.org_id is NOT NULL,
isolation stays airtight via a join through units, without denormalising
org_id onto every table up front. If a hot-path query profile later shows
the join is a real cost, adding org_id to specific tables is a cheap,
targeted follow-up.
"""

from __future__ import annotations


def init_core_schema(conn) -> None:
    _create_organisations(conn)
    _create_organisation_modules(conn)
    _create_units(conn)
    _create_meta_platform_settings(conn)
    _create_whatsapp_numbers(conn)
    _create_whatsapp_templates(conn)
    _create_users(conn)
    _create_user_units(conn)
    _create_campaigns(conn)
    _create_campaign_recipients(conn)
    _create_message_log(conn)
    _create_waba_limits(conn)
    _create_login_attempts(conn)
    _create_send_log(conn)
    _create_whatsapp_onboarding_intents(conn)


# ---------------------------------------------------------------------------
# organisations / organisation_modules — the top-level tenant and its
# entitlement gate.
# ---------------------------------------------------------------------------

def _create_organisations(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS organisations (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            slug        TEXT NOT NULL UNIQUE,
            active      INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT NOT NULL
        )
        """
    )


def _create_organisation_modules(conn) -> None:
    # Lives in core (not integrations/pco/) even though PCO is currently
    # its only consumer: this is the generic mechanism future modules key
    # off, and core owns "what's enabled" while integrations own "what
    # happens when enabled". Also doubles as a future plan/entitlement
    # gate (context seed §4.7), even though billing itself is out of scope
    # for this fork.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS organisation_modules (
            org_id      INTEGER NOT NULL REFERENCES organisations(id),
            module_key  TEXT NOT NULL,
            enabled_at  TEXT NOT NULL,
            disabled_at TEXT,
            PRIMARY KEY (org_id, module_key)
        )
        """
    )
    # organisation_module_grants — the entitlement/plan-tier layer, one
    # level above organisation_modules. A superadmin grants a module to an
    # org (their payment tier/agreement allows it); only then can that
    # org's own org-admin staff flip it on/off via organisation_modules.
    # Kept as a separate table rather than an extra column on
    # organisation_modules so "granted but not yet enabled" and "enabled
    # without being granted" (which storage.modules.enable() refuses) stay
    # cleanly distinguishable.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS organisation_module_grants (
            org_id      INTEGER NOT NULL REFERENCES organisations(id),
            module_key  TEXT NOT NULL,
            granted_at  TEXT NOT NULL,
            PRIMARY KEY (org_id, module_key)
        )
        """
    )


# ---------------------------------------------------------------------------
# units — generalised "congregation" (context seed §8.1 terminology
# question; "unit" is the working term), scoped under an organisation.
# ---------------------------------------------------------------------------

def _create_units(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS units (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            org_id INTEGER NOT NULL REFERENCES organisations(id),
            slug TEXT NOT NULL,
            name TEXT NOT NULL,
            active INTEGER DEFAULT 1,

            pco_webhook_secret TEXT,
            pco_webhook_user_name TEXT,
            pco_campus_id TEXT,

            created_at TEXT NOT NULL,
            UNIQUE(org_id, slug)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_units_org_id ON units(org_id)")


# ---------------------------------------------------------------------------
# meta_platform_settings — the platform provider's own Meta developer app
# credentials for WhatsApp Embedded Signup. Deliberately a platform-wide
# singleton, NOT per-organisation: this is the app used to onboard every
# organisation's own WhatsApp Business Account, not a credential any
# individual organisation owns. Unlike pco_organization_settings (a
# customer's own PCO token — genuinely per-org), there is exactly one of
# these regardless of tenant count.
# ---------------------------------------------------------------------------

def _create_meta_platform_settings(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meta_platform_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            app_id TEXT NOT NULL,
            app_secret TEXT,
            config_id TEXT NOT NULL,
            webhook_verify_token TEXT,
            created_at TEXT NOT NULL
        )
        """
    )


# ---------------------------------------------------------------------------
# whatsapp_numbers — a unit can have more than one WhatsApp number (e.g. a
# main line plus a youth ministry number). Exactly one number per unit is
# flagged is_primary — that's the one transactional automations send from.
# ---------------------------------------------------------------------------

def _create_whatsapp_numbers(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS whatsapp_numbers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            unit_id INTEGER NOT NULL REFERENCES units(id) ON DELETE CASCADE,
            label TEXT NOT NULL,
            phone_number_id TEXT UNIQUE NOT NULL,
            access_token TEXT,
            waba_id TEXT,
            is_primary INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1,
            send_delay_seconds REAL NOT NULL DEFAULT 0.0,
            send_concurrency INTEGER NOT NULL DEFAULT 20,
            meta_app_id TEXT,
            campaign_reserve_percent INTEGER,
            quality_rating TEXT,
            quality_synced_at TEXT,
            onboarded_via TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS one_primary_number_per_unit
        ON whatsapp_numbers(unit_id) WHERE is_primary = 1
        """
    )


# ---------------------------------------------------------------------------
# whatsapp_templates — generic (used by both bulk campaigns and PCO
# automations), so stays in core; PCO automations in integrations/pco/
# reference whatsapp_templates(id) by FK.
# ---------------------------------------------------------------------------

def _create_whatsapp_templates(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS whatsapp_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            unit_id INTEGER NOT NULL REFERENCES units(id) ON DELETE CASCADE,
            template_type TEXT NOT NULL,
            template_name TEXT NOT NULL,
            body_variable_order TEXT NOT NULL,
            button_url_pattern TEXT,
            header_image_url TEXT,
            whatsapp_number_id INTEGER,
            button_variables TEXT,
            active INTEGER DEFAULT 1,
            UNIQUE(unit_id, template_type)
        )
        """
    )


# ---------------------------------------------------------------------------
# users — org-scoped staff, with a separate partial-unique path for
# platform super-admins (context seed §4.6) who span every organisation.
# ---------------------------------------------------------------------------

def _create_users(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            org_id INTEGER REFERENCES organisations(id),
            username TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            is_superadmin INTEGER DEFAULT 0,
            is_org_admin INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            UNIQUE(org_id, username)
        )
        """
    )
    # is_org_admin: scoped to org_id (never set alongside is_superadmin,
    # which has no owning org) — can manage their own org's units/staff
    # and enable/disable already-granted modules, but can't create
    # organisations or grant module entitlements. See
    # storage/modules.py's grant()/enable() split.
    # org_id is nullable: a platform super-admin (context seed §4.6, spans
    # every organisation for onboarding/support/billing) has no single
    # owning org. Regular staff must have org_id set — enforced at the
    # application layer, since SQLite can't express "NOT NULL unless
    # is_superadmin" as a table constraint.
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_users_platform_admin_username
        ON users(username) WHERE org_id IS NULL
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_users_org_id ON users(org_id)")


def _create_user_units(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_units (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            unit_id INTEGER NOT NULL REFERENCES units(id) ON DELETE CASCADE,
            PRIMARY KEY (user_id, unit_id)
        )
        """
    )


# ---------------------------------------------------------------------------
# Bulk WhatsApp campaigns.
# ---------------------------------------------------------------------------

def _create_campaigns(conn) -> None:
    # Campaigns send from a unit's own WhatsApp number/token (shared with
    # the rest of the app) rather than a separate per-tool number table, so
    # there is one place that owns WhatsApp credentials and one set of
    # staff/unit scoping.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            unit_id INTEGER NOT NULL REFERENCES units(id),
            whatsapp_number_id INTEGER REFERENCES whatsapp_numbers(id),
            template_name TEXT NOT NULL,
            language TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            total INTEGER NOT NULL DEFAULT 0,
            sent INTEGER NOT NULL DEFAULT 0,
            failed INTEGER NOT NULL DEFAULT 0,
            scheduled_at TEXT,
            payload_json TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    # campaigns.status is free-form TEXT with no CHECK constraint on
    # purpose — 'running' / 'cancelling' / 'cancelled' / 'throttled' / etc.
    # are application-level values, not enumerated in the schema.


def _create_campaign_recipients(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS campaign_recipients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
            phone TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            detail TEXT,
            updated_at TEXT
        )
        """
    )


def _create_message_log(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS message_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            limit_key TEXT NOT NULL,
            recipient_phone TEXT NOT NULL,
            campaign_id INTEGER REFERENCES campaigns(id),
            sent_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_message_log_key_time ON message_log(limit_key, sent_at)"
    )


def _create_waba_limits(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS waba_limits (
            limit_key TEXT PRIMARY KEY,
            messaging_limit_tier TEXT,
            limit_synced_at TEXT,
            restricted_until TEXT
        )
        """
    )


def _create_login_attempts(conn) -> None:
    # Brute-force login protection, shared by the /login page that fronts
    # both the bulk-campaign UI and (indirectly) the SQLAdmin panel.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS login_attempts (
            identifier TEXT PRIMARY KEY,
            failed_count INTEGER NOT NULL DEFAULT 0,
            last_attempt_at TEXT NOT NULL,
            locked_until TEXT
        )
        """
    )


def _create_send_log(conn) -> None:
    # Append-only history of individual transactional sends (registration
    # poller + form-response webhook, both in integrations/pco/), mirroring
    # what campaign history shows for bulk sends. Deliberately separate
    # from processed_registrations/processed_form_submissions (in
    # integrations/pco/schema.py): those key on registration_id/
    # submission_id and overwrite on retry (they exist to answer "have we
    # handled this ID yet", not "what happened on each attempt") — this
    # table keeps every attempt, including retries. No FK on
    # whatsapp_number_id: purely informational, and a number can be
    # deleted without breaking old log rows. Stays in core (not
    # integrations/pco/) because bulk-campaign sends can write here too via
    # `source`, not just PCO automations.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS send_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sent_at TEXT NOT NULL,
            unit_id INTEGER REFERENCES units(id) ON DELETE CASCADE,
            whatsapp_number_id INTEGER,
            source TEXT NOT NULL,
            recipient_phone TEXT,
            template_name TEXT,
            status TEXT NOT NULL,
            error_code TEXT,
            error_message TEXT,
            reference_id TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_send_log_time ON send_log(sent_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_send_log_unit_time ON send_log(unit_id, sent_at)")


def _create_whatsapp_onboarding_intents(conn) -> None:
    # --- WhatsApp Embedded Signup ---
    # One row per "Connect via WhatsApp" click, written the instant a staff
    # member picks a unit and before they're redirected to Meta — this is
    # what lets the OAuth callback (a separate HTTP request from Meta's
    # side) know which unit to assign the new number to, since Meta's
    # redirect_uri carries only an exchangeable `code`, never our own
    # state. Correlated back via user_id (the callback runs in the
    # same staff member's browser session Meta redirected back to) —
    # consumed_at is set the moment the callback successfully creates a
    # whatsapp_numbers row, so a stale/duplicate callback can't attach a
    # second number to the same intent. created_at also bounds how old an
    # unconsumed intent the callback is willing to honor.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS whatsapp_onboarding_intents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            unit_id INTEGER NOT NULL REFERENCES units(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            consumed_at TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_onboarding_intents_staff ON whatsapp_onboarding_intents(user_id, consumed_at)"
    )

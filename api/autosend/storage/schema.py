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
overhead for tables it will never populate.

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


def _add_column_if_missing(conn, table: str, column: str, ddl: str) -> None:
    """CREATE TABLE IF NOT EXISTS (used everywhere else in this file) only
    covers a table that doesn't exist yet - it's a no-op against a table
    that already exists but predates a newly-added column, which is
    exactly what happened to display_phone_number on an already-deployed
    database. SQLite's ALTER TABLE ADD COLUMN is safe for a nullable
    column (no table rewrite), so this is the narrow exception to the
    "no ALTER TABLE" rule at the top of this file - full rename/recreate
    migration discipline is for drops/renames, not additive nullable
    columns like this one."""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def init_core_schema(conn) -> None:
    _create_organisations(conn)
    _create_organisation_modules(conn)
    _create_units(conn)
    # default_region: ISO 3166-1 alpha-2, used to disambiguate a phone
    # number written in local format (no country code) when normalizing
    # to E.164 - see utils/phone.py. Needed by integrations/email_wa/
    # (booking-confirmation emails carry free-text phone numbers, unlike
    # PCO which returns e164 pre-formatted from its own API), and general
    # enough that any future integration needing the same normalization
    # can reuse it rather than inventing a per-feature column. Existing
    # deployed units predate this column, hence _add_column_if_missing
    # rather than a bare column in _create_units's CREATE TABLE.
    _add_column_if_missing(conn, "units", "default_region", "default_region TEXT NOT NULL DEFAULT 'ZA'")
    # webhook_slug: a random, unguessable per-unit token used to key
    # inbound webhook URLs (currently PCO's people-form webhook - see
    # integrations/webhooks.py). Deliberately separate from the
    # human-readable `slug` column above, which is only unique per
    # organisation (UNIQUE(org_id, slug)) - two orgs each naming a unit
    # "Main" would otherwise collide on a shared /webhooks/.../{slug}
    # path. Left NULL until storage.units.ensure_webhook_slug() mints one
    # lazily, the first time it's actually needed - most units' orgs are
    # never even granted the PCO module, so eagerly generating a webhook
    # path for every unit on every org would just be dead, unusable URLs
    # sitting in the admin UI.
    _add_column_if_missing(conn, "units", "webhook_slug", "webhook_slug TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_units_webhook_slug "
        "ON units(webhook_slug) WHERE webhook_slug IS NOT NULL"
    )
    _create_meta_platform_settings(conn)
    _create_platform_email_settings(conn)
    _create_whatsapp_numbers(conn)
    _add_column_if_missing(conn, "whatsapp_numbers", "display_phone_number", "display_phone_number TEXT")
    # default_region: ISO 3166-1 alpha-2, per-sending-number default used to
    # disambiguate a phone number with no country code when normalizing to
    # E.164 - see utils/phone.py. Scoped to the number rather than the unit
    # because a unit can have numbers sending to different regions.
    _add_column_if_missing(conn, "whatsapp_numbers", "default_region", "default_region TEXT NOT NULL DEFAULT 'ZA'")
    _create_whatsapp_templates(conn)
    _create_users(conn)
    # email: nullable, added after the original table shape (no signup
    # flow collected one until platform billing needed a real address to
    # charge) - additive nullable column via the sanctioned ALTER TABLE
    # exception above, not a rename/recreate.
    _add_column_if_missing(conn, "users", "email", "email TEXT")
    # email_verified_at: nullable timestamp, set once a self-serve signup
    # confirms their address via /signup/verify (see storage/
    # email_verification.py and web/signup_router.py) - NULL for every
    # user created before this column existed and for users added directly
    # by an org-admin/superadmin, neither of which goes through the
    # verification flow at all.
    _add_column_if_missing(conn, "users", "email_verified_at", "email_verified_at TEXT")
    _create_email_verification_tokens(conn)
    _create_user_units(conn)
    _create_campaigns(conn)
    _create_campaign_recipients(conn)
    _create_message_log(conn)
    _create_waba_limits(conn)
    _create_login_attempts(conn)
    _create_send_log(conn)
    _create_whatsapp_onboarding_intents(conn)
    _create_stitch_credentials(conn)
    _create_terms_acceptances(conn)
    _create_kryx_bookings_connections(conn)
    _create_kryx_bookings_automations(conn)


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
    # org's own org-admin users flip it on/off via organisation_modules.
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
# units — a campus/branch, scoped under an organisation.
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
# platform_email_settings — the platform provider's own outbound SMTP
# credentials (currently Mailtrap), used for transactional email (signup
# email verification). Deliberately a platform-wide singleton, same
# reasoning as meta_platform_settings above: this is the platform's own
# mail relay, not a credential any individual organisation owns.
# ---------------------------------------------------------------------------

def _create_platform_email_settings(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS platform_email_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            smtp_host TEXT NOT NULL,
            smtp_port INTEGER NOT NULL,
            smtp_username TEXT,
            smtp_password TEXT,
            from_address TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )


# ---------------------------------------------------------------------------
# whatsapp_numbers — a unit can have more than one WhatsApp number (e.g. a
# main line plus a youth ministry number). There is no "primary"/default
# number: campaigns and automations must each name a specific
# whatsapp_number_id to send from (enforced at the application layer, since
# SQLite has no clean way to require "exactly one FK column is set" here).
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
            active INTEGER DEFAULT 1,
            send_delay_seconds REAL NOT NULL DEFAULT 0.0,
            send_concurrency INTEGER NOT NULL DEFAULT 20,
            meta_app_id TEXT,
            campaign_reserve_percent INTEGER,
            display_phone_number TEXT,
            quality_rating TEXT,
            quality_synced_at TEXT,
            onboarded_via TEXT,
            created_at TEXT NOT NULL
        )
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
# users — org-scoped users, with a separate partial-unique path for
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
            UNIQUE(username)
        )
        """
    )
    # is_org_admin: scoped to org_id (never set alongside is_superadmin,
    # which has no owning org) — can manage their own org's units/users
    # and enable/disable already-granted modules, but can't create
    # organisations or grant module entitlements. See
    # storage/modules.py's grant()/enable() split.
    # org_id is nullable: a platform super-admin (context seed §4.6, spans
    # every organisation for onboarding/support/billing) has no single
    # owning org. Regular users must have org_id set — enforced at the
    # application layer, since SQLite can't express "NOT NULL unless
    # is_superadmin" as a table constraint.
    # username is globally unique, not per-org: authenticate_user() looks
    # up by username alone (one global /login, no org selector), so two
    # orgs sharing a username would make that lookup ambiguous rather than
    # cleanly rejected. The signup/CLI paths already pre-check this via
    # get_user() before insert; the constraint is the DB-level backstop for
    # races and any path that bypasses that check.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_users_org_id ON users(org_id)")


# ---------------------------------------------------------------------------
# email_verification_tokens — proves a self-serve signup's email address is
# real and reachable before the org is allowed to go active (see
# storage.organisations.is_org_email_verified and its callers in
# billing/engine.py and web/signup_router.py's /signup/verify route).
# Deliberately does NOT gate login or payment - is_org_active already has
# its own independent payment-based gate (storage.organisations docstring);
# this is stacked on top of it, not folded into it.
# ---------------------------------------------------------------------------

def _create_email_verification_tokens(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS email_verification_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token TEXT NOT NULL UNIQUE,
            expires_at TEXT NOT NULL,
            used_at TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_email_verification_tokens_user "
        "ON email_verification_tokens(user_id)"
    )


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
    # user/unit scoping.
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


# ---------------------------------------------------------------------------
# stitch_credentials — one row per unit, holding that unit's own Stitch
# Express client_id/client_secret (integrations/stitch.py::StitchClient),
# used to generate real payment links for registration-poller payment
# reminders. One-per-unit rather than one-per-org for the same reason
# whatsapp_numbers is unit-scoped, not org-scoped: units under the same
# org can run genuinely separate finances/bank accounts.
# ---------------------------------------------------------------------------

def _create_stitch_credentials(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stitch_credentials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            unit_id INTEGER NOT NULL UNIQUE REFERENCES units(id) ON DELETE CASCADE,
            client_id TEXT NOT NULL,
            client_secret TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    # active: added after this table's original CREATE TABLE had already
    # shipped, so an already-deployed row's absence of this column can't be
    # covered by IF NOT EXISTS above - same guarded, nullable-column
    # ADD COLUMN exception as whatsapp_numbers.display_phone_number.
    _add_column_if_missing(conn, "stitch_credentials", "active", "active INTEGER NOT NULL DEFAULT 1")


# ---------------------------------------------------------------------------
# kryx_bookings_connections — one row per unit, holding the API key that
# lets that unit's Kryx Bookings connection (a separate, standalone
# booking-engine product, own repo/container - see
# integrations/kryx_bookings.py) trigger a WhatsApp send here on the org's
# behalf. One-per-unit for the same reason stitch_credentials/
# whatsapp_numbers are unit-scoped, not org-scoped: a multi-campus org can
# want a different booking-engine instance (and number) per unit.
#
# api_key_hash stores a SHA-256 hex digest, not an encrypted/reversible
# token - the plaintext key is only ever needed at generation time (shown
# once to the org admin) and at verification time (compared by re-hashing
# the caller's header), so there is no legitimate need to ever decrypt it
# back, unlike whatsapp_numbers.access_token/stitch_credentials.client_secret.
# api_key_prefix (the key's first 12 characters, stored in the clear) is
# purely cosmetic - lets the settings page show "kxb_a1B2c3..." so an admin
# can tell which key is configured without ever re-displaying the full
# secret.
#
# The actual template/variable/number mapping for a connection is *not*
# stored here - it reuses the existing whatsapp_templates table under the
# synthetic template_type "kryx_bookings" (unit_id, template_type) UNIQUE,
# same convention as every other automation in this codebase (see
# storage/kryx_bookings.py).
# ---------------------------------------------------------------------------

def _create_kryx_bookings_connections(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kryx_bookings_connections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            unit_id INTEGER NOT NULL UNIQUE REFERENCES units(id) ON DELETE CASCADE,
            api_key_hash TEXT NOT NULL UNIQUE,
            api_key_prefix TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            last_used_at TEXT
        )
        """
    )


def _create_kryx_bookings_automations(conn) -> None:
    """Maps a (unit, booking status) to one or more WhatsApp sends -
    deliberately a separate table rather than relying on
    whatsapp_templates' own UNIQUE(unit_id, template_type) constraint the
    way most other integrations do (a single synthetic template_type per
    (unit, status) pair), because a single booking-status event can now
    fan out to more than one automation - e.g. "pending" notifying both
    the person who made the request (recipient_mode='requester', sent to
    whatever phone number the booking payload itself carries) and a fixed
    approver number (recipient_mode='fixed', recipient_phone set here) -
    same "small join table pointing at a whatsapp_templates row" pattern
    as form_templates (storage/units.py) uses for PCO form mappings, just
    with no natural external key to upsert against, so callers create/
    update/delete these by their own id instead of an implicit upsert.

    Each row here owns exactly one whatsapp_templates row via
    whatsapp_template_id (that row's template_type is an opaque,
    randomly-suffixed "kryx_bookings:<status>:<hex>" value used only to
    satisfy that table's own uniqueness constraint - never queried by
    value, unlike the "form:<pco_form_id>" convention it otherwise
    mirrors) - ON DELETE CASCADE here only removes the mapping row;
    storage/kryx_bookings.py explicitly deletes the owned whatsapp_templates
    row too so an automation's template config doesn't leak as an orphan."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kryx_bookings_automations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            unit_id INTEGER NOT NULL REFERENCES units(id) ON DELETE CASCADE,
            status TEXT NOT NULL,
            recipient_mode TEXT NOT NULL DEFAULT 'requester',
            recipient_phone TEXT,
            whatsapp_template_id INTEGER NOT NULL REFERENCES whatsapp_templates(id) ON DELETE CASCADE,
            active INTEGER NOT NULL DEFAULT 1
        )
        """
    )


def _create_whatsapp_onboarding_intents(conn) -> None:
    # --- WhatsApp Embedded Signup ---
    # One row per "Connect via WhatsApp" click, written the instant a user
    # picks a unit and before they're redirected to Meta — this is
    # what lets the OAuth callback (a separate HTTP request from Meta's
    # side) know which unit to assign the new number to, since Meta's
    # redirect_uri carries only an exchangeable `code`, never our own
    # state. Correlated back via user_id (the callback runs in the
    # same user's browser session Meta redirected back to) —
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


def _create_terms_acceptances(conn) -> None:
    # Append-only audit trail of Terms & Conditions acceptance, written once
    # at self-serve signup (web/signup_router.py) when the new org-admin
    # ticks the required checkbox. org_id is stored directly (rather than
    # resolved via a join) because this is a point-in-time compliance
    # record, not a live-scoped resource — the acceptance must remain
    # attributable to the org and user as they existed at the moment of
    # acceptance, so nothing here is ever updated or deleted. terms_version
    # lets a later re-acceptance (e.g. after a material Terms change) be
    # told apart from the original signup acceptance, even though there is
    # no re-acceptance flow yet.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS terms_acceptances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            org_id INTEGER NOT NULL REFERENCES organisations(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            terms_version TEXT NOT NULL,
            accepted_at TEXT NOT NULL,
            ip_address TEXT,
            user_agent TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_terms_acceptances_org ON terms_acceptances(org_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_terms_acceptances_user ON terms_acceptances(user_id)"
    )

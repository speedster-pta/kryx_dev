"""
Units, their WhatsApp numbers, message templates, and PCO form
mappings.
"""

import json
import secrets

from ._db import _connect

REGISTRATION_TEMPLATE_TYPES = ("free_acknowledgment", "payment_reminder")


def generate_webhook_slug() -> str:
    """Random, unguessable per-unit token for inbound webhook URLs (see
    get_unit_by_webhook_slug). Unlike the human-readable `slug` column,
    this is only ever unique per-row, never derived from a name, so it
    can't collide across organisations and never needs to change when a
    unit is renamed."""
    return secrets.token_urlsafe(24)


def ensure_webhook_slug(unit_id: int) -> str | None:
    """Lazily generates and persists a webhook_slug for this unit the
    first time it's actually needed, rather than handing every unit a
    live webhook path the moment it's created - most units never get PCO
    wired up at all, and generating one unconditionally would mean
    superadmins/org admins are always looking at a "real" webhook URL for
    organisations that were never even sold the PCO module.

    Returns None (and mints nothing) unless the unit's organisation has
    been granted the PCO module (storage.modules.is_granted) - grant
    rather than the org-admin's enable/disable toggle, since revoking the
    toggle later shouldn't invalidate a webhook URL someone may already
    have registered with PCO; it just means it's dormant.

    Idempotent: once a slug exists it's returned as-is regardless of
    current grant status."""
    from .modules import is_granted, MODULE_PCO

    with _connect() as conn:
        row = conn.execute(
            "SELECT org_id, webhook_slug FROM units WHERE id = ?", (unit_id,)
        ).fetchone()
        if row is None:
            return None
        org_id, webhook_slug = row
        if webhook_slug:
            return webhook_slug
        if not is_granted(org_id, MODULE_PCO, conn):
            return None
        webhook_slug = generate_webhook_slug()
        conn.execute("UPDATE units SET webhook_slug = ? WHERE id = ?", (webhook_slug, unit_id))
        return webhook_slug


def _row_to_unit_dict(conn, row) -> dict:
    """Unit rows no longer carry WhatsApp fields directly (moved to
    whatsapp_numbers) - a unit's numbers are looked up explicitly via
    get_whatsapp_numbers()/get_whatsapp_number_by_id() wherever a send
    needs one, never implicitly off the unit dict."""
    from autosend import crypto

    columns = [d[0] for d in conn.execute("SELECT * FROM units LIMIT 0").description]
    unit = dict(zip(columns, row))

    if unit.get("pco_webhook_secret"):
        unit["pco_webhook_secret"] = crypto.decrypt_token(unit["pco_webhook_secret"])
    return unit


def get_unit_by_phone_id(phone_number_id: str) -> dict | None:
    """Looks up by a specific number's phone_number_id (e.g. from an
    inbound webhook) and returns that number's owning unit."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT c.* FROM units c
            JOIN whatsapp_numbers n ON n.unit_id = c.id
            WHERE n.phone_number_id = ? AND n.active = 1 AND c.active = 1
            """,
            (phone_number_id,),
        ).fetchone()
        if not row:
            return None
        return _row_to_unit_dict(conn, row)


def get_unit_by_slug(slug: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM units WHERE slug = ?", (slug,)
        ).fetchone()
        if not row:
            return None
        return _row_to_unit_dict(conn, row)


def get_unit_by_webhook_slug(webhook_slug: str) -> dict | None:
    """Looks up a unit by its random webhook_slug, used to key inbound
    webhook URLs. Unlike get_unit_by_slug, this is globally unique (see
    idx_units_webhook_slug in schema.py) - no org disambiguation needed."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM units WHERE webhook_slug = ?", (webhook_slug,)
        ).fetchone()
        if not row:
            return None
        return _row_to_unit_dict(conn, row)


def create_unit_webhook_secret(unit_id: int, secret: str, label: str | None = None) -> int:
    """Adds an additional valid PCO webhook Authenticity Secret for a
    unit, alongside its primary units.pco_webhook_secret - see
    schema.py's unit_webhook_secrets table docstring for why a unit can
    need more than one (several PCO webhook subscriptions, each created
    by a different PCO user, all pointed at the same unit URL)."""
    from autosend import crypto
    from datetime import datetime, timezone

    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO unit_webhook_secrets (unit_id, label, secret, created_at) VALUES (?, ?, ?, ?)",
            (unit_id, label, crypto.encrypt_token(secret), datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return cur.lastrowid


def list_unit_webhook_secrets(unit_id: int) -> list[dict]:
    """For the PCO Settings page's management list - id/label/created_at
    only, deliberately never the decrypted secret itself (nothing in the
    UI needs to display it again once saved, same as
    pco_token_secret/pco_webhook_secret elsewhere)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, label, created_at FROM unit_webhook_secrets WHERE unit_id = ? ORDER BY created_at",
            (unit_id,),
        ).fetchall()
        return [{"id": r[0], "label": r[1], "created_at": r[2]} for r in rows]


def get_unit_webhook_secrets_decrypted(unit_id: int) -> list[str]:
    """Decrypted secrets only - used exclusively by
    integrations/webhooks.py's signature verification, never returned
    from any HTTP response."""
    from autosend import crypto

    with _connect() as conn:
        rows = conn.execute(
            "SELECT secret FROM unit_webhook_secrets WHERE unit_id = ?", (unit_id,)
        ).fetchall()
        return [crypto.decrypt_token(r[0]) for r in rows]


def delete_unit_webhook_secret(unit_id: int, secret_id: int) -> bool:
    """Scoped by unit_id as well as secret_id, same "never trust a bare
    pk from the client" rule as every other org/unit-scoped delete in
    this codebase - a guessed secret_id belonging to a different unit
    (and therefore potentially a different org) can't be deleted this
    way."""
    with _connect() as conn:
        conn.execute(
            "DELETE FROM unit_webhook_secrets WHERE id = ? AND unit_id = ?", (secret_id, unit_id),
        )
        conn.commit()
        return conn.total_changes > 0


def get_active_units() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM units WHERE active = 1").fetchall()
        return [_row_to_unit_dict(conn, r) for r in rows]


def get_unit_ids_for_org(org_id: int) -> list[int]:
    """Every unit id (active or not) belonging to org_id — resolved fresh
    on every call rather than cached, so org-admin users (whose effective
    scope is "every unit in my org") see a unit the moment it's created,
    without needing to re-login. See web/auth.py::resolve_unit_ids()."""
    with _connect() as conn:
        rows = conn.execute("SELECT id FROM units WHERE org_id = ?", (org_id,)).fetchall()
        return [r[0] for r in rows]


def count_units_for_org(org_id: int) -> int:
    """Convenience count for billing/entitlements.py's unit-limit check -
    equivalent to len(get_unit_ids_for_org(org_id)) but doesn't materialize
    the id list, and counts active-or-not the same way that function
    does."""
    with _connect() as conn:
        row = conn.execute("SELECT COUNT(*) FROM units WHERE org_id = ?", (org_id,)).fetchone()
        return row[0] if row else 0


def count_whatsapp_numbers_for_org(org_id: int) -> int:
    """Every WhatsApp number (active or not) belonging to org_id, joined
    through units per this project's documented column-scoping convention
    (whatsapp_numbers has no direct org_id, only unit_id). Used by
    billing/entitlements.py's number-limit check."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM whatsapp_numbers n "
            "JOIN units u ON u.id = n.unit_id WHERE u.org_id = ?",
            (org_id,),
        ).fetchone()
        return row[0] if row else 0


# ---- WhatsApp numbers ----

def get_whatsapp_numbers(unit_ids: list[int] | None) -> list[dict]:
    """unit_ids=None means unrestricted (superadmin)."""
    from autosend import crypto
    from .scoping import unit_scope_clause

    with _connect() as conn:
        base = """
            SELECT n.id, n.unit_id, u.name AS unit_name, u.org_id, n.label,
                   n.phone_number_id, n.access_token, n.waba_id, n.meta_app_id, n.active,
                   n.send_delay_seconds, n.send_concurrency, n.campaign_reserve_percent,
                   n.display_phone_number, n.quality_rating, n.quality_synced_at, n.default_region
            FROM whatsapp_numbers n
            JOIN units u ON u.id = n.unit_id
            WHERE n.active = 1 AND u.active = 1
        """
        scope = unit_scope_clause("n.unit_id", unit_ids, joiner="AND")
        if scope is None:
            return []
        clause, params = scope
        rows = conn.execute(base + clause + " ORDER BY u.name, n.label", params).fetchall()

        columns = ["id", "unit_id", "unit_name", "org_id", "label",
                   "phone_number_id", "access_token", "waba_id", "meta_app_id", "active",
                   "send_delay_seconds", "send_concurrency", "campaign_reserve_percent",
                   "display_phone_number", "quality_rating", "quality_synced_at", "default_region"]
        numbers = [dict(zip(columns, r)) for r in rows]
        for n in numbers:
            n["access_token"] = crypto.decrypt_token(n["access_token"])
        return numbers


def get_whatsapp_number_by_id(number_id: int) -> dict | None:
    numbers = [n for n in get_whatsapp_numbers(None) if n["id"] == number_id]
    return numbers[0] if numbers else None


def create_whatsapp_number(
    unit_id: int, label: str, phone_number_id: str, access_token: str,
    waba_id: str, onboarded_via: str, display_phone_number: str | None = None,
) -> int:
    """Used by the Embedded Signup callback (onboarding_router.py) -
    manual creation still goes through WhatsAppNumberAdmin's own
    insert_model (ORM, admin_models.py), same as before this feature.
    This is the raw-sqlite counterpart for the automated path, mirroring
    what SQLAdmin's insert_model does: encrypt access_token before
    writing (EncryptedString does this transparently on the ORM side;
    here it has to be explicit), set created_at."""
    from datetime import datetime, timezone

    from autosend import crypto

    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO whatsapp_numbers
                (unit_id, label, phone_number_id, access_token, waba_id,
                 onboarded_via, display_phone_number, active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                unit_id, label, phone_number_id, crypto.encrypt_token(access_token),
                waba_id, onboarded_via, display_phone_number,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        return cur.lastrowid


# ---- Stitch credentials ----
# Read-only from storage's side (the write path is entirely SQLAdmin's own
# ORM insert_model/update_model on StitchCredentials - see admin_views.py) -
# this is just what clients.get_stitch_client() calls to build a
# StitchClient for a given unit.

def get_stitch_credentials(unit_id: int) -> dict | None:
    from autosend import crypto

    with _connect() as conn:
        row = conn.execute(
            "SELECT client_id, client_secret, active FROM stitch_credentials WHERE unit_id = ?",
            (unit_id,),
        ).fetchone()
    if not row:
        return None
    return {"client_id": row[0], "client_secret": crypto.decrypt_token(row[1]), "active": bool(row[2])}


def is_stitch_active(unit_id: int) -> bool:
    """True only if this unit has a Stitch Credentials row AND its active
    checkbox is on - what gates both generating a real payment link
    (clients.get_stitch_client, called from registration_poller.py) and
    offering the "Stitch Suffix" variable in the Automations UI
    (/api/automations/units)."""
    creds = get_stitch_credentials(unit_id)
    return bool(creds and creds["active"])


# ---- WhatsApp Embedded Signup: pending onboarding intents ----
# See schema.py's whatsapp_onboarding_intents table comment for why this
# exists: correlates Meta's OAuth callback (which carries only an
# exchangeable `code`, no state we control) back to the unit a
# user picked before being redirected away to Meta.

def create_onboarding_intent(user_id: int, unit_id: int) -> int:
    from datetime import datetime, timezone

    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO whatsapp_onboarding_intents (user_id, unit_id, created_at) VALUES (?, ?, ?)",
            (user_id, unit_id, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return cur.lastrowid


def consume_latest_onboarding_intent(user_id: int, max_age_minutes: int) -> dict | None:
    """Finds this user's most recent unconsumed intent (bounded by
    max_age_minutes, so an abandoned flow from days ago can't be
    resurrected by a stray callback) and marks it consumed in the same
    transaction, so a duplicate/retried callback can't attach a second
    number to it. Returns None if there's nothing valid to consume - the
    callback treats that as "I don't know which unit this belongs
    to" and fails loudly rather than guessing."""
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)).isoformat()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT id, unit_id FROM whatsapp_onboarding_intents
            WHERE user_id = ? AND consumed_at IS NULL AND created_at >= ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (user_id, cutoff),
        ).fetchone()
        if not row:
            return None
        intent_id, unit_id = row
        conn.execute(
            "UPDATE whatsapp_onboarding_intents SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
            (datetime.now(timezone.utc).isoformat(), intent_id),
        )
        conn.commit()
        # conn.total_changes here would count the UPDATE only if it
        # actually matched a still-unconsumed row - guards the (very
        # unlikely) race of two callbacks for the same user_id
        # landing at the same instant.
        if conn.total_changes == 0:
            return None
        return {"id": intent_id, "unit_id": unit_id}


def get_meta_platform_settings() -> dict | None:
    """Org-wide Meta app credentials (see schema.py's meta_platform_settings
    table and admin_views.MetaPlatformSettingsAdmin, the singleton
    settings page users use to set this). Returns None if not configured
    yet - onboarding_router.py surfaces that as a clear error rather than
    a confusing downstream Graph API failure."""
    from autosend import crypto

    with _connect() as conn:
        row = conn.execute(
            "SELECT app_id, app_secret, config_id, webhook_verify_token FROM meta_platform_settings LIMIT 1"
        ).fetchone()
        if not row:
            return None
        return {
            "app_id": row[0],
            "app_secret": crypto.decrypt_token(row[1]) if row[1] else None,
            "config_id": row[2],
            "webhook_verify_token": crypto.decrypt_token(row[3]) if row[3] else None,
        }


# ---- Planning Center OAuth: platform app credentials + per-org tokens ----
# See schema.py's pco_platform_settings/pco_oauth_states tables and
# web/pco_oauth_router.py, the "Connect via Planning Center" flow these
# support alongside the existing PAT-based PCOOrganizationSettings path.

def get_pco_platform_settings() -> dict | None:
    """Kryx's own PCO OAuth app credentials (see schema.py's
    pco_platform_settings table and admin_views.PcoPlatformSettingsAdmin,
    the singleton settings page a superadmin uses to set this). Returns
    None if not configured yet - pco_oauth_router.py surfaces that as a
    clear error rather than a confusing downstream PCO API failure."""
    from autosend import crypto

    with _connect() as conn:
        row = conn.execute(
            "SELECT client_id, client_secret FROM pco_platform_settings LIMIT 1"
        ).fetchone()
        if not row:
            return None
        return {
            "client_id": row[0],
            "client_secret": crypto.decrypt_token(row[1]) if row[1] else None,
        }


def create_pco_oauth_state(org_id: int, user_id: int) -> str:
    """Written the instant a user clicks "Connect via Planning
    Center", before the redirect to PCO - the random state token this
    returns is what the OAuth callback receives back from PCO to know
    which org/user this connection belongs to (see schema.py's
    pco_oauth_states table docstring for why this differs from the
    Meta Embedded Signup intent pattern)."""
    from datetime import datetime, timezone

    state = secrets.token_urlsafe(32)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO pco_oauth_states (org_id, user_id, state, created_at) VALUES (?, ?, ?, ?)",
            (org_id, user_id, state, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    return state


def consume_pco_oauth_state(state: str, max_age_minutes: int) -> dict | None:
    """Looks up the org/user a state token was issued for (bounded by
    max_age_minutes, so an abandoned flow from days ago can't be
    resurrected by a stray callback) and marks it consumed in the same
    transaction, so a duplicate/retried callback can't reuse it. Returns
    None if there's nothing valid to consume - the callback treats that
    as "I don't recognize this connection attempt" and fails loudly
    rather than guessing an org."""
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)).isoformat()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT id, org_id, user_id FROM pco_oauth_states
            WHERE state = ? AND consumed_at IS NULL AND created_at >= ?
            """,
            (state, cutoff),
        ).fetchone()
        if not row:
            return None
        state_id, org_id, user_id = row
        conn.execute(
            "UPDATE pco_oauth_states SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
            (datetime.now(timezone.utc).isoformat(), state_id),
        )
        conn.commit()
        # Same race guard as consume_latest_onboarding_intent above.
        if conn.total_changes == 0:
            return None
        return {"org_id": org_id, "user_id": user_id}


def get_pco_org_settings(org_id: int) -> dict | None:
    """Full pco_organization_settings row for an org, decrypted - used by
    clients.py to decide whether to build a PAT or OAuth
    PlanningCenterClient and, for OAuth, whether the access token needs
    refreshing first."""
    from autosend import crypto

    with _connect() as conn:
        row = conn.execute(
            """
            SELECT pco_token_id, pco_token_secret, pco_auth_method,
                   pco_access_token, pco_refresh_token, pco_token_expires_at
            FROM pco_organization_settings WHERE org_id = ?
            """,
            (org_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "pco_token_id": row[0],
            "pco_token_secret": crypto.decrypt_token(row[1]) if row[1] else None,
            "pco_auth_method": row[2],
            "pco_access_token": crypto.decrypt_token(row[3]) if row[3] else None,
            "pco_refresh_token": crypto.decrypt_token(row[4]) if row[4] else None,
            "pco_token_expires_at": row[5],
        }


def save_pco_oauth_tokens(
    org_id: int, access_token: str, refresh_token: str, expires_at: str
) -> None:
    """Stores/updates the OAuth token set on an org's PCO settings row and
    flips pco_auth_method to 'oauth' - called from the initial OAuth
    callback (row may not exist yet, hence the upsert) and from the
    refresh path in clients.py (row always exists there). pco_token_id
    is set to a fixed, non-empty placeholder to satisfy that column's
    NOT NULL constraint on a fresh insert; it is never read once
    pco_auth_method='oauth' (see storage.get_pco_org_settings /
    clients.py)."""
    from autosend import crypto
    from datetime import datetime, timezone

    encrypted_access = crypto.encrypt_token(access_token)
    encrypted_refresh = crypto.encrypt_token(refresh_token)
    with _connect() as conn:
        existing = conn.execute(
            "SELECT id FROM pco_organization_settings WHERE org_id = ?", (org_id,)
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE pco_organization_settings
                SET pco_auth_method = 'oauth', pco_access_token = ?,
                    pco_refresh_token = ?, pco_token_expires_at = ?
                WHERE org_id = ?
                """,
                (encrypted_access, encrypted_refresh, expires_at, org_id),
            )
        else:
            conn.execute(
                """
                INSERT INTO pco_organization_settings
                    (org_id, pco_token_id, pco_auth_method, pco_access_token,
                     pco_refresh_token, pco_token_expires_at, created_at)
                VALUES (?, 'oauth', 'oauth', ?, ?, ?, ?)
                """,
                (org_id, encrypted_access, encrypted_refresh, expires_at,
                 datetime.now(timezone.utc).isoformat()),
            )
        conn.commit()


def disconnect_pco_oauth(org_id: int) -> None:
    """Reverses save_pco_oauth_tokens - called from the "Disconnect"
    button on the PCO Settings page. Only clears Kryx's own copy of the
    token pair; it does NOT call Planning Center's own OAuth revocation
    endpoint (unverified contract, and the org's PCO "Connected Apps"
    list is the authoritative place to actually revoke the grant if
    they want to - clearing our side is enough to stop Kryx from calling
    the PCO API on their behalf, which is the part actually in scope
    here).

    If pco_token_id is still the literal 'oauth' placeholder (meaning
    this org connected via OAuth only and never had a real PAT
    underneath - see save_pco_oauth_tokens), the whole row is deleted,
    reverting to "not connected at all", the same state as before ever
    connecting. If a real PAT is still sitting in pco_token_id/
    pco_token_secret (the org had one configured before adding OAuth on
    top of it), auth_method just falls back to 'pat' and that PAT keeps
    working - existing PCO webhook subscriptions are untouched either
    way, since they're PCO objects independent of whichever credential
    created them."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT pco_token_id, pco_token_secret FROM pco_organization_settings WHERE org_id = ?",
            (org_id,),
        ).fetchone()
        if row is None:
            return
        token_id, token_secret = row
        if token_id == "oauth" and token_secret is None:
            conn.execute("DELETE FROM pco_organization_settings WHERE org_id = ?", (org_id,))
        else:
            conn.execute(
                """
                UPDATE pco_organization_settings
                SET pco_auth_method = 'pat', pco_access_token = NULL,
                    pco_refresh_token = NULL, pco_token_expires_at = NULL
                WHERE org_id = ?
                """,
                (org_id,),
            )
        conn.commit()


def sync_pco_subdomain(org_id: int, subdomain: str) -> None:
    """Fills in (or refreshes) the Church Center subdomain from the
    connected PCO account's own church_center_subdomain attribute -
    called after every OAuth connect/reconnect and after every PAT save
    (see PlanningCenterClient.get_organization_info and
    web/pco_oauth_router.py / admin_org_pages.PcoSettingsView.save_token).
    Deliberately always overwrites rather than only filling a blank value
    - this is PCO's own authoritative answer for "what is this account's
    subdomain", not something an org is expected to hand-maintain a
    divergent value for any more (the manual pco_subdomain form field is
    gone for exactly this reason)."""
    with _connect() as conn:
        conn.execute(
            "UPDATE pco_organization_settings SET pco_subdomain = ? WHERE org_id = ?",
            (subdomain, org_id),
        )
        conn.commit()


def update_whatsapp_number_display_number(number_id: int, display_phone_number: str) -> None:
    """Backfills the human-readable MSISDN onto a number that predates
    display_phone_number being captured at onboarding time (or was added
    manually) - see whatsapp_limits.sync_display_number_from_meta and
    POST /ops/sync-phone-numbers, which calls this once per number missing
    the value. Unlike quality_rating this never goes stale (a WhatsApp
    number's own MSISDN doesn't change), so there's no re-sync cadence."""
    with _connect() as conn:
        conn.execute(
            "UPDATE whatsapp_numbers SET display_phone_number = ? WHERE id = ?",
            (display_phone_number, number_id),
        )
        conn.commit()


def update_whatsapp_number_quality(number_id: int, quality_rating: str, synced_at: str) -> None:
    """Caches this number's Meta quality_rating (GREEN/YELLOW/RED) - see
    whatsapp_limits.quality_summary(), which calls this at most once per
    UTC calendar day per number, same "sync on first use" pattern as
    upsert_waba_limit_tier() in limits.py."""
    with _connect() as conn:
        conn.execute(
            "UPDATE whatsapp_numbers SET quality_rating = ?, quality_synced_at = ? WHERE id = ?",
            (quality_rating, synced_at, number_id),
        )
        conn.commit()


def get_template(unit_id: int, template_type: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT template_name, body_variable_order, button_variables, header_image_url, whatsapp_number_id
            FROM whatsapp_templates
            WHERE unit_id = ? AND template_type = ? AND active = 1
            """,
            (unit_id, template_type),
        ).fetchone()
        if not row:
            return None
        return {
            "template_name": row[0],
            "body_variable_order": json.loads(row[1]),
            "button_variables": json.loads(row[2]) if row[2] else [],
            "header_image_url": row[3],
            "whatsapp_number_id": row[4],
        }

def get_form_whatsapp_template_id(unit_id: int, pco_form_id: str) -> int | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT whatsapp_template_id FROM form_templates WHERE unit_id = ? AND pco_form_id = ? AND active = 1",
            (unit_id, pco_form_id),
        ).fetchone()
        return row[0] if row else None


def get_template_by_id(template_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT template_name, body_variable_order, button_variables, header_image_url, whatsapp_number_id FROM whatsapp_templates WHERE id = ? AND active = 1",
            (template_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "template_name": row[0],
            "body_variable_order": json.loads(row[1]),
            "button_variables": json.loads(row[2]) if row[2] else [],
            "header_image_url": row[3],
            "whatsapp_number_id": row[4],
        }

# ---- Automations page (Free/Paid Registration templates + Form Mappings) ----


def _row_to_registration_template(columns: list[str], row) -> dict:
    d = dict(zip(columns, row))
    d["body_variable_order"] = json.loads(d["body_variable_order"]) if d["body_variable_order"] else []
    d["button_variables"] = json.loads(d["button_variables"]) if d["button_variables"] else []
    return d


def list_registration_templates(unit_ids: list[int] | None, template_type: str) -> list[dict]:
    """unit_ids=None means unrestricted (superadmin)."""
    from .scoping import unit_scope_clause

    with _connect() as conn:
        base = """
            SELECT t.id, t.unit_id, u.name AS unit_name, t.template_type,
                   t.template_name, t.body_variable_order, t.button_variables,
                   t.header_image_url, t.whatsapp_number_id, n.label AS number_label, t.active
            FROM whatsapp_templates t
            JOIN units u ON u.id = t.unit_id
            LEFT JOIN whatsapp_numbers n ON n.id = t.whatsapp_number_id
            WHERE t.template_type = ?
        """
        scope = unit_scope_clause("t.unit_id", unit_ids, joiner="AND")
        if scope is None:
            return []
        clause, scope_params = scope
        params = [template_type, *scope_params]
        rows = conn.execute(base + clause + " ORDER BY u.name", params).fetchall()
        columns = ["id", "unit_id", "unit_name", "template_type", "template_name",
                   "body_variable_order", "button_variables", "header_image_url",
                   "whatsapp_number_id", "number_label", "active"]
        return [_row_to_registration_template(columns, r) for r in rows]


def upsert_registration_template(
    unit_id: int, template_type: str, template_name: str,
    body_variable_order: list[str], whatsapp_number_id: int | None,
    button_variables: list[str], header_image_url: str | None, active: bool,
) -> int:
    if template_type not in REGISTRATION_TEMPLATE_TYPES:
        raise ValueError(f"template_type must be one of {REGISTRATION_TEMPLATE_TYPES}")
    return _upsert_whatsapp_template_row(
        unit_id, template_type, template_name, body_variable_order,
        whatsapp_number_id, button_variables, header_image_url, active,
    )


def list_form_mappings(unit_ids: list[int] | None) -> list[dict]:
    """unit_ids=None means unrestricted (superadmin)."""
    from .scoping import unit_scope_clause

    with _connect() as conn:
        base = """
            SELECT f.id, f.unit_id, u.name AS unit_name, f.pco_form_id, f.active,
                   t.id AS whatsapp_template_id, t.template_name, t.body_variable_order,
                   t.button_variables, t.header_image_url, t.whatsapp_number_id, n.label AS number_label
            FROM form_templates f
            JOIN units u ON u.id = f.unit_id
            JOIN whatsapp_templates t ON t.id = f.whatsapp_template_id
            LEFT JOIN whatsapp_numbers n ON n.id = t.whatsapp_number_id
        """
        scope = unit_scope_clause("f.unit_id", unit_ids, joiner="WHERE")
        if scope is None:
            return []
        clause, params = scope
        rows = conn.execute(base + clause + " ORDER BY u.name, f.pco_form_id", params).fetchall()
        columns = ["id", "unit_id", "unit_name", "pco_form_id", "active",
                   "whatsapp_template_id", "template_name", "body_variable_order",
                   "button_variables", "header_image_url", "whatsapp_number_id", "number_label"]
        results = []
        for r in rows:
            d = dict(zip(columns, r))
            d["body_variable_order"] = json.loads(d["body_variable_order"]) if d["body_variable_order"] else []
            d["button_variables"] = json.loads(d["button_variables"]) if d["button_variables"] else []
            results.append(d)
        return results


def upsert_form_mapping(
    mapping_id: int | None, unit_id: int, pco_form_id: str, template_name: str,
    body_variable_order: list[str], whatsapp_number_id: int | None, active: bool,
    button_variables: list[str] | None = None, header_image_url: str | None = None,
) -> int:
    """Each form mapping owns its own whatsapp_templates row under a synthetic,
    per-form template_type ("form:<pco_form_id>") so multiple form mappings
    can coexist per unit despite whatsapp_templates' UNIQUE(unit_id,
    template_type) constraint - mirrors how this used to require users to invent
    a unique "custom" type by hand; this just does it for them."""
    template_type = f"form:{pco_form_id}"
    whatsapp_template_id = _upsert_whatsapp_template_row(
        unit_id, template_type, template_name, body_variable_order,
        whatsapp_number_id, button_variables or [], header_image_url, active,
    )
    with _connect() as conn:
        if mapping_id is not None:
            conn.execute(
                """
                UPDATE form_templates
                SET unit_id = ?, pco_form_id = ?, whatsapp_template_id = ?, active = ?
                WHERE id = ?
                """,
                (unit_id, pco_form_id, whatsapp_template_id, int(active), mapping_id),
            )
            conn.commit()
            return mapping_id
        conn.execute(
            """
            INSERT INTO form_templates (unit_id, pco_form_id, whatsapp_template_id, active)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(unit_id, pco_form_id) DO UPDATE SET
                whatsapp_template_id = excluded.whatsapp_template_id,
                active = excluded.active
            """,
            (unit_id, pco_form_id, whatsapp_template_id, int(active)),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM form_templates WHERE unit_id = ? AND pco_form_id = ?",
            (unit_id, pco_form_id),
        ).fetchone()
        return row[0]


def _upsert_whatsapp_template_row(
    unit_id: int, template_type: str, template_name: str,
    body_variable_order: list[str], whatsapp_number_id: int | None,
    button_variables: list[str], header_image_url: str | None, active: bool,
) -> int:
    """Single choke point for all three Automations sections (free/paid
    registration templates, form mappings, serving rules all call through
    here) - which makes it the one place that needs to know about
    replacing a header image, rather than duplicating that logic three
    times. If this unit/template_type already had a *different*
    header_image_url (or one at all, and it's being cleared), the old
    file on disk is deleted after the new value is safely committed -
    otherwise every re-save of an automation with an unchanged image
    would silently leak another orphaned file under header_images/."""
    from .header_images import delete_header_image_file

    with _connect() as conn:
        existing = conn.execute(
            "SELECT header_image_url FROM whatsapp_templates WHERE unit_id = ? AND template_type = ?",
            (unit_id, template_type),
        ).fetchone()
        old_header_image_url = existing[0] if existing else None

        conn.execute(
            """
            INSERT INTO whatsapp_templates
                (unit_id, template_type, template_name, body_variable_order,
                 button_variables, header_image_url, whatsapp_number_id, active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(unit_id, template_type) DO UPDATE SET
                template_name = excluded.template_name,
                body_variable_order = excluded.body_variable_order,
                button_variables = excluded.button_variables,
                header_image_url = excluded.header_image_url,
                whatsapp_number_id = excluded.whatsapp_number_id,
                active = excluded.active
            """,
            (unit_id, template_type, template_name, json.dumps(body_variable_order),
             json.dumps(button_variables or []), header_image_url, whatsapp_number_id, int(active)),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM whatsapp_templates WHERE unit_id = ? AND template_type = ?",
            (unit_id, template_type),
        ).fetchone()

    # Outside the connection block and after commit: the DB write is what
    # matters and must never be rolled back because a file happened to be
    # locked/missing/unwritable.
    if old_header_image_url and old_header_image_url != header_image_url:
        delete_header_image_file(old_header_image_url)

    return row[0]


def delete_form_mapping(mapping_id: int) -> None:
    from .header_images import delete_header_image_file

    with _connect() as conn:
        row = conn.execute(
            """
            SELECT t.id, t.header_image_url
            FROM form_templates f
            JOIN whatsapp_templates t ON t.id = f.whatsapp_template_id
            WHERE f.id = ?
            """,
            (mapping_id,),
        ).fetchone()
        conn.execute("DELETE FROM form_templates WHERE id = ?", (mapping_id,))
        if row:
            # the whatsapp_templates row is 1:1 with this mapping (synthetic
            # per-form template_type), so nothing else can reference it
            conn.execute("DELETE FROM whatsapp_templates WHERE id = ?", (row[0],))
        conn.commit()

    if row and row[1]:
        delete_header_image_file(row[1])

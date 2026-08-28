"""
storage/kryx_bookings.py

CRUD for the Kryx Bookings integration: per-unit API keys
(kryx_bookings_connections) plus one or more "automations" per (unit,
booking status) - each an independent WhatsApp send configuration, held in
kryx_bookings_automations (schema.py), which points at its own row in the
shared whatsapp_templates table under a synthetic template_type
"kryx_bookings:<status>:<random suffix>" - same convention as
storage/units.py's form mappings (`f"form:{pco_form_id}"`), except the
random suffix (rather than a natural external key) is what keeps each
automation's whatsapp_templates row unique, since unlike a PCO form id
there's nothing external to key on: an org can freely define more than one
automation for the same (unit, status).

BOOKING_STATUSES mirrors the actual BookingStatus enum in the Kryx
Bookings engine itself (~/bookings/api/app/models.py) - "pending" is that
engine's name for a not-yet-reviewed request (labelled "Requested" in the
UI here, since that's clearer to a Kryx admin who never sees the
booking-engine code). Sending a different, approved WhatsApp template per
status is the point of the per-status split: a "your booking was
declined" message is a different template from "your booking is
confirmed", not the same template with a substituted status word.

The five variables a Kryx Bookings send can ever offer are fixed (not
user-defined like a PCO webhook payload's fields) - they mirror exactly
what the booking engine itself tracks for an appointment.

Each automation also carries a recipient_mode:
- "requester" (default): send to whatever phone number the incoming
  booking payload itself carries (the person who made the booking).
- "fixed": send to a phone number configured on the automation itself
  (recipient_phone), regardless of the payload's own "to" field - e.g. a
  "pending" automation that alerts a fixed approver/manager number that a
  booking needs review, alongside a separate "pending" automation that
  confirms receipt to the requester. Both fire independently for the same
  incoming event.
"""

import hashlib
import json
import secrets
from datetime import datetime, timezone

from ._db import _connect

BOOKING_STATUSES = ["pending", "approved", "declined", "cancelled"]
STATUS_LABELS = {
    "pending": "Requested",
    "approved": "Approved",
    "declined": "Declined",
    "cancelled": "Cancelled",
}

RECIPIENT_MODES = ["requester", "fixed"]

# Order here is just the settings page's dropdown order, not meaningful -
# the actual send order for a given (unit, status) is whatever
# body_variable_order was configured for it.
BOOKING_VARIABLES = ["first_name", "type", "date_time", "status", "location"]

_KEY_PREFIX_LEN = 12


def _template_type(status: str) -> str:
    """A fresh, random suffix per automation - see module docstring for
    why this can't be a deterministic f"kryx_bookings:{status}" the way
    every other status-keyed template_type in this codebase is (that
    scheme assumed exactly one row per (unit, status), which no longer
    holds)."""
    if status not in BOOKING_STATUSES:
        raise ValueError(f"Unknown Kryx Bookings status '{status}' - must be one of {BOOKING_STATUSES}")
    return f"kryx_bookings:{status}:{secrets.token_hex(6)}"


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_api_key(unit_id: int) -> str:
    """Creates (or replaces, if one already existed) this unit's Kryx
    Bookings API key and returns the plaintext value - the only moment
    it's ever available in that form. Replacing rather than appending
    means generating a new key immediately invalidates the old one, which
    is the expected "regenerate" behaviour (a leaked/rotated key should
    stop working the instant a new one is issued, not linger)."""
    raw_key = "kxb_" + secrets.token_urlsafe(32)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO kryx_bookings_connections (unit_id, api_key_hash, api_key_prefix, active, created_at)
            VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(unit_id) DO UPDATE SET
                api_key_hash = excluded.api_key_hash,
                api_key_prefix = excluded.api_key_prefix,
                active = 1,
                created_at = excluded.created_at,
                last_used_at = NULL
            """,
            (unit_id, _hash_key(raw_key), raw_key[:_KEY_PREFIX_LEN], datetime.now(timezone.utc).isoformat()),
        )
    return raw_key


def set_connection_active(unit_id: int, active: bool) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE kryx_bookings_connections SET active = ? WHERE unit_id = ?",
            (int(active), unit_id),
        )


def get_connection(unit_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT id, api_key_prefix, active, created_at, last_used_at
            FROM kryx_bookings_connections WHERE unit_id = ?
            """,
            (unit_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "id": row[0], "unit_id": unit_id, "api_key_prefix": row[1],
        "active": bool(row[2]), "created_at": row[3], "last_used_at": row[4],
    }


def get_connection_by_api_key(raw_key: str) -> dict | None:
    """Auth lookup for POST /integrations/kryx-bookings/send - joins in
    everything integrations/kryx_bookings.py needs from the owning unit
    (org_id for the module gate, slug for logging, default_region for
    phone normalisation), same shape as
    storage.get_email_wa_integration_by_local_part."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT c.id, c.unit_id, c.active, u.org_id, u.slug, u.default_region
            FROM kryx_bookings_connections c
            JOIN units u ON u.id = c.unit_id
            WHERE c.api_key_hash = ?
            """,
            (_hash_key(raw_key),),
        ).fetchone()
    if not row:
        return None
    return {
        "id": row[0], "unit_id": row[1], "active": bool(row[2]),
        "org_id": row[3], "unit_slug": row[4], "default_region": row[5],
    }


def touch_last_used(connection_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE kryx_bookings_connections SET last_used_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), connection_id),
        )


def _validate_automation_fields(
    status: str, body_variable_order: list[str], recipient_mode: str, recipient_phone: str | None,
) -> None:
    if status not in BOOKING_STATUSES:
        raise ValueError(f"Unknown Kryx Bookings status '{status}' - must be one of {BOOKING_STATUSES}")
    unknown = [v for v in body_variable_order if v not in BOOKING_VARIABLES]
    if unknown:
        raise ValueError(f"Unknown Kryx Bookings variable(s): {unknown}")
    if recipient_mode not in RECIPIENT_MODES:
        raise ValueError(f"recipient_mode must be one of {RECIPIENT_MODES}")
    if recipient_mode == "fixed" and not recipient_phone:
        raise ValueError("recipient_phone is required when recipient_mode is 'fixed'")


def upsert_booking_automation(
    automation_id: int | None, unit_id: int, status: str, template_name: str,
    body_variable_order: list[str], whatsapp_number_id: int | None, button_variables: list[str],
    header_image_url: str | None, active: bool, recipient_mode: str = "requester",
    recipient_phone: str | None = None,
) -> int:
    """automation_id=None creates a new automation (and a fresh
    whatsapp_templates row to back it); otherwise updates the existing
    automation (and its owned whatsapp_templates row) in place, including
    reassigning it to a different unit/status if the caller changed those
    - same "identity is the row's own id, not a natural key" model as
    every other CRUD screen in this app, now that (unit, status) is no
    longer unique per automation.

    Deliberately its own INSERT/UPDATE here rather than reusing
    units._upsert_whatsapp_template_row - that helper upserts by
    (unit_id, template_type), which can't represent "move this automation
    to a different unit" (the ON CONFLICT target itself would change), so
    it would leave the old row behind as an orphan. Old header images are
    still cleaned up the same way that helper does, for the same reason."""
    from .header_images import delete_header_image_file

    _validate_automation_fields(status, body_variable_order, recipient_mode, recipient_phone)
    body_json = json.dumps(body_variable_order)
    buttons_json = json.dumps(button_variables or [])

    with _connect() as conn:
        if automation_id is not None:
            row = conn.execute(
                "SELECT whatsapp_template_id FROM kryx_bookings_automations WHERE id = ?",
                (automation_id,),
            ).fetchone()
            if not row:
                raise ValueError(f"Kryx Bookings automation {automation_id} not found")
            whatsapp_template_id = row[0]

            old_header = conn.execute(
                "SELECT header_image_url FROM whatsapp_templates WHERE id = ?",
                (whatsapp_template_id,),
            ).fetchone()
            old_header_image_url = old_header[0] if old_header else None

            conn.execute(
                """
                UPDATE whatsapp_templates
                SET unit_id = ?, template_name = ?, body_variable_order = ?,
                    button_variables = ?, header_image_url = ?, whatsapp_number_id = ?, active = ?
                WHERE id = ?
                """,
                (unit_id, template_name, body_json, buttons_json, header_image_url,
                 whatsapp_number_id, int(active), whatsapp_template_id),
            )
            conn.execute(
                """
                UPDATE kryx_bookings_automations
                SET unit_id = ?, status = ?, recipient_mode = ?, recipient_phone = ?, active = ?
                WHERE id = ?
                """,
                (unit_id, status, recipient_mode, recipient_phone, int(active), automation_id),
            )
            conn.commit()

            if old_header_image_url and old_header_image_url != header_image_url:
                delete_header_image_file(old_header_image_url)
            return automation_id

        template_type = _template_type(status)
        cur = conn.execute(
            """
            INSERT INTO whatsapp_templates
                (unit_id, template_type, template_name, body_variable_order,
                 button_variables, header_image_url, whatsapp_number_id, active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (unit_id, template_type, template_name, body_json, buttons_json,
             header_image_url, whatsapp_number_id, int(active)),
        )
        whatsapp_template_id = cur.lastrowid
        cur2 = conn.execute(
            """
            INSERT INTO kryx_bookings_automations
                (unit_id, status, recipient_mode, recipient_phone, whatsapp_template_id, active)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (unit_id, status, recipient_mode, recipient_phone, whatsapp_template_id, int(active)),
        )
        conn.commit()
        return cur2.lastrowid


def get_booking_automation(automation_id: int) -> dict | None:
    """Ownership/scoping lookup (which unit does this automation belong
    to) - used by the router before allowing an update/delete, same
    pattern as e.g. storage.get_email_integration_by_id."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT a.id, a.unit_id, a.status, a.recipient_mode, a.recipient_phone, a.active,
                   t.id AS whatsapp_template_id, t.header_image_url
            FROM kryx_bookings_automations a
            JOIN whatsapp_templates t ON t.id = a.whatsapp_template_id
            WHERE a.id = ?
            """,
            (automation_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "id": row[0], "unit_id": row[1], "status": row[2], "recipient_mode": row[3],
        "recipient_phone": row[4], "active": bool(row[5]), "whatsapp_template_id": row[6],
        "header_image_url": row[7],
    }


def delete_booking_automation(automation_id: int) -> None:
    """Removes the automation and its owned whatsapp_templates row (and
    header image file, if any) - ON DELETE CASCADE on
    kryx_bookings_automations.whatsapp_template_id only cleans up if the
    whatsapp_templates row is deleted first from that side, not the
    reverse, so this deletes both explicitly rather than relying on the
    FK alone. A no-op if the id doesn't exist (same "idempotent delete"
    shape as storage.delete_email_integration)."""
    from .header_images import delete_header_image_file

    automation = get_booking_automation(automation_id)
    if automation is None:
        return
    with _connect() as conn:
        conn.execute("DELETE FROM kryx_bookings_automations WHERE id = ?", (automation_id,))
        conn.execute("DELETE FROM whatsapp_templates WHERE id = ?", (automation["whatsapp_template_id"],))
        conn.commit()
    if automation["header_image_url"]:
        delete_header_image_file(automation["header_image_url"])


def list_booking_automations(unit_ids: list[int] | None, status: str) -> list[dict]:
    """unit_ids=None means unrestricted (superadmin) - mirrors
    storage.list_registration_templates exactly, just keyed by Kryx
    Bookings status instead of a PCO registration template_type. Now
    returns every automation for that (scoped units, status) pair, not at
    most one per unit."""
    from .scoping import unit_scope_clause

    if status not in BOOKING_STATUSES:
        raise ValueError(f"Unknown Kryx Bookings status '{status}' - must be one of {BOOKING_STATUSES}")

    with _connect() as conn:
        base = """
            SELECT a.id, a.unit_id, u.name AS unit_name, a.recipient_mode, a.recipient_phone,
                   t.template_name, t.body_variable_order, t.whatsapp_number_id, n.label AS number_label,
                   a.active
            FROM kryx_bookings_automations a
            JOIN units u ON u.id = a.unit_id
            JOIN whatsapp_templates t ON t.id = a.whatsapp_template_id
            LEFT JOIN whatsapp_numbers n ON n.id = t.whatsapp_number_id
            WHERE a.status = ?
        """
        scope = unit_scope_clause("a.unit_id", unit_ids, joiner="AND")
        if scope is None:
            return []
        clause, scope_params = scope
        params = [status, *scope_params]
        rows = conn.execute(base + clause + " ORDER BY u.name, a.id", params).fetchall()
        columns = [
            "id", "unit_id", "unit_name", "recipient_mode", "recipient_phone",
            "template_name", "body_variable_order", "whatsapp_number_id", "number_label", "active",
        ]

    results = []
    for r in rows:
        d = dict(zip(columns, r))
        d["body_variable_order"] = json.loads(d["body_variable_order"]) if d["body_variable_order"] else []
        results.append(d)
    return results


def list_active_booking_automations(unit_id: int, status: str) -> list[dict]:
    """Every active automation for this (unit, status) - the send-time
    lookup (integrations/kryx_bookings.py), which now fans out to however
    many automations are configured (e.g. one to the requester, one to a
    fixed approver number) instead of resolving a single template."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT a.id, a.recipient_mode, a.recipient_phone, t.template_name, t.body_variable_order,
                   t.button_variables, t.header_image_url, t.whatsapp_number_id
            FROM kryx_bookings_automations a
            JOIN whatsapp_templates t ON t.id = a.whatsapp_template_id
            WHERE a.unit_id = ? AND a.status = ? AND a.active = 1
            ORDER BY a.id
            """,
            (unit_id, status),
        ).fetchall()
    results = []
    for r in rows:
        results.append({
            "id": r[0],
            "recipient_mode": r[1],
            "recipient_phone": r[2],
            "template_name": r[3],
            "body_variable_order": json.loads(r[4]) if r[4] else [],
            "button_variables": json.loads(r[5]) if r[5] else [],
            "header_image_url": r[6],
            "whatsapp_number_id": r[7],
        })
    return results

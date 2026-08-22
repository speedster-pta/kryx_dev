"""Serving Reminders: per-unit rules for WhatsApp-reminding people
scheduled to serve at an upcoming PCO Services plan, plus the send-dedup
log that stops the same person being reminded twice for the same plan.

Mirrors units.py's form_templates pattern: each rule owns a
synthetic whatsapp_templates row (template_type = f"serving:{service_type_id}")
via the same _upsert_whatsapp_template_row() helper form mappings use, so
it gets whatsapp_number_id/body_variable_order/button_variables/
header_image_url for free instead of duplicating those columns here.
"""

import json
import secrets
from datetime import datetime, timezone

from ._db import _connect

STATUS_FILTERS = ("confirmed_only", "all_scheduled", "unconfirmed_only")
PLAN_SELECTION_MODES = ("next_event", "days_ahead")


def _row_to_serving_rule(columns: list[str], row) -> dict:
    d = dict(zip(columns, row))
    d["body_variable_order"] = json.loads(d["body_variable_order"]) if d["body_variable_order"] else []
    d["button_variables"] = json.loads(d["button_variables"]) if d["button_variables"] else []
    return d


_SERVING_RULE_SELECT = """
    SELECT r.id, r.unit_id, u.name AS unit_name, u.org_id,
           r.pco_service_type_id, r.pco_service_type_name,
           r.send_day_of_week, r.send_time, r.timezone, r.status_filter,
           r.plan_selection_mode, r.days_ahead, r.active,
           t.id AS whatsapp_template_id, t.template_name, t.body_variable_order,
           t.button_variables, t.header_image_url, t.whatsapp_number_id, n.label AS number_label
    FROM serving_reminder_rules r
    JOIN units u ON u.id = r.unit_id
    JOIN whatsapp_templates t ON t.id = r.whatsapp_template_id
    LEFT JOIN whatsapp_numbers n ON n.id = t.whatsapp_number_id
"""
_SERVING_RULE_COLUMNS = [
    "id", "unit_id", "unit_name", "org_id", "pco_service_type_id", "pco_service_type_name",
    "send_day_of_week", "send_time", "timezone", "status_filter",
    "plan_selection_mode", "days_ahead", "active",
    "whatsapp_template_id", "template_name", "body_variable_order",
    "button_variables", "header_image_url", "whatsapp_number_id", "number_label",
]


def list_serving_rules(unit_ids: list[int] | None) -> list[dict]:
    """unit_ids=None means unrestricted (superadmin)."""
    from .scoping import unit_scope_clause

    with _connect() as conn:
        scope = unit_scope_clause("r.unit_id", unit_ids, joiner="WHERE")
        if scope is None:
            return []
        clause, params = scope
        rows = conn.execute(
            _SERVING_RULE_SELECT + clause + " ORDER BY u.name, r.pco_service_type_name", params
        ).fetchall()
        return [_row_to_serving_rule(_SERVING_RULE_COLUMNS, r) for r in rows]


def get_serving_rule_by_id(rule_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute(_SERVING_RULE_SELECT + " WHERE r.id = ?", (rule_id,)).fetchone()
        if not row:
            return None
        return _row_to_serving_rule(_SERVING_RULE_COLUMNS, row)


def list_active_serving_rules() -> list[dict]:
    """Every active rule regardless of unit - for scheduler.py to
    register a recurring job per rule on startup, same shape as
    list_pending_scheduled_campaigns() feeding reload_pending_campaigns()."""
    with _connect() as conn:
        rows = conn.execute(_SERVING_RULE_SELECT + " WHERE r.active = 1").fetchall()
        return [_row_to_serving_rule(_SERVING_RULE_COLUMNS, r) for r in rows]


def _upsert_serving_template_row(
    rule_id: int | None, unit_id: int, pco_service_type_id: str, template_name: str,
    body_variable_order: list[str], whatsapp_number_id: int | None,
    button_variables: list[str], header_image_url: str | None, active: bool,
) -> int:
    """Creates or updates the synthetic whatsapp_templates row backing a
    serving rule. Deliberately NOT units.py's shared
    _upsert_whatsapp_template_row: that helper upserts by the natural key
    (unit_id, template_type), which is correct for registration
    templates and form mappings (each meant to be a strict one-per-key
    singleton) but wrong here - serving rules must allow several rules to
    coexist for the same unit + service type (e.g. a separate
    Wednesday-evening reminder alongside an existing Sunday-morning one),
    so a second rule's save must never be able to collide with and
    silently overwrite a sibling rule's template row. So:
      - editing an existing rule (rule_id given) updates its already-owned
        template row directly by id, never touching template_type;
      - creating a new rule (rule_id=None) always INSERTs a brand-new
        template row, with a random suffix appended to template_type so
        it can never collide with whatsapp_templates' own
        UNIQUE(unit_id, template_type) constraint.
    """
    from .header_images import delete_header_image_file

    with _connect() as conn:
        if rule_id is not None:
            existing = conn.execute(
                """
                SELECT t.id, t.header_image_url
                FROM serving_reminder_rules r
                JOIN whatsapp_templates t ON t.id = r.whatsapp_template_id
                WHERE r.id = ?
                """,
                (rule_id,),
            ).fetchone()
            if existing is None:
                raise ValueError(f"serving reminder rule {rule_id} not found")
            template_id, old_header_image_url = existing
            conn.execute(
                """
                UPDATE whatsapp_templates
                SET template_name = ?, body_variable_order = ?, button_variables = ?,
                    header_image_url = ?, whatsapp_number_id = ?, active = ?
                WHERE id = ?
                """,
                (template_name, json.dumps(body_variable_order), json.dumps(button_variables or []),
                 header_image_url, whatsapp_number_id, int(active), template_id),
            )
            conn.commit()
        else:
            template_type = f"serving:{pco_service_type_id}:{secrets.token_hex(6)}"
            cur = conn.execute(
                """
                INSERT INTO whatsapp_templates
                    (unit_id, template_type, template_name, body_variable_order,
                     button_variables, header_image_url, whatsapp_number_id, active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (unit_id, template_type, template_name, json.dumps(body_variable_order),
                 json.dumps(button_variables or []), header_image_url, whatsapp_number_id, int(active)),
            )
            conn.commit()
            template_id = cur.lastrowid
            old_header_image_url = None

    # Outside the connection block and after commit: the DB write is what
    # matters and must never be rolled back because a file happened to be
    # locked/missing/unwritable.
    if old_header_image_url and old_header_image_url != header_image_url:
        delete_header_image_file(old_header_image_url)

    return template_id


def upsert_serving_rule(
    rule_id: int | None, unit_id: int, pco_service_type_id: str, pco_service_type_name: str,
    send_day_of_week: str, send_time: str, timezone_name: str, status_filter: str,
    template_name: str, body_variable_order: list[str], whatsapp_number_id: int | None,
    button_variables: list[str] | None, header_image_url: str | None, active: bool,
    plan_selection_mode: str = "next_event", days_ahead: int | None = None,
) -> int:
    if status_filter not in STATUS_FILTERS:
        raise ValueError(f"status_filter must be one of {STATUS_FILTERS}")
    if plan_selection_mode not in PLAN_SELECTION_MODES:
        raise ValueError(f"plan_selection_mode must be one of {PLAN_SELECTION_MODES}")
    if plan_selection_mode == "days_ahead":
        if not days_ahead or days_ahead < 1:
            raise ValueError("days_ahead must be a positive integer when plan_selection_mode is 'days_ahead'")
    else:
        # Not used in 'next_event' mode - normalized to None rather than
        # trusting whatever the client happened to send, so a stale value
        # left over from switching modes in the UI never silently takes
        # effect if the mode is switched back later.
        days_ahead = None

    whatsapp_template_id = _upsert_serving_template_row(
        rule_id, unit_id, pco_service_type_id, template_name, body_variable_order,
        whatsapp_number_id, button_variables or [], header_image_url, active,
    )

    with _connect() as conn:
        if rule_id is not None:
            conn.execute(
                """
                UPDATE serving_reminder_rules
                SET unit_id = ?, pco_service_type_id = ?, pco_service_type_name = ?,
                    send_day_of_week = ?, send_time = ?, timezone = ?, status_filter = ?,
                    plan_selection_mode = ?, days_ahead = ?,
                    whatsapp_template_id = ?, active = ?
                WHERE id = ?
                """,
                (unit_id, pco_service_type_id, pco_service_type_name,
                 send_day_of_week, send_time, timezone_name, status_filter,
                 plan_selection_mode, days_ahead,
                 whatsapp_template_id, int(active), rule_id),
            )
            conn.commit()
            return rule_id
        # New rule: always a fresh INSERT - never an upsert keyed by
        # (unit_id, pco_service_type_id). Multiple rules for the
        # same service type are a supported configuration (e.g. separate
        # day/time reminders), not a duplicate to collapse into one row.
        cur = conn.execute(
            """
            INSERT INTO serving_reminder_rules
                (unit_id, pco_service_type_id, pco_service_type_name, send_day_of_week,
                 send_time, timezone, status_filter, plan_selection_mode, days_ahead,
                 whatsapp_template_id, active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (unit_id, pco_service_type_id, pco_service_type_name, send_day_of_week,
             send_time, timezone_name, status_filter, plan_selection_mode, days_ahead,
             whatsapp_template_id, int(active),
             datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return cur.lastrowid


def delete_serving_rule(rule_id: int) -> None:
    from .header_images import delete_header_image_file

    with _connect() as conn:
        row = conn.execute(
            """
            SELECT t.id, t.header_image_url
            FROM serving_reminder_rules r
            JOIN whatsapp_templates t ON t.id = r.whatsapp_template_id
            WHERE r.id = ?
            """,
            (rule_id,),
        ).fetchone()
        conn.execute("DELETE FROM serving_reminder_rules WHERE id = ?", (rule_id,))
        if row:
            # 1:1 synthetic template row, same cleanup as delete_form_mapping
            conn.execute("DELETE FROM whatsapp_templates WHERE id = ?", (row[0],))
        conn.commit()

    if row and row[1]:
        delete_header_image_file(row[1])


# ---- Send dedup / audit log ----

def is_serving_reminder_sent(rule_id: int, pco_plan_id: str, pco_person_id: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM serving_reminder_log WHERE rule_id = ? AND pco_plan_id = ? AND pco_person_id = ? AND status = 'sent'",
            (rule_id, pco_plan_id, pco_person_id),
        ).fetchone()
        return row is not None


def mark_serving_reminder(
    rule_id: int, pco_plan_id: str, pco_person_id: str, status: str, detail: str | None = None
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO serving_reminder_log (rule_id, pco_plan_id, pco_person_id, sent_at, status, detail)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(rule_id, pco_plan_id, pco_person_id) DO UPDATE SET
                sent_at = excluded.sent_at, status = excluded.status, detail = excluded.detail
            """,
            (rule_id, pco_plan_id, pco_person_id, datetime.now(timezone.utc).isoformat(), status, detail),
        )
        conn.commit()


def list_deferred_serving_reminders() -> list[dict]:
    """Every (rule_id, pco_plan_id) pair currently holding at least one
    status='deferred' row in the log, for rules that are still active.
    Feeds the scheduler's periodic serving-reminder throttle-recheck job
    (scheduler.py::recheck_deferred_serving_reminders) - the serving
    reminder equivalent of list_throttled_campaigns(), except state here
    lives per rule+plan+person in serving_reminder_log rather than on a
    single campaign-level status column, so this returns distinct
    (rule_id, plan_id) pairs to retry rather than whole rule/campaign rows.

    A rule can have deferred rows against more than one plan at once
    (e.g. plan_selection_mode='days_ahead' throttled partway through
    several plans in one run) - each pair is returned separately since
    retrying is naturally scoped per plan, not per rule.

    Excludes rules that have since gone inactive or been deleted (the
    JOIN against serving_reminder_rules drops those implicitly) - no
    point retrying a send for a rule users turned off.
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT l.rule_id, l.pco_plan_id
            FROM serving_reminder_log l
            JOIN serving_reminder_rules r ON r.id = l.rule_id
            WHERE l.status = 'deferred' AND r.active = 1
            """
        ).fetchall()
        return [{"rule_id": r[0], "pco_plan_id": r[1]} for r in rows]


# ---- Service type cache (per-unit PCO folder scoping) ----

def get_cached_service_types(unit_id: int, today: str) -> list[dict] | None:
    """Returns the cached [{"id":..., "name":...}, ...] list if this
    unit already has a cache stamped with today's date, or None
    if there's nothing cached yet today (first selection of the day, or
    never polled) - callers treat None as "go poll PCO and repopulate"."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT pco_service_type_id, pco_service_type_name FROM serving_service_type_cache "
            "WHERE unit_id = ? AND cached_date = ? ORDER BY pco_service_type_name",
            (unit_id, today),
        ).fetchall()
        if not rows:
            return None
        return [{"id": r[0], "name": r[1]} for r in rows]


def set_cached_service_types(unit_id: int, service_types: list[dict], today: str) -> None:
    """Wholesale replace: clears any prior cache (today's or stale) for
    this unit and inserts the freshly-polled set stamped with
    today's date. Whole-set replace rather than incremental diffing since
    the source list itself is small (one unit's service types) and
    this only runs once a day per unit."""
    with _connect() as conn:
        conn.execute("DELETE FROM serving_service_type_cache WHERE unit_id = ?", (unit_id,))
        conn.executemany(
            "INSERT INTO serving_service_type_cache (unit_id, pco_service_type_id, pco_service_type_name, cached_date) "
            "VALUES (?, ?, ?, ?)",
            [(unit_id, st["id"], st["name"], today) for st in service_types],
        )
        conn.commit()


def get_serving_reminder_counts(rule_id: int, pco_plan_id: str) -> dict:
    """sent/failed counts for this rule's most recent run against a given
    plan - lets the manual "send now" button show "already sent to N
    people" instead of blindly re-attempting everyone the dedup log would
    just skip anyway."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM serving_reminder_log WHERE rule_id = ? AND pco_plan_id = ? GROUP BY status",
            (rule_id, pco_plan_id),
        ).fetchall()
        return {status: count for status, count in rows}

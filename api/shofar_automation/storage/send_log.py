"""
Append-only history of individual transactional WhatsApp sends
(registration poller + form-response webhook).

Unlike dedup.py's processed_registrations/processed_form_submissions -
which key on registration_id/submission_id and overwrite on retry, because
their job is answering "have we already sent this one" - every call to
record_send() here inserts a new row, so a failed attempt that later
succeeds on retry still shows up as two separate history entries.
"""

from datetime import datetime, timedelta, timezone

from ._db import _connect

_COLUMNS = [
    "id", "sent_at", "unit_id", "whatsapp_number_id", "source",
    "recipient_phone", "template_name", "status", "error_code",
    "error_message", "reference_id",
]

# Whitelist of columns /history's table can be sorted by, mapped to their
# qualified SQL expression. "number" sorts by the WhatsApp number's label
# (what's actually shown in the table), not the raw whatsapp_number_id, so
# this requires the LEFT JOIN in get_recent_sends() below rather than just
# ordering by a send_log column directly. Never interpolate the `sort`
# query param straight into SQL - always go through this dict so an
# unrecognized value can only ever fall back to "time", never reach the
# query string.
_SORT_COLUMNS = {
    "time": "sl.sent_at",
    "source": "sl.source",
    "recipient": "sl.recipient_phone",
    "number": "wn.label",
}


def record_send(
    unit_id: int | None,
    source: str,
    status: str,
    whatsapp_number_id: int | None = None,
    recipient_phone: str | None = None,
    template_name: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    reference_id: str | None = None,
) -> None:
    """source: 'registration_poller' or 'form_webhook'.
    status: 'sent', 'failed', or 'deferred' (messaging-limit defer, not a
    real failure - see MessagingLimitExceeded handling in both send paths).
    reference_id: the registration_id or submission_id this attempt was for,
    for cross-referencing against dedup.py's tables if ever needed."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO send_log (
                sent_at, unit_id, whatsapp_number_id, source,
                recipient_phone, template_name, status, error_code,
                error_message, reference_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                unit_id, whatsapp_number_id, source, recipient_phone,
                template_name, status, error_code, error_message, reference_id,
            ),
        )


def _scope_where(unit_ids: list[int] | None, whatsapp_number_id: int | None) -> tuple[str, list] | None:
    """Builds the WHERE clause (and its params) shared by
    get_recent_sends(), get_send_count(), get_distinct_number_ids(), and
    get_send_status_summary(), so their scoping can't drift apart.

    Columns are qualified as sl.* since get_recent_sends() joins against
    whatsapp_numbers (aliased wn) to sort by number label, and
    unit_id exists on both tables - every caller here aliases
    send_log as sl (even the ones that don't join) so this WHERE clause
    is valid unchanged in all of them.

    Returns None when unit_ids is an empty list, meaning the
    caller has no units assigned and therefore no accessible rows
    at all - distinct from unit_ids=None, which means "no
    scoping, superadmin sees everything"."""
    if unit_ids is not None and not unit_ids:
        return None
    clauses = []
    params: list = []
    if unit_ids is not None:
        placeholders = ", ".join("?" for _ in unit_ids)
        clauses.append(f"sl.unit_id IN ({placeholders})")
        params.extend(unit_ids)
    if whatsapp_number_id is not None:
        clauses.append("sl.whatsapp_number_id = ?")
        params.append(whatsapp_number_id)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


def get_recent_sends(
    limit: int = 50,
    offset: int = 0,
    unit_ids: list[int] | None = None,
    whatsapp_number_id: int | None = None,
    sort: str = "time",
    direction: str = "desc",
) -> list[dict]:
    """Most recent sends first by default. unit_ids=None returns
    across all units (superadmin); pass a list to scope to one or
    more units, matching the multi-unit
    staff_user_units model in admin_auth.py's session
    unit_ids. whatsapp_number_id optionally narrows to a single
    number. offset supports pagination (e.g. /history's page-of-N view).

    sort: one of "time" (default), "source", "recipient", "number" - any
    other value silently falls back to "time" rather than raising, since
    this is driven by a query param HistoryView passes straight through.
    "number" sorts by the WhatsApp number's label via a LEFT JOIN against
    whatsapp_numbers, not the raw whatsapp_number_id, so it matches what
    /history actually displays in that column.
    direction: "asc" or "desc" (default), same fallback behaviour.
    When sorting by something other than time, sent_at DESC is appended
    as a stable secondary order so rows with equal sort values still come
    out most-recent-first rather than in arbitrary/sqlite-internal order."""
    scope = _scope_where(unit_ids, whatsapp_number_id)
    if scope is None:
        return []  # no units assigned - nothing accessible
    where, params = scope

    sort_col = _SORT_COLUMNS.get(sort, _SORT_COLUMNS["time"])
    sort_dir = "ASC" if direction == "asc" else "DESC"
    order_by = f"{sort_col} {sort_dir}"
    if sort_col != _SORT_COLUMNS["time"]:
        order_by += ", sl.sent_at DESC"

    select_cols = ", ".join(f"sl.{c}" for c in _COLUMNS)
    query = (
        f"SELECT {select_cols} FROM send_log sl "
        f"LEFT JOIN whatsapp_numbers wn ON wn.id = sl.whatsapp_number_id"
        f"{where} ORDER BY {order_by} LIMIT ? OFFSET ?"
    )
    params = params + [limit, offset]
    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(zip(_COLUMNS, row)) for row in rows]


def get_send_count(unit_ids: list[int] | None = None, whatsapp_number_id: int | None = None) -> int:
    """Total matching row count, same scoping as get_recent_sends() -
    used to compute total pages for /history's pagination."""
    scope = _scope_where(unit_ids, whatsapp_number_id)
    if scope is None:
        return 0
    where, params = scope
    query = f"SELECT COUNT(*) FROM send_log sl{where}"
    with _connect() as conn:
        return conn.execute(query, params).fetchone()[0]


def get_distinct_number_ids(unit_ids: list[int] | None = None) -> list[int]:
    """Every distinct whatsapp_number_id that has ever appeared in
    send_log within scope - backs the Number filter dropdown on /history
    and the Automations page's recent-history card, so the dropdown's
    options stay stable across pagination rather than being limited to
    whatever happens to be on the current page."""
    scope = _scope_where(unit_ids, None)
    if scope is None:
        return []
    where, params = scope
    query = f"SELECT DISTINCT sl.whatsapp_number_id FROM send_log sl{where}"
    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [r[0] for r in rows if r[0] is not None]


def get_send_status_summary(days: int, unit_ids: list[int] | None = None) -> dict[str, int]:
    """Counts by status ('sent'/'failed'/'deferred') for the last `days`
    days, same scoping as get_recent_sends() - backs the 7/30/90-day
    summary cards at the top of /history, same idea as waba_usage.html's
    "Totals - last N days" section but broken out by send status rather
    than WABA pool."""
    scope = _scope_where(unit_ids, None)
    if scope is None:
        return {}
    where, params = scope
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    since_clause = "sl.sent_at >= ?"
    where = f"{where} AND {since_clause}" if where else f" WHERE {since_clause}"
    params = params + [since]
    query = f"SELECT status, COUNT(*) FROM send_log sl{where} GROUP BY status"
    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return {status: count for status, count in rows}

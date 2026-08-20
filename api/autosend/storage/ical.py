"""
storage/ical.py

CRUD over ical_events/ical_links/ical_link_events (schema owned by
integrations/ical/schema.py - see that module's docstring for the overall
design). Any automation trigger (serving_reminder.py today; a future
email_wa/sme_metrics provider) goes through upsert_ical_event() /
get_or_create_ical_link() / attach_event_to_link() rather than inserting
rows directly, so the update-in-place-vs-new-row decision and the
token-reuse-per-recipient decision stay in one place.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone

from ._db import _connect

_EVENT_COLUMNS = [
    "id", "unit_id", "uid", "sequence", "title", "description", "location",
    "starts_at", "ends_at", "status", "source_system", "source_external_id",
    "expires_at", "created_at", "updated_at",
]

_LINK_COLUMNS = [
    "id", "link_key", "token", "recipient_phone", "expires_at",
    "revoked_at", "accessed_count", "last_accessed_at", "created_at",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_event(row) -> dict:
    return dict(zip(_EVENT_COLUMNS, row))


def _row_to_link(row) -> dict:
    return dict(zip(_LINK_COLUMNS, row))


def get_ical_event_by_source(unit_id: int, source_system: str, source_external_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {', '.join(_EVENT_COLUMNS)} FROM ical_events "
            "WHERE unit_id = ? AND source_system = ? AND source_external_id = ?",
            (unit_id, source_system, source_external_id),
        ).fetchone()
    return _row_to_event(row) if row else None


def upsert_ical_event(
    unit_id: int,
    source_system: str,
    source_external_id: str | None,
    title: str,
    starts_at: str,
    *,
    description: str | None = None,
    location: str | None = None,
    ends_at: str | None = None,
    expires_at: str,
) -> tuple[dict, bool]:
    """Creates a new ical_events row, or - only when source_external_id is
    given and a prior row with the same (unit_id, source_system,
    source_external_id) exists - updates that row in place instead
    (bumping `sequence` per RFC 5545 so a calendar app that re-imports the
    same UID picks up the change rather than duplicating the entry).

    source_external_id=None always inserts a new row: a source with no
    stable identifier for "this is the same occurrence as before" has no
    basis to correlate against, so every trigger becomes its own event
    (see integrations/ical/schema.py's docstring on the NULL-is-distinct
    behaviour this relies on).

    Returns (event, is_update).
    """
    now = _now_iso()

    existing = (
        get_ical_event_by_source(unit_id, source_system, source_external_id)
        if source_external_id is not None
        else None
    )

    with _connect() as conn:
        if existing:
            conn.execute(
                """
                UPDATE ical_events
                SET sequence = sequence + 1, title = ?, description = ?, location = ?,
                    starts_at = ?, ends_at = ?, status = 'confirmed', expires_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (title, description, location, starts_at, ends_at, expires_at, now, existing["id"]),
            )
            row = conn.execute(
                f"SELECT {', '.join(_EVENT_COLUMNS)} FROM ical_events WHERE id = ?",
                (existing["id"],),
            ).fetchone()
            return _row_to_event(row), True

        cursor = conn.execute(
            f"""
            INSERT INTO ical_events (
                unit_id, uid, sequence, title, description, location,
                starts_at, ends_at, status, source_system, source_external_id,
                expires_at, created_at, updated_at
            ) VALUES (?, ?, 0, ?, ?, ?, ?, ?, 'confirmed', ?, ?, ?, ?, ?)
            """,
            (
                unit_id, str(uuid.uuid4()), title, description, location,
                starts_at, ends_at, source_system, source_external_id,
                expires_at, now, now,
            ),
        )
        row = conn.execute(
            f"SELECT {', '.join(_EVENT_COLUMNS)} FROM ical_events WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()
        return _row_to_event(row), False


def cancel_ical_event(event_id: int) -> None:
    """Marks the event cancelled and bumps sequence, so the next time any
    of its links is opened the .ics served reflects STATUS:CANCELLED
    rather than 404ing (see integrations/ical/builder.py) - callers should
    still send an explicit WhatsApp cancellation message too, since a
    static re-fetch isn't a reliable update-push mechanism on its own."""
    with _connect() as conn:
        conn.execute(
            "UPDATE ical_events SET status = 'cancelled', sequence = sequence + 1, updated_at = ? WHERE id = ?",
            (_now_iso(), event_id),
        )


def get_or_create_ical_link(link_key: str, recipient_phone: str | None, expires_at: str) -> dict:
    """Idempotent per (link_key, recipient_phone): re-running an
    automation that already generated this person's link for this exact
    key returns the same token rather than minting a new one, so a link
    already sitting in someone's WhatsApp thread keeps working.
    recipient_phone=None always creates a fresh, unshared-with-any-lookup
    link (see schema.py's docstring on NULL-is-distinct).

    link_key is caller-defined - a single-event caller can use something
    like f"appt:{event uid}"; a bundling caller (see
    services/serving_reminder.py) derives it from the exact set of events
    being combined, so a different set of events naturally gets a
    different link rather than growing an existing one indefinitely."""
    now = _now_iso()
    with _connect() as conn:
        if recipient_phone is not None:
            row = conn.execute(
                f"SELECT {', '.join(_LINK_COLUMNS)} FROM ical_links "
                "WHERE link_key = ? AND recipient_phone = ?",
                (link_key, recipient_phone),
            ).fetchone()
            if row:
                return _row_to_link(row)

        token = secrets.token_urlsafe(32)
        cursor = conn.execute(
            """
            INSERT INTO ical_links (
                link_key, token, recipient_phone, expires_at,
                accessed_count, created_at
            ) VALUES (?, ?, ?, ?, 0, ?)
            """,
            (link_key, token, recipient_phone, expires_at, now),
        )
        row = conn.execute(
            f"SELECT {', '.join(_LINK_COLUMNS)} FROM ical_links WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()
        return _row_to_link(row)


def attach_event_to_link(ical_link_id: int, ical_event_id: int) -> None:
    """Idempotent - safe to call again on a rerun that includes an event
    already attached to this link."""
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO ical_link_events (ical_link_id, ical_event_id) VALUES (?, ?)",
            (ical_link_id, ical_event_id),
        )


def get_ical_link_with_events(token: str) -> dict | None:
    """Pure lookup, no validity check - callers (web/ical_router.py)
    decide separately whether the link/its events are still live, so a
    not-found vs found-but-expired/revoked distinction is available to
    the rate-limiting layer (only a genuine not-found should count as a
    guessing attempt - see web/ical_link_security.py).

    Returns the link dict with an "events" key (list of event dicts,
    ordered by starts_at) - one event for a single-appointment link, many
    for a bundled one. A link with zero attached events (shouldn't
    normally happen, but e.g. a race with the events not yet attached)
    returns events=[]."""
    with _connect() as conn:
        link_row = conn.execute(
            f"SELECT {', '.join(_LINK_COLUMNS)} FROM ical_links WHERE token = ?",
            (token,),
        ).fetchone()
        if not link_row:
            return None
        link = _row_to_link(link_row)

        event_rows = conn.execute(
            f"""
            SELECT {', '.join(f'e.{c}' for c in _EVENT_COLUMNS)}
            FROM ical_link_events le
            JOIN ical_events e ON e.id = le.ical_event_id
            WHERE le.ical_link_id = ?
            ORDER BY e.starts_at
            """,
            (link["id"],),
        ).fetchall()
        link["events"] = [_row_to_event(r) for r in event_rows]
        return link


def mark_ical_link_accessed(link_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE ical_links SET accessed_count = accessed_count + 1, last_accessed_at = ? WHERE id = ?",
            (_now_iso(), link_id),
        )

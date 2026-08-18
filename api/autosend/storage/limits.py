"""
WhatsApp 24h messaging-limit tracking, keyed on waba_id (pooled per
portfolio) or a per-number fallback key. See whatsapp_limits.py for the
higher-level logic that calls into these; this module is the raw
message_log / waba_limits persistence.
"""

from datetime import datetime, timezone, timedelta

from ._db import _connect


def log_sent_message(limit_key: str, recipient_phone: str, campaign_id: int | None = None) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO message_log (limit_key, recipient_phone, campaign_id, sent_at) VALUES (?, ?, ?, ?)",
            (limit_key, recipient_phone, campaign_id, datetime.now(timezone.utc).isoformat()),
        )


def count_recent_unique_recipients(limit_key: str, window_hours: int = 24) -> int:
    """Distinct recipients messaged in the trailing window - Meta's limit
    counts unique contacts reached, not total messages sent."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(DISTINCT recipient_phone) FROM message_log WHERE limit_key = ? AND sent_at >= ?",
            (limit_key, cutoff),
        ).fetchone()
        return row[0] if row else 0


def oldest_message_in_window(limit_key: str, window_hours: int = 24) -> str | None:
    """Used only to estimate when capacity will free up (oldest + 24h) for
    display purposes - not stored, recomputed live each time it's asked."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    with _connect() as conn:
        row = conn.execute(
            "SELECT MIN(sent_at) FROM message_log WHERE limit_key = ? AND sent_at >= ?",
            (limit_key, cutoff),
        ).fetchone()
        return row[0] if row and row[0] else None


def get_waba_limit(limit_key: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT limit_key, messaging_limit_tier, limit_synced_at, restricted_until "
            "FROM waba_limits WHERE limit_key = ?",
            (limit_key,),
        ).fetchone()
        if not row:
            return None
        return {
            "limit_key": row[0], "messaging_limit_tier": row[1],
            "limit_synced_at": row[2], "restricted_until": row[3],
        }


def upsert_waba_limit_tier(limit_key: str, tier: str, synced_at: str) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO waba_limits (limit_key, messaging_limit_tier, limit_synced_at)
            VALUES (?, ?, ?)
            ON CONFLICT(limit_key) DO UPDATE SET
                messaging_limit_tier = excluded.messaging_limit_tier,
                limit_synced_at = excluded.limit_synced_at
            """,
            (limit_key, tier, synced_at),
        )


def set_waba_restricted(limit_key: str, restricted_until: str) -> None:
    """Records that Meta itself rejected a send for this pool as exceeding
    the messaging limit - see whatsapp_limits.record_rejection(). Upserts
    so this works whether or not a tier row already exists for this key."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO waba_limits (limit_key, restricted_until)
            VALUES (?, ?)
            ON CONFLICT(limit_key) DO UPDATE SET restricted_until = excluded.restricted_until
            """,
            (limit_key, restricted_until),
        )


def daily_message_counts(days: int = 30) -> list[dict]:
    """Per-day, per-WABA send totals for the usage dashboard - a pure
    read over message_log, no new writes. `limit_key` here is whatever
    _limit_key() in whatsapp_limits.py logged the send under: a real
    waba_id when the number's portfolio is known, or a
    "number:{phone_number_id}" fallback otherwise (see that module's
    docstring). SQLite's date() understands the ISO8601 timestamps
    sent_at is stored as, so grouping by calendar date needs no Python-side
    parsing. Ordered newest-day-first, then busiest WABA first within a
    day, since that's the order you'd scan a report in."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT date(sent_at) AS send_date, limit_key, COUNT(*) AS message_count
            FROM message_log
            WHERE sent_at >= ?
            GROUP BY send_date, limit_key
            ORDER BY send_date DESC, message_count DESC
            """,
            (cutoff,),
        ).fetchall()
        return [
            {"date": r[0], "limit_key": r[1], "message_count": r[2]}
            for r in rows
        ]


def waba_label_map() -> dict[str, str]:
    """Maps every limit_key currently in use (real waba_id or the
    "number:{phone_number_id}" fallback - see _limit_key() in
    whatsapp_limits.py) to a human-readable label for the usage
    dashboard, so it shows "Kryx - Main Line" instead of a raw
    WABA ID. A waba_id can be shared by several numbers/units
    (that's the whole point of Meta's pooled limit) so those are joined
    with '+' into one label; a fallback key only ever maps to the one
    number it was minted for."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT n.waba_id, n.phone_number_id, n.label, u.name
            FROM whatsapp_numbers n
            JOIN units u ON u.id = n.unit_id
            """
        ).fetchall()

    grouped: dict[str, list[str]] = {}
    for waba_id, phone_number_id, label, cong_name in rows:
        key = waba_id or f"number:{phone_number_id}"
        grouped.setdefault(key, []).append(f"{cong_name} - {label}")

    return {key: " + ".join(labels) for key, labels in grouped.items()}

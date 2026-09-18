"""
Real send-volume reporting for the /usage dashboard.

message_log (storage/limits.py) only exists to gate the 24h WABA messaging
limit, so it only logs business-initiated *template* sends - it misses
Inbox staff replies and AI auto-replies entirely, both of which are real
outbound messages an org would want to see counted. This module instead
UNIONs across every table that represents an actual, successfully-sent
outbound WhatsApp message:
  - campaign_recipients (status='sent') - bulk campaigns, joined through
    campaigns for unit_id/whatsapp_number_id since the recipient row
    itself carries neither.
  - send_log (status='sent') - PCO registration/form-webhook automations.
  - conversation_messages (direction='out', sender_type='staff',
    status='sent') - manual Inbox replies, joined through conversations
    for unit_id/whatsapp_number_id.
  - ai_reply_log (sent=1, source IN ('live', 'keyword')) - AI Assistant
    auto-replies; whatsapp_number_id is direct, unit_id still needs the
    conversations join.
"""

from datetime import datetime, timedelta, timezone

from ._db import _connect

_COMBINED_SOURCES_SQL = """
    SELECT date(cr.updated_at) AS send_date, c.unit_id AS unit_id, c.whatsapp_number_id AS whatsapp_number_id
    FROM campaign_recipients cr
    JOIN campaigns c ON c.id = cr.campaign_id
    WHERE cr.status = 'sent'

    UNION ALL

    SELECT date(sl.sent_at), sl.unit_id, sl.whatsapp_number_id
    FROM send_log sl
    WHERE sl.status = 'sent'

    UNION ALL

    SELECT date(cm.created_at), conv.unit_id, conv.whatsapp_number_id
    FROM conversation_messages cm
    JOIN conversations conv ON conv.id = cm.conversation_id
    WHERE cm.direction = 'out' AND cm.sender_type = 'staff' AND cm.status = 'sent'

    UNION ALL

    SELECT date(arl.created_at), conv.unit_id, arl.whatsapp_number_id
    FROM ai_reply_log arl
    JOIN conversations conv ON conv.id = arl.conversation_id
    WHERE arl.sent = 1 AND arl.source IN ('live', 'keyword')
"""


def send_totals_by_number(days: int = 30) -> list[dict]:
    """Total sent-message count per WhatsApp number over the trailing
    `days`, across every source above - backs the /usage page's summary
    tiles. Numbers with zero sends in the window simply don't appear."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    with _connect() as conn:
        rows = conn.execute(
            f"""
            WITH combined AS ({_COMBINED_SOURCES_SQL})
            SELECT whatsapp_number_id, COUNT(*) AS message_count
            FROM combined
            WHERE send_date >= ? AND whatsapp_number_id IS NOT NULL
            GROUP BY whatsapp_number_id
            ORDER BY message_count DESC
            """,
            (cutoff,),
        ).fetchall()
    return [{"whatsapp_number_id": r[0], "message_count": r[1]} for r in rows]


def daily_send_group_count(days: int = 30) -> int:
    """Number of distinct (date, unit_id) groups in the trailing `days` -
    used to compute total pages for /usage's daily breakdown, same
    page/offset convention as storage.get_send_count/get_recent_sends."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    with _connect() as conn:
        return conn.execute(
            f"""
            WITH combined AS ({_COMBINED_SOURCES_SQL})
            SELECT COUNT(*) FROM (
                SELECT 1 FROM combined WHERE send_date >= ? GROUP BY send_date, unit_id
            )
            """,
            (cutoff,),
        ).fetchone()[0]


def daily_send_counts(days: int = 30, limit: int = 10, offset: int = 0) -> list[dict]:
    """Paginated daily breakdown rows grouped by (date, unit_id) - newest
    day first, then busiest unit within a day. Pair with
    daily_send_group_count() for total-pages, same split as
    storage.get_recent_sends()/get_send_count()."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    with _connect() as conn:
        rows = conn.execute(
            f"""
            WITH combined AS ({_COMBINED_SOURCES_SQL})
            SELECT send_date, unit_id, COUNT(*) AS message_count
            FROM combined
            WHERE send_date >= ?
            GROUP BY send_date, unit_id
            ORDER BY send_date DESC, message_count DESC
            LIMIT ? OFFSET ?
            """,
            (cutoff, limit, offset),
        ).fetchall()
    return [{"date": r[0], "unit_id": r[1], "message_count": r[2]} for r in rows]


def unit_label_map() -> dict[int, str]:
    """unit_id -> unit name, for labeling the daily-breakdown table. Plain
    lookup (no WABA-style grouping needed - unit_id already uniquely
    identifies one row here)."""
    with _connect() as conn:
        rows = conn.execute("SELECT id, name FROM units").fetchall()
    return {r[0]: r[1] for r in rows}


def number_label_map() -> dict[int, str]:
    """whatsapp_number_id -> "Unit - Number label", for the summary tiles
    - same display format as storage.waba_label_map() but keyed by the
    number's own id rather than its (possibly shared) WABA pool, since
    send_totals_by_number() groups by whatsapp_number_id directly."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT n.id, n.label, u.name
            FROM whatsapp_numbers n
            JOIN units u ON u.id = n.unit_id
            """
        ).fetchall()
    return {r[0]: f"{r[2]} - {r[1]}" for r in rows}

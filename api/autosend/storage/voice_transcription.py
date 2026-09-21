"""storage/voice_transcription.py

Voice Transcription module (storage.MODULE_VOICE_TRANSCRIPTION): which
WhatsApp number is assigned to receive forwarded voice notes
(whatsapp_numbers.voice_transcription_enabled), that number's whitelist
of activated sending numbers (voice_transcription_allowed_senders) -
only a whitelisted sender's voice note gets a transcription reply, see
services/voice_transcription_reply.py - and an append-only log of every
voice note the module claimed (voice_transcription_log), which powers
the /usage page's per-number counter.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ._db import _connect

_SENDER_COLUMNS = ["id", "whatsapp_number_id", "wa_id", "label", "created_at"]


def get_voice_transcription_settings() -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT model, effort FROM voice_transcription_settings LIMIT 1"
        ).fetchone()
        if not row:
            return None
        return {"model": row[0], "effort": row[1]}


def set_voice_transcription_enabled(whatsapp_number_id: int, enabled: bool) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE whatsapp_numbers SET voice_transcription_enabled = ? WHERE id = ?",
            (int(enabled), whatsapp_number_id),
        )
        conn.commit()


def _row_to_sender(row) -> dict:
    return dict(zip(_SENDER_COLUMNS, row))


def list_voice_transcription_allowed_senders(whatsapp_number_id: int) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {', '.join(_SENDER_COLUMNS)} FROM voice_transcription_allowed_senders "
            "WHERE whatsapp_number_id = ? ORDER BY created_at",
            (whatsapp_number_id,),
        ).fetchall()
        return [_row_to_sender(r) for r in rows]


def get_voice_transcription_allowed_sender(sender_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {', '.join(_SENDER_COLUMNS)} FROM voice_transcription_allowed_senders WHERE id = ?",
            (sender_id,),
        ).fetchone()
        return _row_to_sender(row) if row else None


def add_voice_transcription_allowed_sender(whatsapp_number_id: int, wa_id: str, label: str | None) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO voice_transcription_allowed_senders (whatsapp_number_id, wa_id, label, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (whatsapp_number_id, wa_id, label, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return cur.lastrowid


def delete_voice_transcription_allowed_sender(sender_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM voice_transcription_allowed_senders WHERE id = ?", (sender_id,))
        conn.commit()


def is_voice_transcription_sender_allowed(whatsapp_number_id: int, wa_id: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM voice_transcription_allowed_senders WHERE whatsapp_number_id = ? AND wa_id = ?",
            (whatsapp_number_id, wa_id),
        ).fetchone()
        return row is not None


def record_voice_transcription(
    *, whatsapp_number_id: int | None, conversation_id: int | None, inbound_message_id: int | None, sent: bool,
) -> int:
    """One row per voice note the module claimed (see
    services/voice_transcription_reply.py::maybe_reply_with_transcription),
    regardless of whether the reply actually reached the contact -
    sent=False still counts as "processed" (a real Claude clean-up call
    was attempted, or would have been if credentials were configured),
    same "claimed but not delivered" distinction as ai_reply_log.sent."""
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO voice_transcription_log
                (created_at, whatsapp_number_id, conversation_id, inbound_message_id, sent)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(), whatsapp_number_id, conversation_id,
                inbound_message_id, int(sent),
            ),
        )
        conn.commit()
        return cur.lastrowid


def voice_transcription_counts_by_number(days: int = 30) -> list[dict]:
    """Count of voice notes claimed (request_count) and how many actually
    got a reply delivered (sent_count) per WhatsApp number over the
    trailing `days` - feeds the /usage page's Voice Transcriptions card,
    same per-number shape as storage.usage.send_totals_by_number. Numbers
    with zero requests in the window simply don't appear."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT whatsapp_number_id, COUNT(*) AS request_count, COALESCE(SUM(sent), 0) AS sent_count
            FROM voice_transcription_log
            WHERE created_at >= ? AND whatsapp_number_id IS NOT NULL
            GROUP BY whatsapp_number_id
            ORDER BY request_count DESC
            """,
            (since,),
        ).fetchall()
    return [{"whatsapp_number_id": r[0], "request_count": r[1], "sent_count": r[2]} for r in rows]

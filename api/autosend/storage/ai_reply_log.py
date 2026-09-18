"""storage/ai_reply_log.py

Append-only log of every AI Assistant reply attempt (source='live', a
real inbound message) or keyword auto-reply (source='keyword', 0 tokens),
plus a daily-cap check used to gate live AI replies (keyword replies are
NOT subject to this cap - they're fully independent of the AI pipeline,
see services/ai_reply.py::maybe_generate_ai_reply).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from ._db import _connect


def record_reply(
    *, whatsapp_number_id: int | None, conversation_id: int | None, inbound_message_id: int | None,
    source: str, sent: bool, escalated: bool = False,
    retrieved_entry_ids: list[int] | None = None,
    prompt_tokens: int | None = None, completion_tokens: int | None = None, model: str | None = None,
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO ai_reply_log
                (created_at, whatsapp_number_id, conversation_id, inbound_message_id,
                 retrieved_entry_ids, prompt_tokens, completion_tokens, escalated, sent, source, model)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(), whatsapp_number_id, conversation_id,
                inbound_message_id, json.dumps(retrieved_entry_ids) if retrieved_entry_ids else None,
                prompt_tokens, completion_tokens, int(escalated), int(sent), source, model,
            ),
        )
        conn.commit()
        return cur.lastrowid


def count_replies_today(whatsapp_number_id: int, source: str = "live") -> int:
    """Today = the last 24 hours (rolling, not calendar-day) - simplest
    correct definition for a cap that has to hold regardless of what time
    of day the count is checked. Cutoff computed in Python (same
    isoformat()-with-timezone strings as created_at) rather than SQLite's
    datetime('now', ...), matching storage/limits.py's established
    convention for comparing timestamp strings."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM ai_reply_log WHERE whatsapp_number_id = ? AND source = ? AND created_at >= ?",
            (whatsapp_number_id, source, cutoff),
        ).fetchone()
        return row[0] if row else 0

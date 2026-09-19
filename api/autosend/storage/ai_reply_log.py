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


def reply_token_usage_by_org(days: int) -> list[dict]:
    """Summed input/output tokens and call counts per organisation and
    model over the trailing `days`, most tokens first - feeds the /usage
    page's AI auto-reply report card. Includes both source='live' and
    'playground' calls (both spend real Anthropic tokens), unlike
    count_replies_today()'s daily-cap check, which deliberately excludes
    playground. source='keyword' rows are excluded entirely here too - a
    canned keyword reply never calls Claude, so folding its (always-zero)
    tokens into this card would just add non-AI call counts to what's
    meant to be an AI usage/cost report. ai_reply_log carries no org_id of
    its own (see storage/schema.py's column-pattern note), so
    organisation is resolved via whatsapp_number_id -> whatsapp_numbers ->
    units -> organisations, the same chain a number's own org always sits
    behind. Anthropic's own terminology (and this table's prompt_tokens/
    completion_tokens columns, populated directly from
    response.usage.input_tokens/output_tokens - see services/ai_reply.py)
    is input/output tokens, not OpenAI's prompt/completion - the query
    aliases below use that naming so callers/templates don't have to
    translate."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT
                u.org_id AS org_id,
                org.name AS org_name,
                l.model AS model,
                COUNT(*) AS call_count,
                COALESCE(SUM(l.prompt_tokens), 0) AS input_tokens,
                COALESCE(SUM(l.completion_tokens), 0) AS output_tokens
            FROM ai_reply_log l
            LEFT JOIN whatsapp_numbers wn ON wn.id = l.whatsapp_number_id
            LEFT JOIN units u ON u.id = wn.unit_id
            LEFT JOIN organisations org ON org.id = u.org_id
            WHERE l.created_at >= ? AND l.source IN ('live', 'playground')
            GROUP BY u.org_id, l.model
            ORDER BY (input_tokens + output_tokens) DESC
            """,
            (since,),
        ).fetchall()
        columns = ["org_id", "org_name", "model", "call_count", "input_tokens", "output_tokens"]
        return [dict(zip(columns, r)) for r in rows]


def keyword_reply_counts_by_org(days: int) -> list[dict]:
    """Count of source='keyword' rows per organisation over the trailing
    `days`, most replies first - feeds the /usage page's own keyword
    auto-reply card. Kept separate from reply_token_usage_by_org (which
    excludes 'keyword' rows) since a canned reply spends no tokens and
    isn't an AI cost figure, but orgs still want to see how much traffic
    their keyword replies are handling. Same org resolution path as
    reply_token_usage_by_org (ai_reply_log has no org_id of its own)."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT
                u.org_id AS org_id,
                org.name AS org_name,
                COUNT(*) AS reply_count
            FROM ai_reply_log l
            LEFT JOIN whatsapp_numbers wn ON wn.id = l.whatsapp_number_id
            LEFT JOIN units u ON u.id = wn.unit_id
            LEFT JOIN organisations org ON org.id = u.org_id
            WHERE l.created_at >= ? AND l.source = 'keyword'
            GROUP BY u.org_id
            ORDER BY reply_count DESC
            """,
            (since,),
        ).fetchall()
        columns = ["org_id", "org_name", "reply_count"]
        return [dict(zip(columns, r)) for r in rows]


def reply_counts_by_category(days: int) -> dict[str, int]:
    """AI vs. keyword split of ai_reply_log over the trailing `days`, for
    the /usage page's cross-channel 'Messages by category' card - counts
    actual sends only (sent=1), and excludes source='playground' since a
    playground call never reaches a real contact, so it shouldn't inflate
    a "messages sent" figure. Kept separate from reply_token_usage_by_org
    (an AI cost report, live+playground, per org) - this is a plain
    send-count for a different card."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) FILTER (WHERE source = 'live') AS ai_count,
                COUNT(*) FILTER (WHERE source = 'keyword') AS keyword_count
            FROM ai_reply_log
            WHERE created_at >= ? AND sent = 1
            """,
            (since,),
        ).fetchone()
        return {"ai_auto_reply": row[0] or 0, "keyword_auto_reply": row[1] or 0}


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

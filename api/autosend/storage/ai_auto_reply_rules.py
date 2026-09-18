"""storage/ai_auto_reply_rules.py

Per-WhatsApp-number exact-match keyword auto-replies - fully independent
of the AI Assistant RAG pipeline (see services/ai_reply.py::
maybe_generate_ai_reply, which checks these rules before any AI-specific
gating at all). Matching is exact-whole-message, case-insensitive,
whitespace-trimmed, deliberately not substring/fuzzy - a "new" rule
should not fire on "what's new".
"""

from __future__ import annotations

from datetime import datetime, timezone

from ._db import _connect

_RULE_COLUMNS = ["id", "whatsapp_number_id", "keyword", "response_text", "active", "created_at"]


def _row_to_rule(row) -> dict:
    return dict(zip(_RULE_COLUMNS, row))


def list_rules(whatsapp_number_id: int, active_only: bool = False) -> list[dict]:
    query = f"SELECT {', '.join(_RULE_COLUMNS)} FROM ai_auto_reply_rules WHERE whatsapp_number_id = ?"
    if active_only:
        query += " AND active = 1"
    query += " ORDER BY keyword"
    with _connect() as conn:
        rows = conn.execute(query, (whatsapp_number_id,)).fetchall()
        return [_row_to_rule(r) for r in rows]


def get_rule(rule_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {', '.join(_RULE_COLUMNS)} FROM ai_auto_reply_rules WHERE id = ?", (rule_id,),
        ).fetchone()
        return _row_to_rule(row) if row else None


def create_rule(whatsapp_number_id: int, keyword: str, response_text: str, active: bool = True) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO ai_auto_reply_rules (whatsapp_number_id, keyword, response_text, active, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (whatsapp_number_id, keyword, response_text, int(active), datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return cur.lastrowid


def update_rule(rule_id: int, *, keyword: str, response_text: str, active: bool) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE ai_auto_reply_rules SET keyword = ?, response_text = ?, active = ? WHERE id = ?",
            (keyword, response_text, int(active), rule_id),
        )
        conn.commit()


def delete_rule(rule_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM ai_auto_reply_rules WHERE id = ?", (rule_id,))
        conn.commit()


def find_matching_rule(whatsapp_number_id: int, message_text: str) -> dict | None:
    normalized = (message_text or "").strip().lower()
    if not normalized:
        return None
    for rule in list_rules(whatsapp_number_id, active_only=True):
        if rule["keyword"].strip().lower() == normalized:
            return rule
    return None

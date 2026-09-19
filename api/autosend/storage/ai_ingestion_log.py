"""storage/ai_ingestion_log.py

Append-only token-usage log for the Knowledge Base's ingestion
"FAQ-ification" pass (services/knowledge_ingest.py) - separate from
storage.ai_reply_log since ingestion and live replies use independently
configurable models/costs.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ._db import _connect


def record_ingestion(
    *, org_id: int, unit_id: int | None, source_type: str, source_ref: str | None,
    prompt_tokens: int | None = None, completion_tokens: int | None = None, model: str | None = None,
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO ai_ingestion_log
                (created_at, org_id, unit_id, source_type, source_ref, prompt_tokens, completion_tokens, model)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(), org_id, unit_id, source_type, source_ref,
                prompt_tokens, completion_tokens, model,
            ),
        )
        conn.commit()
        return cur.lastrowid


def ingestion_token_usage_by_org(days: int) -> list[dict]:
    """Summed input/output tokens and call counts per organisation and
    model over the trailing `days`, most tokens first - feeds the /usage
    page's ingestion report card. org_id is direct and NOT NULL on this
    table (see storage/schema.py's column-pattern note), unlike
    knowledge_base_entries' separately nullable unit_id, so there is no
    "global"/unscoped row to special-case here. Anthropic's own
    terminology (and this table's prompt_tokens/completion_tokens
    columns, populated directly from response.usage.input_tokens/
    output_tokens) is input/output tokens, not OpenAI's prompt/
    completion - the query aliases below use that naming so callers/
    templates don't have to translate."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT
                l.org_id AS org_id,
                org.name AS org_name,
                l.model AS model,
                COUNT(*) AS call_count,
                COALESCE(SUM(l.prompt_tokens), 0) AS input_tokens,
                COALESCE(SUM(l.completion_tokens), 0) AS output_tokens
            FROM ai_ingestion_log l
            LEFT JOIN organisations org ON org.id = l.org_id
            WHERE l.created_at >= ?
            GROUP BY l.org_id, l.model
            ORDER BY (input_tokens + output_tokens) DESC
            """,
            (since,),
        ).fetchall()
        columns = ["org_id", "org_name", "model", "call_count", "input_tokens", "output_tokens"]
        return [dict(zip(columns, r)) for r in rows]

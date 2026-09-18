"""storage/ai_ingestion_log.py

Append-only token-usage log for the Knowledge Base's ingestion
"FAQ-ification" pass (services/knowledge_ingest.py) - separate from
storage.ai_reply_log since ingestion and live replies use independently
configurable models/costs.
"""

from __future__ import annotations

from datetime import datetime, timezone

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

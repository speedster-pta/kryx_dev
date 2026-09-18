"""storage/ai_ingestion_settings.py

Platform-wide Anthropic credentials for the Knowledge Base's ingestion
"FAQ-ification" pass (services/knowledge_ingest.py) - a separate singleton
from storage.ai_credentials so ingestion can use a different model/cost
tier than live customer-facing replies.
"""

from __future__ import annotations

from ._db import _connect


def get_ai_ingestion_settings() -> dict | None:
    from autosend import crypto

    with _connect() as conn:
        row = conn.execute(
            "SELECT api_key, model, effort FROM ai_ingestion_settings LIMIT 1"
        ).fetchone()
        if not row:
            return None
        return {
            "api_key": crypto.decrypt_token(row[0]) if row[0] else None,
            "model": row[1],
            "effort": row[2],
        }

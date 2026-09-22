"""storage/ai_ingestion_settings.py

Platform-wide model/effort for the Knowledge Base's ingestion
"FAQ-ification" pass (services/knowledge_ingest.py) - a separate singleton
from storage.ai_credentials so ingestion can use a different model/cost
tier than live customer-facing replies. The Anthropic API key itself is
not duplicated here - every Claude-backed pipeline shares the one
platform-wide key from storage.ai_credentials (see clients.get_anthropic_client()).
"""

from __future__ import annotations

from ._db import _connect


def get_ai_ingestion_settings() -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT model, effort FROM ai_ingestion_settings LIMIT 1"
        ).fetchone()
        if not row:
            return None
        return {
            "model": row[0],
            "effort": row[1],
        }

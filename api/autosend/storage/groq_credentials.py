"""storage/groq_credentials.py

Platform-wide Groq credentials for Whisper transcription of inbound
WhatsApp voice notes (services/audio_transcription.py) - same singleton
pattern as storage.ai_credentials/storage.ai_ingestion_settings.
"""

from __future__ import annotations

from ._db import _connect


def get_groq_credentials() -> dict | None:
    from autosend import crypto

    with _connect() as conn:
        row = conn.execute(
            "SELECT api_key, model FROM groq_credentials LIMIT 1"
        ).fetchone()
        if not row:
            return None
        return {
            "api_key": crypto.decrypt_token(row[0]) if row[0] else None,
            "model": row[1],
        }

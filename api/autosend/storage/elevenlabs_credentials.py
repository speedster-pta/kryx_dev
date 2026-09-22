"""storage/elevenlabs_credentials.py

Platform-wide ElevenLabs credentials for Scribe transcription of inbound
WhatsApp voice notes (services/audio_transcription.py) - the alternative
provider to Groq Whisper, selected via
voice_transcription_settings.transcription_provider. Same singleton
pattern as storage.groq_credentials/storage.ai_credentials.
"""

from __future__ import annotations

from ._db import _connect


def get_elevenlabs_credentials() -> dict | None:
    from autosend import crypto

    with _connect() as conn:
        row = conn.execute(
            "SELECT api_key, model FROM elevenlabs_credentials LIMIT 1"
        ).fetchone()
        if not row:
            return None
        return {
            "api_key": crypto.decrypt_token(row[0]) if row[0] else None,
            "model": row[1],
        }

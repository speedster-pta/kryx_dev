"""storage/transcription_usage.py

Append-only log of every raw speech-to-text provider call
(services/audio_transcription.py), independent of whether the Voice
Transcription module (storage/voice_transcription.py, gated by number
assignment + sender whitelist) goes on to claim the message for a Claude
clean-up reply. A voice note is transcribed via Groq/ElevenLabs for every
org with an audio message, since the transcript also feeds the AI
Assistant reply pipeline (services/ai_reply.py) regardless of whether the
Voice Transcription module is assigned to that number - so provider
usage/cost has to be tracked here, at the actual API-call site, not
inside voice_transcription_log (which only ever sees module-claimed
messages).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ._db import _connect


def record_transcription_call(
    *, whatsapp_number_id: int | None, conversation_id: int | None, inbound_message_id: int | None,
    provider: str, audio_duration_secs: float | None = None, success: bool = True,
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO transcription_provider_log
                (created_at, whatsapp_number_id, conversation_id, inbound_message_id,
                 provider, audio_duration_secs, success)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(), whatsapp_number_id, conversation_id,
                inbound_message_id, provider, audio_duration_secs, int(success),
            ),
        )
        conn.commit()
        return cur.lastrowid


def elevenlabs_usage_by_org(days: int) -> list[dict]:
    """Call count and summed audio duration (seconds) per organisation for
    provider='elevenlabs' calls over the trailing `days` - feeds the
    /usage page's ElevenLabs usage card, since ElevenLabs bills Scribe
    transcription by audio duration, not tokens (see
    services/audio_transcription.py::_transcribe_via_elevenlabs, which
    reads audio_duration_secs straight off the API response). Same
    whatsapp_number_id -> whatsapp_numbers -> units -> organisations
    resolution chain as storage.reply_token_usage_by_org (this table
    carries no org_id of its own either)."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT
                u.org_id AS org_id,
                org.name AS org_name,
                COUNT(*) AS call_count,
                COALESCE(SUM(l.audio_duration_secs), 0) AS total_duration_secs
            FROM transcription_provider_log l
            LEFT JOIN whatsapp_numbers wn ON wn.id = l.whatsapp_number_id
            LEFT JOIN units u ON u.id = wn.unit_id
            LEFT JOIN organisations org ON org.id = u.org_id
            WHERE l.created_at >= ? AND l.provider = 'elevenlabs' AND l.success = 1
            GROUP BY u.org_id
            ORDER BY total_duration_secs DESC
            """,
            (since,),
        ).fetchall()
        columns = ["org_id", "org_name", "call_count", "total_duration_secs"]
        return [dict(zip(columns, r)) for r in rows]

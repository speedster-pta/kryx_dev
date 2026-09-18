"""storage/whatsapp_number_ai_settings.py

Per-WhatsApp-number AI Assistant customisation - a youth-ministry number
can sound different from a congregation's main line. custom_instructions
here is layered on top of (appended after) ai_credentials'
platform-wide custom_instructions at reply time, not a replacement for
it - see services/ai_reply.py::_build_system_prompt.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ._db import _connect


def get_ai_settings(whatsapp_number_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, whatsapp_number_id, custom_instructions, bot_description, handoff_message, "
            "draft_review_enabled, created_at "
            "FROM whatsapp_number_ai_settings WHERE whatsapp_number_id = ?",
            (whatsapp_number_id,),
        ).fetchone()
        if not row:
            return None
        columns = [
            "id", "whatsapp_number_id", "custom_instructions", "bot_description", "handoff_message",
            "draft_review_enabled", "created_at",
        ]
        return dict(zip(columns, row))


def upsert_ai_settings(
    whatsapp_number_id: int, *, custom_instructions: str | None, bot_description: str | None,
    handoff_message: str | None, draft_review_enabled: bool = False,
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO whatsapp_number_ai_settings
                (whatsapp_number_id, custom_instructions, bot_description, handoff_message,
                 draft_review_enabled, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(whatsapp_number_id) DO UPDATE SET
                custom_instructions = excluded.custom_instructions,
                bot_description = excluded.bot_description,
                handoff_message = excluded.handoff_message,
                draft_review_enabled = excluded.draft_review_enabled
            """,
            (whatsapp_number_id, custom_instructions, bot_description, handoff_message,
             int(draft_review_enabled), datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()

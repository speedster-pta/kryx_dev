"""storage/ai_credentials.py

Platform-wide Anthropic credentials for live AI Assistant replies -
singleton table, same shape/reasoning as storage.platform_email's
get_platform_email_settings(). Read fresh on every reply generation
(services/ai_reply.py) rather than cached in this module - clients.py's
get_anthropic_client() is what caches the actual SDK client instance,
same convention as every other client in that registry.
"""

from __future__ import annotations

from ._db import _connect


def get_ai_credentials() -> dict | None:
    from autosend import crypto

    with _connect() as conn:
        row = conn.execute(
            "SELECT api_key, model, effort, custom_instructions, system_prompt "
            "FROM ai_credentials LIMIT 1"
        ).fetchone()
        if not row:
            return None
        return {
            "api_key": crypto.decrypt_token(row[0]) if row[0] else None,
            "model": row[1],
            "effort": row[2],
            "custom_instructions": row[3],
            "system_prompt": row[4],
        }

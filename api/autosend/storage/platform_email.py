"""
storage/platform_email.py

Platform-wide outbound SMTP credentials (currently Mailtrap) - singleton
table, same shape/reasoning as storage.units.get_meta_platform_settings
(one row regardless of tenant count, since this is the platform's own mail
relay, not a customer credential). Read by integrations/mailer.py fresh on
every send rather than cached, unlike clients.py's WhatsApp/PCO registry -
transactional email is low-volume enough that a per-send DB read is cheap,
and it means an edited credential takes effect immediately, no app restart
needed.
"""

from __future__ import annotations

from ._db import _connect


def get_platform_email_settings() -> dict | None:
    from autosend import crypto

    with _connect() as conn:
        row = conn.execute(
            "SELECT smtp_host, smtp_port, smtp_username, smtp_password, from_address "
            "FROM platform_email_settings LIMIT 1"
        ).fetchone()
        if not row:
            return None
        return {
            "smtp_host": row[0],
            "smtp_port": row[1],
            "smtp_username": row[2],
            "smtp_password": crypto.decrypt_token(row[3]) if row[3] else None,
            "from_address": row[4],
        }

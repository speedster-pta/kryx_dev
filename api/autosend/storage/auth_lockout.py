"""
Brute-force login protection, shared by the /login page that fronts both
the bulk-campaign UI and (indirectly) the SQLAdmin panel.
"""

from datetime import datetime, timezone

from ._db import _connect


def get_lockout(identifier: str) -> str | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT locked_until FROM login_attempts WHERE identifier = ?", (identifier,)
        ).fetchone()
        return row[0] if row and row[0] else None


def record_login_attempt(identifier: str, failed_count: int, locked_until: str | None) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO login_attempts (identifier, failed_count, last_attempt_at, locked_until)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(identifier) DO UPDATE SET
                failed_count = excluded.failed_count,
                last_attempt_at = excluded.last_attempt_at,
                locked_until = excluded.locked_until
            """,
            (identifier, failed_count, datetime.now(timezone.utc).isoformat(), locked_until),
        )


def get_login_attempt_row(identifier: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT failed_count, last_attempt_at FROM login_attempts WHERE identifier = ?",
            (identifier,),
        ).fetchone()
        return {"failed_count": row[0], "last_attempt_at": row[1]} if row else None


def clear_login_attempts(identifier: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM login_attempts WHERE identifier = ?", (identifier,))

"""
Idempotency guards so a poll or webhook delivery never double-sends.

Two things live here:
1. processed_registrations / processed_form_submissions - which IDs
   we've already messaged ('sent') or attempted and failed on ('failed').
2. signup_watermark - per-signup pointer to the newest registration ID
   we've already looked at, so each poll only pages back to where it
   left off instead of re-walking a signup's whole history.
"""

from datetime import datetime, timezone

from ._db import _connect


def get_signup_watermark(signup_id: str) -> str | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT last_seen_registration_id FROM signup_watermark WHERE signup_id = ?",
            (signup_id,),
        ).fetchone()
        return row[0] if row else None


def set_signup_watermark(signup_id: str, registration_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO signup_watermark (signup_id, last_seen_registration_id, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(signup_id) DO UPDATE SET
                last_seen_registration_id = excluded.last_seen_registration_id,
                updated_at = excluded.updated_at
            """,
            (signup_id, registration_id, datetime.now(timezone.utc).isoformat()),
        )


def get_recent_failures(limit: int = 50) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT registration_id, signup_id, processed_at, detail
            FROM processed_registrations
            WHERE status = 'failed'
            ORDER BY processed_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            {
                "registration_id": r[0],
                "signup_id": r[1],
                "processed_at": r[2],
                "detail": r[3],
            }
            for r in rows
        ]


def get_recent_form_failures(limit: int = 50) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT submission_id, person_id, processed_at, detail
            FROM processed_form_submissions
            WHERE status = 'failed'
            ORDER BY processed_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            {
                "submission_id": r[0],
                "person_id": r[1],
                "processed_at": r[2],
                "detail": r[3],
            }
            for r in rows
        ]


def is_processed(registration_id: str) -> bool:
    """Only counts as processed if it previously SENT successfully.
    A prior 'failed' row does not block a retry on the next poll."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_registrations WHERE registration_id = ? AND status = 'sent'",
            (registration_id,),
        ).fetchone()
        return row is not None


def mark_processed(registration_id: str, signup_id: str, status: str, detail: str = "") -> None:
    """status is 'sent' or 'failed'."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO processed_registrations (registration_id, signup_id, processed_at, status, detail)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(registration_id) DO UPDATE SET
                processed_at = excluded.processed_at,
                status = excluded.status,
                detail = excluded.detail
            """,
            (registration_id, signup_id, datetime.now(timezone.utc).isoformat(), status, detail),
        )


def is_form_submission_processed(submission_id: str) -> bool:
    """Only counts as processed if it previously SENT successfully, same
    semantics as is_processed() above - guards against PCO re-delivering
    the same webhook (e.g. after a timeout)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_form_submissions WHERE submission_id = ? AND status = 'sent'",
            (submission_id,),
        ).fetchone()
        return row is not None


def mark_form_submission_processed(
    submission_id: str, person_id: str, status: str, detail: str = ""
) -> None:
    """status is 'sent' or 'failed'."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO processed_form_submissions (submission_id, person_id, processed_at, status, detail)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(submission_id) DO UPDATE SET
                processed_at = excluded.processed_at,
                status = excluded.status,
                detail = excluded.detail
            """,
            (submission_id, person_id, datetime.now(timezone.utc).isoformat(), status, detail),
        )

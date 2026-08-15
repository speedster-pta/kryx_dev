"""
Bulk WhatsApp campaigns: creation, recipients, progress tracking,
scheduling, and cancellation.
"""

import json
from datetime import datetime, timezone

from ._db import _connect


def create_campaign(user_id: int, unit_id: int, whatsapp_number_id: int,
                     template_name: str, language: str, total: int,
                     scheduled_at: str | None = None, payload: dict | None = None) -> int:
    """scheduled_at (ISO timestamp) and payload (the CSV rows + column
    mappings needed to run the send later) go together - only set both when
    the campaign shouldn't send immediately. Immediate sends (the original
    behaviour) leave both as None and start at status='running'."""
    status = "scheduled" if scheduled_at else "running"
    payload_json = json.dumps(payload) if payload is not None else None
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO campaigns
                (user_id, unit_id, whatsapp_number_id, template_name, language,
                 status, total, scheduled_at, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, unit_id, whatsapp_number_id, template_name, language,
             status, total, scheduled_at, payload_json, datetime.now(timezone.utc).isoformat()),
        )
        return cur.lastrowid


def add_campaign_recipient(campaign_id: int, phone: str) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO campaign_recipients (campaign_id, phone, status, updated_at) VALUES (?, ?, 'pending', ?)",
            (campaign_id, phone, datetime.now(timezone.utc).isoformat()),
        )
        return cur.lastrowid


def update_campaign_recipient(recipient_id: int, status: str, detail: str = "") -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE campaign_recipients SET status=?, detail=?, updated_at=? WHERE id=?",
            (status, detail, datetime.now(timezone.utc).isoformat(), recipient_id),
        )


def update_campaign_progress(campaign_id: int, sent: int, failed: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE campaigns SET sent=?, failed=? WHERE id=?",
            (sent, failed, campaign_id),
        )


def finalize_campaign_status(campaign_id: int, status: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE campaigns SET status=? WHERE id=?", (status, campaign_id))


def get_campaign_status(campaign_id: int) -> str | None:
    """Cheap status-only read, used by the send loop to check for a
    cancellation request without pulling the whole campaign + recipients."""
    with _connect() as conn:
        row = conn.execute("SELECT status FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
        return row[0] if row else None


def get_campaign_payload(campaign_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT payload_json FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
        return json.loads(row[0]) if row and row[0] else None


def set_campaign_payload(campaign_id: int, payload: dict) -> None:
    """Persists (or re-persists) the not-yet-sent rows + column mappings for
    a campaign. Previously only scheduled campaigns ever had a payload_json
    (set at creation, cleared once the scheduler kicked it off). Now also
    used to pause an *immediate* campaign mid-run when it hits the 24h
    messaging limit - _run_campaign calls this with the remaining rows
    before stopping, so launch_scheduled_campaign's resume path (repurposed
    for throttle-resume too) has something to pick back up."""
    with _connect() as conn:
        conn.execute(
            "UPDATE campaigns SET payload_json = ? WHERE id = ?",
            (json.dumps(payload), campaign_id),
        )


def clear_campaign_payload(campaign_id: int) -> None:
    """Called once a scheduled campaign has started running - no reason to
    keep the CSV data (which may contain phone numbers/names) sitting in
    the DB after it's served its purpose."""
    with _connect() as conn:
        conn.execute("UPDATE campaigns SET payload_json = NULL WHERE id = ?", (campaign_id,))


def request_campaign_cancel(campaign_id: int) -> bool:
    """Marks a running or scheduled campaign for cancellation. Returns False
    (no-op) if the campaign is already finished or already cancelling, so
    callers can tell the user nothing happened rather than silently no-op."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE campaigns SET status='cancelling' WHERE id=? AND status IN ('running','scheduled')",
            (campaign_id,),
        )
        return cur.rowcount > 0


def list_pending_scheduled_campaigns() -> list[dict]:
    """Campaigns still waiting to fire - read on app startup to re-register
    scheduler jobs that were lost when the container restarted."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM campaigns WHERE status = 'scheduled' AND scheduled_at IS NOT NULL"
        ).fetchall()
        columns = [d[0] for d in conn.execute("SELECT * FROM campaigns LIMIT 0").description]
        return [dict(zip(columns, r)) for r in rows]


def list_campaigns(unit_ids: list[int] | None, limit: int = 50) -> list[dict]:
    """unit_ids=None means unrestricted (superadmin)."""
    from .scoping import unit_scope_clause

    with _connect() as conn:
        base = """
            SELECT c.*, u.name AS unit_name, su.username, n.label AS number_label
            FROM campaigns c
            JOIN units u ON u.id = c.unit_id
            JOIN users su ON su.id = c.user_id
            LEFT JOIN whatsapp_numbers n ON n.id = c.whatsapp_number_id
        """
        scope = unit_scope_clause("c.unit_id", unit_ids, joiner="WHERE")
        if scope is None:
            return []
        clause, scope_params = scope
        rows = conn.execute(
            base + clause + " ORDER BY c.created_at DESC LIMIT ?",
            (*scope_params, limit),
        ).fetchall()
        columns = [d[0] for d in conn.execute("SELECT * FROM campaigns LIMIT 0").description]
        extra = ["unit_name", "username", "number_label"]
        return [dict(zip(columns + extra, r)) for r in rows]


def get_campaign(campaign_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
        if not row:
            return None
        columns = [d[0] for d in conn.execute("SELECT * FROM campaigns LIMIT 0").description]
        campaign = dict(zip(columns, row))
        recipients = conn.execute(
            "SELECT phone, status, detail, updated_at FROM campaign_recipients WHERE campaign_id = ?",
            (campaign_id,),
        ).fetchall()
        campaign["recipients"] = [
            {"phone": r[0], "status": r[1], "detail": r[2], "updated_at": r[3]} for r in recipients
        ]
        return campaign


def list_throttled_campaigns() -> list[dict]:
    """Read by the scheduler's periodic throttle-recheck job."""
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM campaigns WHERE status = 'throttled'").fetchall()
        columns = [d[0] for d in conn.execute("SELECT * FROM campaigns LIMIT 0").description]
        return [dict(zip(columns, r)) for r in rows]
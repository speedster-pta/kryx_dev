"""
Staff user accounts and their unit scoping.
"""

from datetime import datetime, timezone

from ._db import _connect


def get_staff_user(username: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM staff_users WHERE username = ? AND active = 1",
            (username,),
        ).fetchone()
        if not row:
            return None
        columns = [d[0] for d in conn.execute("SELECT * FROM staff_users LIMIT 0").description]
        user = dict(zip(columns, row))
        cong_rows = conn.execute(
            "SELECT unit_id FROM staff_user_units WHERE staff_user_id = ?",
            (user["id"],),
        ).fetchall()
        user["unit_ids"] = [r[0] for r in cong_rows]
        return user


def get_staff_user_by_id(user_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM staff_users WHERE id = ? AND active = 1", (user_id,)
        ).fetchone()
        if not row:
            return None
        columns = [d[0] for d in conn.execute("SELECT * FROM staff_users LIMIT 0").description]
        return dict(zip(columns, row))


def update_staff_password(user_id: int, password_hash: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE staff_users SET password_hash = ? WHERE id = ?",
            (password_hash, user_id),
        )


def create_staff_user(username: str, password_hash: str, is_superadmin: bool = False) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO staff_users (username, password_hash, is_superadmin, created_at) VALUES (?,?,?,?)",
            (username, password_hash, int(is_superadmin), datetime.now(timezone.utc).isoformat()),
        )
        return cur.lastrowid


def assign_staff_unit(staff_user_id: int, unit_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO staff_user_units (staff_user_id, unit_id) VALUES (?,?)",
            (staff_user_id, unit_id),
        )

"""
Staff user accounts and their unit scoping.
"""

from datetime import datetime, timezone

from ._db import _connect


def get_user(username: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ? AND active = 1",
            (username,),
        ).fetchone()
        if not row:
            return None
        columns = [d[0] for d in conn.execute("SELECT * FROM users LIMIT 0").description]
        user = dict(zip(columns, row))
        cong_rows = conn.execute(
            "SELECT unit_id FROM user_units WHERE user_id = ?",
            (user["id"],),
        ).fetchall()
        user["unit_ids"] = [r[0] for r in cong_rows]
        return user


def get_user_by_id(user_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE id = ? AND active = 1", (user_id,)
        ).fetchone()
        if not row:
            return None
        columns = [d[0] for d in conn.execute("SELECT * FROM users LIMIT 0").description]
        return dict(zip(columns, row))


def update_staff_password(user_id: int, password_hash: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (password_hash, user_id),
        )


def update_staff_username(user_id: int, username: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET username = ? WHERE id = ?",
            (username, user_id),
        )


def update_staff_email(user_id: int, email: str) -> None:
    """Also clears email_verified_at - a changed address hasn't been proven
    reachable yet, so the caller must re-send a verification link (see
    web/account_router.py's /api/account/email)."""
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET email = ?, email_verified_at = NULL WHERE id = ?",
            (email, user_id),
        )


def create_user(
    username: str,
    password_hash: str,
    is_superadmin: bool = False,
    org_id: int | None = None,
    is_org_admin: bool = False,
    email: str | None = None,
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, is_superadmin, is_org_admin, org_id, email, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                username,
                password_hash,
                int(is_superadmin),
                int(is_org_admin),
                org_id,
                email,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        return cur.lastrowid


def assign_staff_unit(user_id: int, unit_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO user_units (user_id, unit_id) VALUES (?,?)",
            (user_id, unit_id),
        )


def count_active_org_admins(org_id: int) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM users WHERE org_id = ? AND is_org_admin = 1 AND active = 1",
            (org_id,),
        ).fetchone()
        return row[0] if row else 0


def count_active_org_users(org_id: int) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM users WHERE org_id = ? AND active = 1",
            (org_id,),
        ).fetchone()
        return row[0] if row else 0

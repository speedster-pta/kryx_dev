"""
Terms & Conditions acceptance audit trail.

One row per acceptance event, written once at self-serve signup (see
web/signup_router.py) when the new org-admin ticks the required checkbox.
Append-only - never updated or deleted - so it stays usable as evidence
that a specific user agreed to a specific version of the Terms, on behalf
of a specific organisation, at a specific time.
"""

from datetime import datetime, timezone

from ._db import _connect


def record_terms_acceptance(
    org_id: int,
    user_id: int,
    terms_version: str,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO terms_acceptances "
            "(org_id, user_id, terms_version, accepted_at, ip_address, user_agent) "
            "VALUES (?,?,?,?,?,?)",
            (
                org_id,
                user_id,
                terms_version,
                datetime.now(timezone.utc).isoformat(),
                ip_address,
                user_agent,
            ),
        )
        return cur.lastrowid


def get_terms_acceptances_for_org(org_id: int) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM terms_acceptances WHERE org_id = ? ORDER BY accepted_at DESC",
            (org_id,),
        ).fetchall()
        columns = [d[0] for d in conn.execute("SELECT * FROM terms_acceptances LIMIT 0").description]
        return [dict(zip(columns, row)) for row in rows]

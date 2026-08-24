"""
storage/organisations.py

CRUD for the organisations table. Platform-admin-facing (org
provisioning) rather than per-org-users-facing.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from autosend.storage._db import _connect as get_conn


@dataclass(frozen=True)
class Organisation:
    id: int
    name: str
    slug: str
    active: bool
    created_at: str


def _row_to_org(row: sqlite3.Row) -> Organisation:
    # _connect() yields plain tuple rows (no sqlite3.Row row_factory), same
    # as every other module in this package - positional, matching the
    # organisations table's column order (id, name, slug, active, created_at).
    return Organisation(
        id=row[0], name=row[1], slug=row[2], active=bool(row[3]), created_at=row[4],
    )


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "org"


def generate_unique_slug(name: str) -> str:
    """organisations.slug is globally UNIQUE (unlike units.slug, which is
    only unique per-org) - every caller that creates an organisation from
    a user-supplied name (public /signup, superadmin "create organisation")
    needs to handle collisions the same way, by appending -2, -3, ... until
    free. Centralised here rather than duplicated per caller."""
    base = _slugify(name)
    slug = base
    suffix = 2
    while get_organisation_by_slug(slug) is not None:
        slug = f"{base}-{suffix}"
        suffix += 1
    return slug


def create_organisation(name: str, slug: str, active: bool = True) -> Organisation:
    """Every organisation must have at least one unit (see
    admin_views.UnitAdmin.delete_model's matching last-unit guard) - so
    the default "Main" unit is created in the same transaction as the
    org itself, rather than left to a separate call a caller might skip.
    "Main" rather than repeating the org's own name: users who later add
    a second unit would otherwise end up with a unit confusingly named
    after the whole organisation.

    active defaults to True (superadmin/CLI creation) - public self-serve
    signup (web/signup_router.py) passes active=False explicitly, since
    the platform is a paid product and a fresh signup shouldn't be able
    to send until a superadmin activates it (see storage.is_org_active
    and its callers for what "inactive" actually blocks)."""
    with get_conn() as conn:
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "INSERT INTO organisations (name, slug, active, created_at) VALUES (?, ?, ?, ?)",
            (name, slug, int(active), now),
        )
        org_id = cur.lastrowid
        # slug is "main" for every org's default unit (see docstring) and
        # is only unique per-org - webhook_slug (the globally-unique,
        # random identifier actual webhook URLs key off, see
        # get_unit_by_webhook_slug) is deliberately left NULL here rather
        # than generated up front: a fresh org hasn't been sold the PCO
        # module yet, so there's nothing to key a webhook off. It's
        # minted lazily by storage.units.ensure_webhook_slug() once the
        # org is actually granted the module.
        conn.execute(
            "INSERT INTO units (org_id, slug, name, created_at) VALUES (?, ?, ?, ?)",
            (org_id, "main", "Main", now),
        )
        row = conn.execute("SELECT * FROM organisations WHERE id = ?", (org_id,)).fetchone()
        return _row_to_org(row)


def get_organisation(org_id: int) -> Organisation | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM organisations WHERE id = ?", (org_id,)).fetchone()
        return _row_to_org(row) if row else None


def get_organisation_by_slug(slug: str) -> Organisation | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM organisations WHERE slug = ?", (slug,)).fetchone()
        return _row_to_org(row) if row else None


def list_organisations(active_only: bool = True) -> list[Organisation]:
    with get_conn() as conn:
        if active_only:
            rows = conn.execute("SELECT * FROM organisations WHERE active = 1 ORDER BY name").fetchall()
        else:
            rows = conn.execute("SELECT * FROM organisations ORDER BY name").fetchall()
        return [_row_to_org(r) for r in rows]


def deactivate_organisation(org_id: int) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE organisations SET active = 0 WHERE id = ?", (org_id,))


def activate_organisation(org_id: int) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE organisations SET active = 1 WHERE id = ?", (org_id,))


def is_org_active(org_id: int | None) -> bool:
    """Single choke point for "can this org actually send messages right
    now" - checked at every send-triggering point (bulk campaigns,
    scheduled/throttled campaign resume, serving reminders, PCO
    registration/form confirmations, email-to-WhatsApp) but deliberately
    NOT inside storage.modules.is_enabled(), which also gates whether an
    org can see/configure its integrations - an inactive (e.g.
    non-paying) org must still be able to set up numbers and provision
    integrations, just not send through them. org_id=None (superadmin
    context with no owning org) is treated as active - callers should
    already be routing superadmins around any org-scoped send anyway."""
    if org_id is None:
        return True
    org = get_organisation(org_id)
    return org is not None and org.active


def is_org_email_verified(org_id: int) -> bool:
    """True once at least one org-admin in this org has confirmed their
    email address via /signup/verify (see storage/email_verification.py).
    Deliberately scoped to org-admins, not any user - a verified plain
    staff member shouldn't be enough to satisfy this. Checked alongside a
    successful payment before activate_organisation() is actually called
    (billing/engine.py's two payment-success call sites), so a
    paying-but-unverified org doesn't go active until both are true - an
    independent condition from is_org_active itself, same relationship as
    the payment gate described in that function's own docstring."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM users WHERE org_id = ? AND is_org_admin = 1 AND email_verified_at IS NOT NULL LIMIT 1",
            (org_id,),
        ).fetchone()
        return row is not None


def update_organisation_name(org_id: int, name: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE organisations SET name = ? WHERE id = ?", (name, org_id))

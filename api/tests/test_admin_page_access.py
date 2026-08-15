"""Regression tests for two admin_pages.py bugs found in a cleanup audit:

1. WabaUsageView.is_accessible/is_visible correctly restrict the nav item
   to superadmins, but per this project's own documented gotcha
   (BaseView.@expose routes are never auto-guarded by SQLAdmin), the
   /usage route itself had no inline check - any logged-in staff user
   could reach it directly and see cross-org WhatsApp volume data.
2. _scoped_unit_ids() read the raw session unit_ids for non-superadmins,
   which under-scopes org-admins (whose effective scope is "every unit in
   my org", only obtainable via web.auth.resolve_unit_ids()) - so the
   Automations/History pages would show them little or nothing.
"""
from autosend.admin_pages import _scoped_unit_ids
from autosend.web.auth import resolve_unit_ids


class _FakeRequest:
    def __init__(self, session: dict):
        self.session = session


class TestWabaUsageViewAccess:
    """/usage - superadmin only, enforced both by nav visibility and (after
    the fix) by the route handler itself."""

    def test_plain_staff_forbidden(self, client, login_as, tenants):
        tenant_a, _ = tenants
        login_as(client, tenant_a.staff_username)
        resp = client.get("/usage")
        assert resp.status_code == 403

    def test_org_admin_forbidden(self, client, login_as, tenants):
        tenant_a, _ = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/usage")
        assert resp.status_code == 403

    def test_superadmin_allowed(self, client, login_as, tenants, superadmin_username):
        login_as(client, superadmin_username)
        resp = client.get("/usage")
        assert resp.status_code == 200


class TestScopedUnitIdsResolution:
    """_scoped_unit_ids() must match web.auth.resolve_unit_ids()'s
    contract for every non-superadmin session, not read session["unit_ids"]
    raw (which is only ever populated for plain staff, not org-admins)."""

    def test_org_admin_gets_full_org_scope_not_raw_session_value(self, tenants):
        tenant_a, _ = tenants
        session = {
            "is_superadmin": False,
            "is_org_admin": True,
            "org_id": tenant_a.org_id,
            # Deliberately empty/stale, as a real org-admin session's
            # unit_ids key would be for a plain-staff login path.
            "unit_ids": [],
        }
        request = _FakeRequest(session)

        assert _scoped_unit_ids(request) == resolve_unit_ids(session)
        assert _scoped_unit_ids(request) != []

    def test_plain_staff_uses_session_assignment(self, tenants):
        tenant_a, _ = tenants
        session = {
            "is_superadmin": False,
            "is_org_admin": False,
            "org_id": tenant_a.org_id,
            "unit_ids": [tenant_a.unit_id],
        }
        request = _FakeRequest(session)

        assert _scoped_unit_ids(request) == [tenant_a.unit_id]

    def test_superadmin_gets_unrestricted_none(self):
        session = {"is_superadmin": True}
        request = _FakeRequest(session)

        assert _scoped_unit_ids(request) is None

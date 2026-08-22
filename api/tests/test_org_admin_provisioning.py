"""Positive-path coverage for org-admin self-service provisioning:
adding units, WhatsApp numbers, and users (with unit assignment) within
their own organisation.

test_cross_org_isolation.py already covers the negative path thoroughly
(cross-org reads/edits/deletes/creates get rejected) but every one of its
"create" tests only exercises a *rejected* create (smuggled/other-org id).
Nothing actually asserted that a legitimate, in-scope create by an
org-admin succeeds - this file closes that gap.
"""
from sqlalchemy.orm import Session

from autosend.admin_models import Unit, User, WhatsAppNumber, engine


class TestOrgAdminCanProvisionOwnOrg:
    def test_org_admin_can_create_a_unit(self, client, login_as, tenants, grant_unlimited_capacity):
        tenant_a, _tenant_b = tenants
        # Standard entitlement is 1 unit (see billing/entitlements.py) and
        # this org already has one from the tenants fixture - this test is
        # about the provisioning permission boundary, not the plan limit,
        # so give it enough headroom to add a second one.
        grant_unlimited_capacity(tenant_a.org_id)
        login_as(client, tenant_a.org_admin_username)

        resp = client.post(
            "/unit/create",
            data={"organisation": str(tenant_a.org_id), "name": "Second Unit"},
            follow_redirects=False,
        )
        assert resp.status_code == 302

        with Session(engine) as session:
            unit = session.query(Unit).filter(Unit.name == "Second Unit").one_or_none()
        assert unit is not None
        assert unit.org_id == tenant_a.org_id

    def test_org_admin_can_create_a_whatsapp_number_for_own_unit(self, client, login_as, tenants, grant_unlimited_capacity):
        tenant_a, _tenant_b = tenants
        # Same reasoning as test_org_admin_can_create_a_unit above -
        # standard entitlement is 1 WhatsApp number and this org already
        # has one.
        grant_unlimited_capacity(tenant_a.org_id)
        login_as(client, tenant_a.org_admin_username)

        resp = client.post(
            "/whatsapp-numbers/create",
            data={
                "unit": str(tenant_a.unit_id),
                "label": "Second Number",
                "phone_number_id": "phone-second-number",
                "send_delay_seconds": "0",
                "send_concurrency": "10",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302

        with Session(engine) as session:
            number = (
                session.query(WhatsAppNumber)
                .filter(WhatsAppNumber.phone_number_id == "phone-second-number")
                .one_or_none()
            )
        assert number is not None
        assert number.unit_id == tenant_a.unit_id

    def test_org_admin_can_create_a_user_assigned_to_own_unit(self, client, login_as, tenants, grant_unlimited_capacity):
        tenant_a, _tenant_b = tenants
        # Same reasoning as test_org_admin_can_create_a_unit above -
        # standard entitlement is 1 user and this org already has two
        # (the seeded staff + org-admin).
        grant_unlimited_capacity(tenant_a.org_id)
        login_as(client, tenant_a.org_admin_username)

        resp = client.post(
            "/users/create",
            data={
                "organisation": str(tenant_a.org_id),
                "username": "new-team-member",
                "password_hash": "some-password-123",
                "units": [str(tenant_a.unit_id)],
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302

        with Session(engine) as session:
            user = session.query(User).filter(User.username == "new-team-member").one_or_none()
            assert user is not None
            assert user.org_id == tenant_a.org_id
            assert not user.is_superadmin
            assigned_unit_ids = {u.id for u in user.units}
        assert assigned_unit_ids == {tenant_a.unit_id}

    def test_org_admin_can_reassign_an_existing_user_to_a_new_unit(self, client, login_as, tenants):
        """Covers "assign users to units" for an existing user, not just
        at creation time."""
        tenant_a, _tenant_b = tenants

        with Session(engine) as session:
            extra_unit = Unit(
                org_id=tenant_a.org_id, slug="extra-unit", name="Extra Unit", active=True,
                created_at="2026-01-01T00:00:00+00:00",
            )
            session.add(extra_unit)
            session.commit()
            extra_unit_id = extra_unit.id

        login_as(client, tenant_a.org_admin_username)
        resp = client.post(
            f"/users/edit/{tenant_a.staff_id}",
            data={
                "organisation": str(tenant_a.org_id),
                "username": tenant_a.staff_username,
                "units": [str(extra_unit_id)],
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302

        with Session(engine) as session:
            user = session.get(User, tenant_a.staff_id)
            assigned_unit_ids = {u.id for u in user.units}
        assert assigned_unit_ids == {extra_unit_id}

"""Every organisation must always have at least one unit - see
storage.organisations.create_organisation (auto-provisions a default
"Main" unit in the same transaction as the org) and
admin_views.UnitAdmin.delete_model (refuses to delete an org's last
remaining unit). This suite covers both ends of that invariant.
"""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from autosend import storage
from autosend.admin_models import Unit, engine


def _get_units(org_id):
    with Session(engine) as session:
        return session.query(Unit).filter(Unit.org_id == org_id).all()


class TestOrganisationCreationProvisionsDefaultUnit:
    def test_new_organisation_gets_exactly_one_unit_named_main(self):
        org = storage.create_organisation("Test Org", "test-org-provisioning")
        units = _get_units(org.id)
        assert len(units) == 1
        assert units[0].name == "Main"


class TestLastUnitCannotBeDeleted:
    def test_deleting_an_orgs_only_unit_is_rejected(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        # sqladmin's own delete route swallows whatever delete_model raises
        # into a 200 response with an embedded error param (see
        # test_cross_org_isolation.py's identical caveat) - the only
        # reliable assertion is that the row still exists afterwards.
        client.delete(f"/unit/delete?pks={tenant_a.unit_id}")
        assert _get_units(tenant_a.org_id)[0].id == tenant_a.unit_id

    def test_deleting_a_non_last_unit_still_succeeds(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        with Session(engine) as session:
            extra = Unit(
                org_id=tenant_a.org_id,
                slug="extra",
                name="Extra Unit",
                active=True,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            session.add(extra)
            session.commit()
            extra_id = extra.id

        login_as(client, tenant_a.org_admin_username)
        client.delete(f"/unit/delete?pks={extra_id}")
        remaining_ids = {u.id for u in _get_units(tenant_a.org_id)}
        assert remaining_ids == {tenant_a.unit_id}

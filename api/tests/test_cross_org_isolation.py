"""Cross-org / cross-unit isolation tests for the SQLAdmin CRUD views.

The multi-org security boundary is enforced by hand-reasoned checks
scattered across ScopedModelView (admin_scoping.py) and the per-view
overrides in admin_views.py (UserAdmin, PCOOrganizationSettingsAdmin).
There was no automated coverage for any of it. This suite spins up two
independent organisations (each with its own unit, WhatsApp number, PCO
settings row, plain unit-scoped staff user and org-admin user) and
attacks the boundary the way a logged-in-but-unauthorized staff member
actually could: guessing another tenant's row id in an edit/details/delete
URL, reading list pages for leaked rows, and submitting create/edit forms
with another tenant's id in a relationship field.

Every `tenants` fixture call seeds a fresh, uuid-tagged org pair, so tests
can run in any order against the one long-lived sqlite file without
stepping on each other (see conftest.py).
"""
from sqlalchemy.orm import Session

from autosend.admin_models import PCOOrganizationSettings, Unit, User, WhatsAppNumber, engine


def _get(model, pk):
    with Session(engine) as session:
        return session.get(model, pk)


class TestUnitAdmin:
    """/unit/* - superadmin or org-admin only; unit_field='id' scoping."""

    def test_list_excludes_other_org_units(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/unit/list")
        assert resp.status_code == 200
        assert tenant_a.unit_name in resp.text
        assert tenant_b.unit_name not in resp.text

    def test_details_blocked_for_other_org_unit(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.get(f"/unit/details/{tenant_b.unit_id}")
        assert resp.status_code == 404

    def test_edit_page_blocked_for_other_org_unit(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.get(f"/unit/edit/{tenant_b.unit_id}")
        assert resp.status_code == 404

    def test_update_blocked_for_guessed_pk_of_other_org_unit(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.post(f"/unit/edit/{tenant_b.unit_id}", data={"name": "Renamed by attacker"})
        assert resp.status_code == 404
        assert _get(Unit, tenant_b.unit_id).name == tenant_b.unit_name

    def test_delete_blocked_for_guessed_pk_of_other_org_unit(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        # sqladmin's own delete route fetches the row by raw pk before
        # calling delete_model, and swallows whatever delete_model raises
        # into a 200 response with an embedded error param - so the only
        # reliable assertion here is that the row still exists afterwards,
        # not the HTTP status code.
        client.delete(f"/unit/delete?pks={tenant_b.unit_id}")
        assert _get(Unit, tenant_b.unit_id) is not None

    def test_create_dropdown_hides_other_orgs(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/unit/create")
        assert resp.status_code == 200
        assert tenant_b.org_name not in resp.text

    def test_plain_staff_cannot_reach_unit_admin(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.staff_username)
        resp = client.get("/unit/list")
        assert resp.status_code == 403

    def test_superadmin_sees_both_orgs(self, client, login_as, tenants, superadmin_username):
        # Superadmin's list_query is unfiltered (sees every org's units,
        # across every test that has run in this session against the one
        # shared sqlite file) - pageSize=100 keeps this test's own two
        # rows from being paginated off page 1 by everything else's.
        tenant_a, tenant_b = tenants
        login_as(client, superadmin_username)
        resp = client.get("/unit/list", params={"pageSize": 100})
        assert resp.status_code == 200
        assert tenant_a.unit_name in resp.text
        assert tenant_b.unit_name in resp.text


class TestWhatsAppNumberAdmin:
    """/whatsapp-numbers/* - ScopedModelView, default unit_field='unit_id',
    open to any logged-in staff (not just org admins)."""

    def test_list_excludes_other_units_numbers(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        login_as(client, tenant_a.staff_username)
        resp = client.get("/whatsapp-numbers/list")
        assert resp.status_code == 200
        assert tenant_a.number_label in resp.text
        assert tenant_b.number_label not in resp.text

    def test_details_blocked_for_other_units_number(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        login_as(client, tenant_a.staff_username)
        resp = client.get(f"/whatsapp-numbers/details/{tenant_b.number_id}")
        assert resp.status_code == 404

    def test_edit_page_blocked_for_other_units_number(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        login_as(client, tenant_a.staff_username)
        resp = client.get(f"/whatsapp-numbers/edit/{tenant_b.number_id}")
        assert resp.status_code == 404

    def test_update_blocked_for_guessed_pk_of_other_units_number(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        login_as(client, tenant_a.staff_username)
        resp = client.post(f"/whatsapp-numbers/edit/{tenant_b.number_id}", data={"label": "Hijacked"})
        assert resp.status_code == 404
        assert _get(WhatsAppNumber, tenant_b.number_id).label == tenant_b.number_label

    def test_delete_blocked_for_guessed_pk_of_other_units_number(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        login_as(client, tenant_a.staff_username)
        client.delete(f"/whatsapp-numbers/delete?pks={tenant_b.number_id}")
        assert _get(WhatsAppNumber, tenant_b.number_id) is not None

    def test_create_form_rejects_other_units_id(self, client, login_as, tenants):
        """The unit picker on the create form is pre-filtered to the
        caller's own unit(s) (ScopedModelView.scaffold_form); WTForms'
        QuerySelectField.pre_validate rejects any submitted pk that isn't
        in that filtered choice list. A crafted POST naming another
        tenant's unit id should never create a row under it."""
        tenant_a, tenant_b = tenants
        login_as(client, tenant_a.staff_username)
        resp = client.post(
            "/whatsapp-numbers/create",
            data={
                "unit": str(tenant_b.unit_id),
                "label": "Smuggled Number",
                "phone_number_id": "smuggled-phone-id",
                "send_delay_seconds": "0",
                "send_concurrency": "20",
            },
            follow_redirects=False,
        )
        assert resp.status_code != 302, "creation should have been rejected, not redirected as a success"
        with Session(engine) as session:
            leaked = (
                session.query(WhatsAppNumber)
                .filter(WhatsAppNumber.phone_number_id == "smuggled-phone-id")
                .one_or_none()
            )
        assert leaked is None

    def test_superadmin_sees_both_units_numbers(self, client, login_as, tenants, superadmin_username):
        # See test_superadmin_sees_both_orgs above for why pageSize=100.
        tenant_a, tenant_b = tenants
        login_as(client, superadmin_username)
        resp = client.get("/whatsapp-numbers/list", params={"pageSize": 100})
        assert resp.status_code == 200
        assert tenant_a.number_label in resp.text
        assert tenant_b.number_label in resp.text


class TestUnitWebhookAdmin:
    """/pco-webhook/* - ScopedModelView over Unit itself (unit_field='id'),
    open to any logged-in staff; can_create/can_delete are off."""

    def test_list_excludes_other_units(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        login_as(client, tenant_a.staff_username)
        resp = client.get("/pco-webhook/list")
        assert resp.status_code == 200
        assert tenant_a.unit_name in resp.text
        assert tenant_b.unit_name not in resp.text

    def test_edit_page_blocked_for_other_org_unit(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        login_as(client, tenant_a.staff_username)
        resp = client.get(f"/pco-webhook/edit/{tenant_b.unit_id}")
        assert resp.status_code == 404

    def test_update_blocked_for_guessed_pk_of_other_org_unit(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        login_as(client, tenant_a.staff_username)
        resp = client.post(
            f"/pco-webhook/edit/{tenant_b.unit_id}",
            data={"pco_webhook_user_name": "attacker"},
        )
        assert resp.status_code == 404
        assert _get(Unit, tenant_b.unit_id).pco_webhook_user_name != "attacker"


class TestPCOOrganizationSettingsAdmin:
    """/pco-settings/* - org-scoped via org_id directly (no unit_id)."""

    def test_list_excludes_other_orgs_settings(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/pco-settings/list")
        assert resp.status_code == 200
        assert tenant_a.org_name in resp.text
        assert tenant_b.org_name not in resp.text

    def test_details_blocked_for_other_orgs_settings(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.get(f"/pco-settings/details/{tenant_b.pco_settings_id}")
        assert resp.status_code == 404

    def test_edit_page_blocked_for_other_orgs_settings(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.get(f"/pco-settings/edit/{tenant_b.pco_settings_id}")
        assert resp.status_code == 404

    def test_update_blocked_for_guessed_pk_of_other_orgs_settings(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.post(
            f"/pco-settings/edit/{tenant_b.pco_settings_id}",
            data={"pco_token_id": "hijacked-token"},
        )
        assert resp.status_code == 404
        assert _get(PCOOrganizationSettings, tenant_b.pco_settings_id).pco_token_id != "hijacked-token"

    def test_plain_staff_cannot_reach_pco_settings(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.staff_username)
        resp = client.get("/pco-settings/list")
        assert resp.status_code == 403

    def test_insert_forces_callers_own_org_even_if_another_org_is_posted(self, client, login_as, tenants):
        """The `organisation` dropdown on this create form isn't filtered
        to the caller's own org (unlike ScopedModelView.scaffold_form's
        unit/organisation filtering) - insert_model is the only thing
        standing between an org admin and creating settings under a
        different org. This pins that down as a regression guard."""
        tenant_a, tenant_b = tenants
        # The `tenants` fixture already seeds one PCOOrganizationSettings
        # row per org - insert_model's singleton-per-org guard would 400
        # on a second row for org A regardless of scoping, so clear it
        # first to isolate what this test actually checks.
        with Session(engine) as session:
            session.query(PCOOrganizationSettings).filter(
                PCOOrganizationSettings.id == tenant_a.pco_settings_id
            ).delete()
            session.commit()
        login_as(client, tenant_a.org_admin_username)
        resp = client.post(
            "/pco-settings/create",
            data={"organisation": str(tenant_b.org_id), "pco_token_id": "new-token", "pco_token_secret": "s3cret"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        with Session(engine) as session:
            created = (
                session.query(PCOOrganizationSettings)
                .filter(PCOOrganizationSettings.pco_token_id == "new-token")
                .one()
            )
            assert created.org_id == tenant_a.org_id


class TestUserAdmin:
    """/users/* - org-scoped via org_id directly (no unit_id), same
    pattern as PCOOrganizationSettingsAdmin above."""

    def test_list_excludes_other_orgs_staff(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/users/list")
        assert resp.status_code == 200
        assert tenant_a.staff_username in resp.text
        assert tenant_b.staff_username not in resp.text
        assert tenant_b.org_admin_username not in resp.text

    def test_details_blocked_for_other_orgs_staff(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.get(f"/users/details/{tenant_b.staff_id}")
        assert resp.status_code == 404

    def test_edit_page_blocked_for_other_orgs_staff(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.get(f"/users/edit/{tenant_b.staff_id}")
        assert resp.status_code == 404

    def test_update_blocked_for_guessed_pk_of_other_orgs_staff(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.post(f"/users/edit/{tenant_b.staff_id}", data={"username": "hijacked"})
        assert resp.status_code == 404
        assert _get(User, tenant_b.staff_id).username == tenant_b.staff_username

    def test_delete_blocked_for_guessed_pk_of_other_orgs_staff(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        client.delete(f"/users/delete?pks={tenant_b.staff_id}")
        assert _get(User, tenant_b.staff_id) is not None

    def test_org_admin_cannot_promote_self_to_superadmin(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.post(
            f"/users/edit/{tenant_a.org_admin_id}",
            data={"username": tenant_a.org_admin_username, "is_superadmin": "y", "is_org_admin": "y"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert _get(User, tenant_a.org_admin_id).is_superadmin is False

    def test_plain_staff_cannot_reach_user_admin(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.staff_username)
        resp = client.get("/users/list")
        assert resp.status_code == 403

    # --- The `units` multi-select on this form used to have no equivalent
    # of ScopedModelView.scaffold_form's filtering (UserAdmin is a plain
    # ModelView, not a ScopedModelView), and insert_model/update_model
    # never re-validated submitted unit ids against the caller's own org
    # the way they do for org_id/is_superadmin. An org admin could
    # therefore both see and successfully grant a new or existing staff
    # member access to a *different organisation's* unit - and everything
    # gated on that unit (WhatsApp numbers, campaigns, templates, ...)
    # opened up to them. Fixed by UserAdmin.scaffold_form/
    # _restrict_units_to_org in admin_views.py.

    def test_create_dropdown_should_not_list_other_orgs_units(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/users/create")
        assert resp.status_code == 200
        assert tenant_b.unit_name not in resp.text, (
            "Create User form leaks another organisation's unit name into the "
            "'Units' picker - see UserAdmin in admin_views.py, which has no "
            "scaffold_form override to filter it the way ScopedModelView does."
        )

    def test_org_admin_cannot_grant_new_user_access_to_another_orgs_unit(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.post(
            "/users/create",
            data={
                "username": "cross-org-grant-attempt",
                "password_hash": "SomeStrongPassw0rd!",
                "active": "y",
                "units": str(tenant_b.unit_id),
            },
            follow_redirects=False,
        )
        with Session(engine) as session:
            created = (
                session.query(User).filter(User.username == "cross-org-grant-attempt").one_or_none()
            )
            assert created is not None, "expected the user to be created (scoped to org_id, just without the unit grant)"
            granted_unit_ids = [u.id for u in created.units]
        assert tenant_b.unit_id not in granted_unit_ids, (
            "Org admin for org A was able to grant a newly-created staff user "
            "access to org B's unit via the 'Units' checkbox list on "
            "/users/create - see UserAdmin.insert_model in admin_views.py, "
            "which forces org_id but never validates `units` against the "
            "caller's own org."
        )

    def test_org_admin_can_still_grant_own_orgs_unit(self, client, login_as, tenants):
        """Regression guard alongside the two tests above: the fix must
        narrow the boundary, not remove the feature - an org admin still
        needs to be able to grant a new staff member access to their own
        org's unit(s)."""
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.post(
            "/users/create",
            data={
                "username": "legit-own-org-grant",
                "password_hash": "SomeStrongPassw0rd!",
                "active": "y",
                "units": str(tenant_a.unit_id),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302
        with Session(engine) as session:
            created = session.query(User).filter(User.username == "legit-own-org-grant").one()
            assert [u.id for u in created.units] == [tenant_a.unit_id]

    def test_superadmin_can_grant_any_orgs_unit(self, client, login_as, tenants, superadmin_username):
        """The org-scoping fix above only applies to non-superadmins -
        superadmins still manage every org's staff and units, unrestricted."""
        _tenant_a, tenant_b = tenants
        login_as(client, superadmin_username)
        resp = client.post(
            "/users/create",
            data={
                "username": "superadmin-cross-org-grant",
                "password_hash": "SomeStrongPassw0rd!",
                "active": "y",
                "units": str(tenant_b.unit_id),
                "organisation": str(tenant_b.org_id),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302
        with Session(engine) as session:
            created = session.query(User).filter(User.username == "superadmin-cross-org-grant").one()
            assert [u.id for u in created.units] == [tenant_b.unit_id]

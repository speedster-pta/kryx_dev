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

from autosend import storage
from autosend.admin_models import (
    PCOOrganizationSettings,
    StitchCredentials,
    Unit,
    User,
    WhatsAppNumber,
    engine,
)


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
        # Filtered by each tenant's own unique unit name rather than
        # paginating through the whole (ever-growing, shared across the
        # test session) units table - a plain pageSize bump is fragile
        # since every test using the `tenants` fixture adds one more row
        # that could push a given tenant's unit off the requested page
        # (see the identical reasoning on TestWhatsAppNumbers's
        # test_superadmin_sees_both_units_numbers below).
        tenant_a, tenant_b = tenants
        login_as(client, superadmin_username)
        resp = client.get("/unit/list", params={"search": tenant_a.unit_name})
        assert resp.status_code == 200
        assert tenant_a.unit_name in resp.text
        resp = client.get("/unit/list", params={"search": tenant_b.unit_name})
        assert resp.status_code == 200
        assert tenant_b.unit_name in resp.text


class TestUnitsPage:
    """/units/* - the bespoke UnitsView (admin_unit_pages.py), a hand-rolled
    BaseView sitting alongside /unit/* (TestUnitAdmin above). Same
    superadmin-or-org-admin-only role gate, org_id scoping enforced inline
    in every @expose handler rather than by ScopedModelView."""

    def test_list_excludes_other_org_units(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/units")
        assert resp.status_code == 200
        assert tenant_a.unit_name in resp.text
        assert tenant_b.unit_name not in resp.text

    def test_detail_blocked_for_other_org_unit(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.get(f"/units/{tenant_b.unit_id}")
        assert resp.status_code == 404

    def test_update_blocked_for_guessed_pk_of_other_org_unit(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.post(f"/units/{tenant_b.unit_id}/update", data={"name": "Renamed by attacker"})
        assert resp.status_code == 404
        assert _get(Unit, tenant_b.unit_id).name == tenant_b.unit_name

    def test_campus_update_blocked_for_other_org_unit(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.post(f"/units/{tenant_b.unit_id}/campus", data={"pco_campus_id": "hijacked-campus"})
        assert resp.status_code == 404
        assert _get(Unit, tenant_b.unit_id).pco_campus_id != "hijacked-campus"

    def test_create_forces_callers_own_org_even_if_another_org_is_posted(self, client, login_as, tenants):
        """An org admin has no org_id field on this create form at all -
        create() only ever reads org_id from the form for a superadmin,
        else forces the caller's session org_id - but a crafted POST
        naming another org's id should still never land under it."""
        tenant_a, tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.post(
            "/units",
            data={"name": "Smuggled Unit", "org_id": str(tenant_b.org_id)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        with Session(engine) as session:
            created = session.query(Unit).filter(Unit.name == "Smuggled Unit").one()
            assert created.org_id == tenant_a.org_id

    def test_plain_staff_cannot_reach_units_page(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.staff_username)
        resp = client.get("/units")
        assert resp.status_code == 403

    def test_superadmin_sees_both_orgs(self, client, login_as, tenants, superadmin_username):
        tenant_a, tenant_b = tenants
        login_as(client, superadmin_username)
        resp = client.get("/units")
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
        # Filtered by each tenant's own unique label rather than paginating
        # through the whole (ever-growing, shared across the test session)
        # numbers table - a plain pageSize bump is fragile since every test
        # using the `tenants` fixture adds one more row that could push a
        # given tenant's number off the requested page.
        tenant_a, tenant_b = tenants
        login_as(client, superadmin_username)
        resp = client.get("/whatsapp-numbers/list", params={"search": tenant_a.number_label})
        assert resp.status_code == 200
        assert tenant_a.number_label in resp.text
        resp = client.get("/whatsapp-numbers/list", params={"search": tenant_b.number_label})
        assert resp.status_code == 200
        assert tenant_b.number_label in resp.text


class TestWhatsAppNumbersPage:
    """/whatsapp-numbers/* (the page routes, not /whatsapp-numbers/list
    etc.) - the bespoke WhatsAppNumbersView (admin_number_pages.py). No
    is_accessible override (open to plain unit-scoped staff, same as
    WhatsAppNumberAdmin above); scoping goes through
    admin_pages._scoped_unit_ids()/resolve_unit_ids(), which is
    unit-grained, not just org-grained - a staff member assigned to one
    unit in their own org must not reach a sibling unit's number in that
    *same* org, not only another tenant's."""

    def test_list_excludes_other_units_numbers(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        login_as(client, tenant_a.staff_username)
        resp = client.get("/whatsapp-numbers")
        assert resp.status_code == 200
        assert tenant_a.number_label in resp.text
        assert tenant_b.number_label not in resp.text

    def test_detail_blocked_for_other_units_number(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        login_as(client, tenant_a.staff_username)
        resp = client.get(f"/whatsapp-numbers/{tenant_b.number_id}")
        assert resp.status_code == 404

    def test_update_blocked_for_guessed_pk_of_other_units_number(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        login_as(client, tenant_a.staff_username)
        resp = client.post(
            f"/whatsapp-numbers/{tenant_b.number_id}/update",
            data={"label": "Hijacked", "send_delay_seconds": "0", "send_concurrency": "20"},
        )
        assert resp.status_code == 404
        assert _get(WhatsAppNumber, tenant_b.number_id).label == tenant_b.number_label

    def test_credentials_update_blocked_for_other_units_number(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        login_as(client, tenant_a.staff_username)
        resp = client.post(
            f"/whatsapp-numbers/{tenant_b.number_id}/credentials",
            data={"phone_number_id": "hijacked-phone-id"},
        )
        assert resp.status_code == 404
        assert _get(WhatsAppNumber, tenant_b.number_id).phone_number_id != "hijacked-phone-id"

    def test_staff_cannot_reach_sibling_units_number_in_same_org(self, client, login_as, tenants):
        """resolve_unit_ids() scopes a plain staff user to their own
        assigned unit(s), not their whole org - a second unit in the SAME
        org that this staff member isn't assigned to must be just as
        invisible as another tenant's unit."""
        tenant_a, _tenant_b = tenants
        with Session(engine) as session:
            sibling_unit = Unit(
                org_id=tenant_a.org_id, slug="sibling-unit", name="Sibling Unit",
                active=True, created_at="2024-01-01T00:00:00+00:00",
            )
            session.add(sibling_unit)
            session.flush()
            sibling_number = WhatsAppNumber(
                unit_id=sibling_unit.id, label="Sibling Number", phone_number_id="sibling-phone-id",
                active=True, created_at="2024-01-01T00:00:00+00:00",
            )
            session.add(sibling_number)
            session.commit()
            sibling_number_id = sibling_number.id

        login_as(client, tenant_a.staff_username)
        resp = client.get(f"/whatsapp-numbers/{sibling_number_id}")
        assert resp.status_code == 404

    def test_superadmin_sees_both_units_numbers(self, client, login_as, tenants, superadmin_username):
        tenant_a, tenant_b = tenants
        login_as(client, superadmin_username)
        resp = client.get("/whatsapp-numbers")
        assert resp.status_code == 200
        assert tenant_a.number_label in resp.text
        assert tenant_b.number_label in resp.text


class TestUnitWebhookAdmin:
    """/pco-webhook/* - ScopedModelView over Unit itself (unit_field='id'),
    open to any logged-in staff (once their org has the PCO module
    enabled - see test_pco_module_gating.py for that gate itself);
    can_create/can_delete are off."""

    def test_list_excludes_other_units(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        storage.grant(tenant_a.org_id, storage.MODULE_PCO)
        storage.enable(tenant_a.org_id, storage.MODULE_PCO)
        login_as(client, tenant_a.staff_username)
        resp = client.get("/pco-webhook/list")
        assert resp.status_code == 200
        assert tenant_a.unit_name in resp.text
        assert tenant_b.unit_name not in resp.text

    def test_edit_page_blocked_for_other_org_unit(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        storage.grant(tenant_a.org_id, storage.MODULE_PCO)
        storage.enable(tenant_a.org_id, storage.MODULE_PCO)
        login_as(client, tenant_a.staff_username)
        resp = client.get(f"/pco-webhook/edit/{tenant_b.unit_id}")
        assert resp.status_code == 404

    def test_update_blocked_for_guessed_pk_of_other_org_unit(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        storage.grant(tenant_a.org_id, storage.MODULE_PCO)
        storage.enable(tenant_a.org_id, storage.MODULE_PCO)
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
        storage.grant(tenant_a.org_id, storage.MODULE_PCO)
        storage.enable(tenant_a.org_id, storage.MODULE_PCO)
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/pco-settings/list")
        assert resp.status_code == 200
        assert tenant_a.org_name in resp.text
        assert tenant_b.org_name not in resp.text

    def test_details_blocked_for_other_orgs_settings(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        storage.grant(tenant_a.org_id, storage.MODULE_PCO)
        storage.enable(tenant_a.org_id, storage.MODULE_PCO)
        login_as(client, tenant_a.org_admin_username)
        resp = client.get(f"/pco-settings/details/{tenant_b.pco_settings_id}")
        assert resp.status_code == 404

    def test_edit_page_blocked_for_other_orgs_settings(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        storage.grant(tenant_a.org_id, storage.MODULE_PCO)
        storage.enable(tenant_a.org_id, storage.MODULE_PCO)
        login_as(client, tenant_a.org_admin_username)
        resp = client.get(f"/pco-settings/edit/{tenant_b.pco_settings_id}")
        assert resp.status_code == 404

    def test_update_blocked_for_guessed_pk_of_other_orgs_settings(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        storage.grant(tenant_a.org_id, storage.MODULE_PCO)
        storage.enable(tenant_a.org_id, storage.MODULE_PCO)
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
        storage.grant(tenant_a.org_id, storage.MODULE_PCO)
        storage.enable(tenant_a.org_id, storage.MODULE_PCO)
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


class TestPcoOAuthStart:
    """/pco-oauth/start - writes a pco_oauth_states row (see
    schema.py/storage.units.create_pco_oauth_state) correlating PCO's
    OAuth callback back to an org. Unlike PCOOrganizationSettingsAdmin's
    create form above, there's no client-controllable field for the org
    at all for a non-superadmin - org_id is only read from the POST body
    when the caller is a superadmin."""

    def _configure_pco(self, tenant_a):
        storage.grant(tenant_a.org_id, storage.MODULE_PCO)
        storage.enable(tenant_a.org_id, storage.MODULE_PCO)

    def test_org_admin_posted_org_id_is_ignored(self, client, login_as, tenants):
        """An org admin POSTing another org's id in the org_id field must
        still only ever create a state row for their own session org -
        the same "never trust the client for which org this belongs to"
        rule as every other org-scoped write in this codebase."""
        tenant_a, tenant_b = tenants
        self._configure_pco(tenant_a)
        # Platform OAuth app credentials must exist for the redirect to
        # be built at all.
        with Session(engine) as session:
            from autosend.admin_models import PcoPlatformSettings
            from datetime import datetime, timezone

            session.add(PcoPlatformSettings(
                client_id="test-client-id", client_secret="test-client-secret",
                created_at=datetime.now(timezone.utc).isoformat(),
            ))
            session.commit()

        login_as(client, tenant_a.org_admin_username)
        resp = client.post(
            "/pco-oauth/start", data={"org_id": str(tenant_b.org_id)}, follow_redirects=False,
        )
        assert resp.status_code == 303
        # Pull the state PCO's redirect URL carries and confirm it was
        # issued for tenant_a's org, never tenant_b's.
        state = resp.headers["location"].split("state=")[1].split("&")[0]
        consumed = storage.consume_pco_oauth_state(state, max_age_minutes=30)
        assert consumed is not None
        assert consumed["org_id"] == tenant_a.org_id

    def test_plain_staff_cannot_start_oauth(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        self._configure_pco(tenant_a)
        login_as(client, tenant_a.staff_username)
        resp = client.post("/pco-oauth/start", data={}, follow_redirects=False)
        assert resp.status_code == 403


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

    def test_org_admin_cannot_delete_self_leaving_org_without_an_admin(self, client, login_as, tenants):
        # SQLAdmin's bulk-delete endpoint swallows the HTTPException raised
        # by delete_model and still responds 200 (same as the guessed-pk
        # delete test above) - the real assertion is that the row survives.
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        client.delete(f"/users/delete?pks={tenant_a.org_admin_id}")
        assert _get(User, tenant_a.org_admin_id) is not None, (
            "Org admin deleted themselves, leaving the organisation with no "
            "admin - see UserAdmin.delete_model in admin_views.py."
        )

    def test_org_admin_cannot_demote_self_leaving_org_without_an_admin(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.post(
            f"/users/edit/{tenant_a.org_admin_id}",
            data={"username": tenant_a.org_admin_username, "is_org_admin": ""},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert _get(User, tenant_a.org_admin_id).is_org_admin is True, (
            "Org admin demoted themselves to plain staff, leaving the "
            "organisation with no admin - see UserAdmin.update_model in "
            "admin_views.py."
        )

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

    def test_org_admin_cannot_grant_new_user_access_to_another_orgs_unit(
        self, client, login_as, tenants, grant_unlimited_capacity
    ):
        tenant_a, tenant_b = tenants
        # Standard entitlement is 1 user (see billing/entitlements.py) and
        # tenant_a already has two (seeded staff + org-admin) - this test
        # is about the unit-scoping boundary, not the plan's seat limit.
        grant_unlimited_capacity(tenant_a.org_id)
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

    def test_org_admin_can_still_grant_own_orgs_unit(self, client, login_as, tenants, grant_unlimited_capacity):
        """Regression guard alongside the two tests above: the fix must
        narrow the boundary, not remove the feature - an org admin still
        needs to be able to grant a new staff member access to their own
        org's unit(s)."""
        tenant_a, _tenant_b = tenants
        # Same reasoning as the test above - tenant_a is already at the
        # standard 1-seat entitlement from the tenants fixture.
        grant_unlimited_capacity(tenant_a.org_id)
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

    def test_superadmin_can_grant_any_orgs_unit(
        self, client, login_as, tenants, superadmin_username, grant_unlimited_capacity
    ):
        """The org-scoping fix above only applies to non-superadmins -
        superadmins still manage every org's staff and units, unrestricted."""
        _tenant_a, tenant_b = tenants
        # Same reasoning as the two tests above - tenant_b is already at
        # the standard 1-seat entitlement from the tenants fixture.
        grant_unlimited_capacity(tenant_b.org_id)
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


class TestUsersPage:
    """/users/* (the bespoke page routes, distinct from /users/list etc.
    below) - the bespoke UsersView (admin_user_pages.py), sitting
    alongside the plain ModelView UserAdmin above. Same
    superadmin-or-org-admin-only role gate and org_id scoping, re-checked
    inline in every @expose handler rather than inherited from a base
    class - every one of UserAdmin's hand-reasoned protections
    (org-forcing, last-admin guard, unit-grant restricted to the target
    org) has its own separate implementation here that needs its own
    coverage."""

    def test_list_excludes_other_orgs_staff(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/users")
        assert resp.status_code == 200
        assert tenant_a.staff_username in resp.text
        assert tenant_b.staff_username not in resp.text

    def test_detail_blocked_for_other_orgs_staff(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.get(f"/users/{tenant_b.staff_id}")
        assert resp.status_code == 404

    def test_update_blocked_for_guessed_pk_of_other_orgs_staff(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.post(f"/users/{tenant_b.staff_id}/update", data={"is_org_admin": "y"})
        assert resp.status_code == 404
        assert _get(User, tenant_b.staff_id).is_org_admin is False

    def test_password_reset_blocked_for_other_orgs_user(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        original_hash = _get(User, tenant_b.staff_id).password_hash
        login_as(client, tenant_a.org_admin_username)
        resp = client.post(f"/users/{tenant_b.staff_id}/password", data={"password": "AttackerPassw0rd!"})
        assert resp.status_code == 404
        assert _get(User, tenant_b.staff_id).password_hash == original_hash

    def test_create_forces_callers_own_org_even_if_another_org_is_posted(
        self, client, login_as, tenants, grant_unlimited_capacity
    ):
        tenant_a, tenant_b = tenants
        grant_unlimited_capacity(tenant_a.org_id)
        login_as(client, tenant_a.org_admin_username)
        resp = client.post(
            "/users",
            data={
                "username": "bespoke-cross-org-create",
                "password": "SomeStrongPassw0rd!",
                "org_id": str(tenant_b.org_id),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        with Session(engine) as session:
            created = session.query(User).filter(User.username == "bespoke-cross-org-create").one()
            assert created.org_id == tenant_a.org_id

    def test_create_form_should_not_list_other_orgs_units(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/users/new")
        assert resp.status_code == 200
        assert tenant_b.unit_name not in resp.text

    def test_org_admin_cannot_grant_new_user_access_to_another_orgs_unit(
        self, client, login_as, tenants, grant_unlimited_capacity
    ):
        tenant_a, tenant_b = tenants
        grant_unlimited_capacity(tenant_a.org_id)
        login_as(client, tenant_a.org_admin_username)
        resp = client.post(
            "/users",
            data={
                "username": "bespoke-cross-org-grant-attempt",
                "password": "SomeStrongPassw0rd!",
                "units": str(tenant_b.unit_id),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        with Session(engine) as session:
            created = session.query(User).filter(User.username == "bespoke-cross-org-grant-attempt").one()
            granted_unit_ids = [u.id for u in created.units]
        assert tenant_b.unit_id not in granted_unit_ids

    def test_org_admin_cannot_remove_last_admin_via_bespoke_update(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.post(f"/users/{tenant_a.org_admin_id}/update", data={}, follow_redirects=False)
        assert resp.status_code == 400
        assert _get(User, tenant_a.org_admin_id).is_org_admin is True

    def test_plain_staff_cannot_reach_users_page(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.staff_username)
        resp = client.get("/users")
        assert resp.status_code == 403

    def test_superadmin_sees_both_orgs(self, client, login_as, tenants, superadmin_username):
        tenant_a, tenant_b = tenants
        login_as(client, superadmin_username)
        resp = client.get("/users")
        assert resp.status_code == 200
        assert tenant_a.staff_username in resp.text
        assert tenant_b.staff_username in resp.text


class TestOrganisationsView:
    """/organisations and /organisations/{org_id} - the superadmin org
    list + per-org detail/config page from admin_org_pages.py. Role
    check is superadmin-only (org-admins have no path into this view at
    all, so there's no per-org scoping to attack here the way there is
    for org-scoped views)."""

    def test_plain_staff_cannot_reach_list(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.staff_username)
        resp = client.get("/organisations")
        assert resp.status_code == 403

    def test_org_admin_cannot_reach_list(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/organisations")
        assert resp.status_code == 403

    def test_org_admin_cannot_reach_detail_page(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.get(f"/organisations/{tenant_a.org_id}")
        assert resp.status_code == 403

    def test_superadmin_sees_both_orgs_in_list(self, client, login_as, tenants, superadmin_username):
        tenant_a, tenant_b = tenants
        login_as(client, superadmin_username)
        resp = client.get("/organisations")
        assert resp.status_code == 200
        assert tenant_a.org_name in resp.text
        assert tenant_b.org_name in resp.text

    def test_superadmin_detail_page_shows_that_orgs_modules(self, client, login_as, tenants, superadmin_username):
        tenant_a, _tenant_b = tenants
        storage.grant(tenant_a.org_id, storage.MODULE_PCO)
        login_as(client, superadmin_username)
        resp = client.get(f"/organisations/{tenant_a.org_id}")
        assert resp.status_code == 200
        assert tenant_a.org_name in resp.text

    def test_org_admin_cannot_post_identity_update(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.post(
            f"/organisations/{tenant_a.org_id}/update",
            data={"name": "Hijacked Name", "active": "y"},
            follow_redirects=False,
        )
        assert resp.status_code == 403
        with Session(engine) as session:
            from autosend.admin_models import Organisation

            assert session.get(Organisation, tenant_a.org_id).name == tenant_a.org_name

    def test_superadmin_can_rename_org(self, client, login_as, tenants, superadmin_username):
        tenant_a, _tenant_b = tenants
        login_as(client, superadmin_username)
        resp = client.post(
            f"/organisations/{tenant_a.org_id}/update",
            data={"name": "Renamed Org", "active": "y"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        with Session(engine) as session:
            from autosend.admin_models import Organisation

            assert session.get(Organisation, tenant_a.org_id).name == "Renamed Org"


class TestOwnOrganisationPage:
    """/organisation - an org-admin's own-org equivalent of the
    superadmin detail page above; always uses the caller's session
    org_id, never a client-supplied one."""

    def test_plain_staff_cannot_reach_it(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.staff_username)
        resp = client.get("/organisation")
        assert resp.status_code == 403

    def test_org_admin_sees_only_their_own_org(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/organisation")
        assert resp.status_code == 200
        assert tenant_a.org_name in resp.text
        assert tenant_b.org_name not in resp.text

    def test_org_admin_cannot_edit_identity_from_own_org_page(self, client, login_as, tenants):
        """editable_identity=False for this page - name/active aren't a
        form here at all, only superadmin's /organisations/{id} exposes
        that (see TestOrganisationsView above)."""
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/organisation")
        assert f'action="/organisations/{tenant_a.org_id}/update"' not in resp.text

    def test_superadmin_with_no_org_is_sent_to_the_list(self, client, login_as, superadmin_username):
        login_as(client, superadmin_username)
        resp = client.get("/organisation", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/organisations"


class TestPcoSettingsAggregator:
    """/pco-settings, /pco-settings/token, /pco-settings/unit/{id} - the
    merged org-token + per-unit-webhook page from admin_org_pages.py.
    PCOOrganizationSettingsAdmin/UnitWebhookAdmin (tested above) stay
    fully functional; this is the additional org-admin-friendly surface
    over the same underlying data, so the same cross-org attacks apply."""

    def test_plain_staff_cannot_reach_page(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.staff_username)
        resp = client.get("/pco-settings")
        assert resp.status_code == 403

    def test_org_admin_sees_own_orgs_token_not_the_other_orgs(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        storage.grant(tenant_a.org_id, storage.MODULE_PCO)
        storage.enable(tenant_a.org_id, storage.MODULE_PCO)
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/pco-settings")
        assert resp.status_code == 200
        with Session(engine) as session:
            token_b = session.get(PCOOrganizationSettings, tenant_b.pco_settings_id).pco_token_id
        assert token_b not in resp.text

    def test_org_admin_org_id_query_param_is_ignored(self, client, login_as, tenants):
        """A non-superadmin always gets their own org's settings, even if
        they pass ?org_id=<someone else's> - only a superadmin's org_id
        selects which org this page shows.

        The org-admin render path doesn't print the org's name anywhere
        on the page (that's only shown to superadmins, in the "back to
        org" link), so the hidden org_id on the token-save form is the
        only in-page signal of which org this actually is."""
        tenant_a, tenant_b = tenants
        storage.grant(tenant_a.org_id, storage.MODULE_PCO)
        storage.enable(tenant_a.org_id, storage.MODULE_PCO)
        login_as(client, tenant_a.org_admin_username)
        resp = client.get(f"/pco-settings?org_id={tenant_b.org_id}")
        assert resp.status_code == 200
        assert f'value="{tenant_a.org_id}"' in resp.text
        assert f'value="{tenant_b.org_id}"' not in resp.text

    def test_superadmin_with_no_org_id_is_sent_to_the_list(self, client, login_as, superadmin_username):
        login_as(client, superadmin_username)
        resp = client.get("/pco-settings", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/organisations"

    def test_superadmin_can_view_any_orgs_settings(self, client, login_as, tenants, superadmin_username):
        tenant_a, _tenant_b = tenants
        login_as(client, superadmin_username)
        resp = client.get(f"/pco-settings?org_id={tenant_a.org_id}")
        assert resp.status_code == 200
        assert tenant_a.org_name in resp.text

    def test_org_admin_token_save_forces_own_org_even_if_another_org_is_posted(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        storage.grant(tenant_a.org_id, storage.MODULE_PCO)
        storage.enable(tenant_a.org_id, storage.MODULE_PCO)
        login_as(client, tenant_a.org_admin_username)
        resp = client.post(
            "/pco-settings/token",
            data={"org_id": str(tenant_b.org_id), "pco_token_id": "hijacked-token", "pco_token_secret": "s3cret"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        with Session(engine) as session:
            # The posted org_id (tenant_b's) must be ignored - the write
            # lands on the caller's own org, and tenant_b's row is untouched.
            assert session.get(PCOOrganizationSettings, tenant_a.pco_settings_id).pco_token_id == "hijacked-token"
            assert session.get(PCOOrganizationSettings, tenant_b.pco_settings_id).pco_token_id != "hijacked-token"

    def test_token_save_blank_secret_keeps_existing_secret(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        storage.grant(tenant_a.org_id, storage.MODULE_PCO)
        storage.enable(tenant_a.org_id, storage.MODULE_PCO)
        with Session(engine) as session:
            row = session.get(PCOOrganizationSettings, tenant_a.pco_settings_id)
            row.pco_token_secret = "original-secret"
            session.commit()

        login_as(client, tenant_a.org_admin_username)
        resp = client.post(
            "/pco-settings/token",
            data={"org_id": str(tenant_a.org_id), "pco_token_id": "updated-id", "pco_token_secret": ""},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        with Session(engine) as session:
            row = session.get(PCOOrganizationSettings, tenant_a.pco_settings_id)
            assert row.pco_token_id == "updated-id"
            assert row.pco_token_secret == "original-secret"

    def test_plain_staff_cannot_post_token(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.staff_username)
        resp = client.post(
            "/pco-settings/token",
            data={"org_id": str(tenant_a.org_id), "pco_token_id": "attacker-token", "pco_token_secret": "s3cret"},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_org_admin_cannot_edit_other_orgs_unit_webhook(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        storage.grant(tenant_a.org_id, storage.MODULE_PCO)
        storage.enable(tenant_a.org_id, storage.MODULE_PCO)
        login_as(client, tenant_a.org_admin_username)
        resp = client.post(
            f"/pco-settings/unit/{tenant_b.unit_id}",
            data={"pco_webhook_user_name": "attacker"},
            follow_redirects=False,
        )
        assert resp.status_code == 404
        assert _get(Unit, tenant_b.unit_id).pco_webhook_user_name != "attacker"

    def test_org_admin_can_edit_their_own_units_webhook(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        storage.grant(tenant_a.org_id, storage.MODULE_PCO)
        storage.enable(tenant_a.org_id, storage.MODULE_PCO)
        login_as(client, tenant_a.org_admin_username)
        resp = client.post(
            f"/pco-settings/unit/{tenant_a.unit_id}",
            data={"pco_webhook_user_name": "Jane Sexton", "pco_campus_id": "campus-1"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        updated = _get(Unit, tenant_a.unit_id)
        assert updated.pco_webhook_user_name == "Jane Sexton"
        assert updated.pco_campus_id == "campus-1"

    def test_superadmin_can_edit_any_orgs_unit_webhook(self, client, login_as, tenants, superadmin_username):
        _tenant_a, tenant_b = tenants
        login_as(client, superadmin_username)
        resp = client.post(
            f"/pco-settings/unit/{tenant_b.unit_id}",
            data={"pco_webhook_user_name": "Superadmin Set"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert _get(Unit, tenant_b.unit_id).pco_webhook_user_name == "Superadmin Set"


class TestStitchSettingsPage:
    """/stitch-settings* - StitchSettingsView (admin_org_pages.py).
    Gated by module enablement (stitch_module_visible), not by role - a
    plain unit-scoped staff member can reach it once their org's Stitch
    module is enabled, same policy as WhatsAppNumbersView. Scoping is
    two-layered: _resolve_org_id pins non-superadmins to their own
    session org, and _visible_unit_ids_within_org further narrows a
    plain staff member (not org-admin) to only their own assigned
    unit(s) within that org."""

    def test_plain_staff_cannot_reach_page_when_module_not_enabled(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.staff_username)
        resp = client.get("/stitch-settings")
        assert resp.status_code == 403

    def test_org_admin_sees_only_own_orgs_units(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        storage.grant(tenant_a.org_id, storage.MODULE_STITCH)
        storage.enable(tenant_a.org_id, storage.MODULE_STITCH)
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/stitch-settings")
        assert resp.status_code == 200
        assert tenant_a.unit_name in resp.text
        assert tenant_b.unit_name not in resp.text

    def test_save_blocked_for_other_orgs_unit(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        storage.grant(tenant_a.org_id, storage.MODULE_STITCH)
        storage.enable(tenant_a.org_id, storage.MODULE_STITCH)
        login_as(client, tenant_a.org_admin_username)
        resp = client.post(
            f"/stitch-settings/unit/{tenant_b.unit_id}",
            data={"client_id": "hijacked-client-id", "client_secret": "hijacked-secret"},
            follow_redirects=False,
        )
        assert resp.status_code == 404
        with Session(engine) as session:
            leaked = (
                session.query(StitchCredentials)
                .filter(StitchCredentials.unit_id == tenant_b.unit_id)
                .one_or_none()
            )
        assert leaked is None

    def test_plain_staff_cannot_save_sibling_units_credentials_in_same_org(self, client, login_as, tenants):
        """Mirrors the WhatsAppNumbersView same-org sibling-unit case -
        _visible_unit_ids_within_org must scope a plain staff member down
        to their own unit(s), not their whole org, even once the org's
        Stitch module is enabled."""
        tenant_a, _tenant_b = tenants
        storage.grant(tenant_a.org_id, storage.MODULE_STITCH)
        storage.enable(tenant_a.org_id, storage.MODULE_STITCH)
        with Session(engine) as session:
            sibling_unit = Unit(
                org_id=tenant_a.org_id, slug="stitch-sibling-unit", name="Stitch Sibling Unit",
                active=True, created_at="2024-01-01T00:00:00+00:00",
            )
            session.add(sibling_unit)
            session.commit()
            sibling_unit_id = sibling_unit.id

        login_as(client, tenant_a.staff_username)
        resp = client.post(
            f"/stitch-settings/unit/{sibling_unit_id}",
            data={"client_id": "smuggled-client-id", "client_secret": "smuggled-secret"},
            follow_redirects=False,
        )
        assert resp.status_code == 404
        with Session(engine) as session:
            leaked = (
                session.query(StitchCredentials)
                .filter(StitchCredentials.unit_id == sibling_unit_id)
                .one_or_none()
            )
        assert leaked is None

    def test_superadmin_can_view_and_save_any_orgs_unit(self, client, login_as, tenants, superadmin_username):
        _tenant_a, tenant_b = tenants
        login_as(client, superadmin_username)
        resp = client.get(f"/stitch-settings?org_id={tenant_b.org_id}")
        assert resp.status_code == 200
        assert tenant_b.unit_name in resp.text

        resp = client.post(
            f"/stitch-settings/unit/{tenant_b.unit_id}",
            data={"client_id": "superadmin-client-id", "client_secret": "superadmin-secret"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        with Session(engine) as session:
            saved = (
                session.query(StitchCredentials)
                .filter(StitchCredentials.unit_id == tenant_b.unit_id)
                .one()
            )
            assert saved.client_id == "superadmin-client-id"


class TestKryxBookingsSettingsPage:
    """/kryx-bookings-settings* - KryxBookingsSettingsView
    (admin_org_pages.py). Same shape/gating as TestStitchSettingsPage
    above - gated by module enablement, not role, with the same two-layer
    scoping (_resolve_org_id, _visible_unit_ids_within_org). There is no
    ORM model for kryx_bookings_connections (it's only ever read/written
    via storage/kryx_bookings.py, not a raw SQLAdmin CRUD screen), so
    these tests verify via storage.get_kryx_bookings_connection directly
    rather than a session.query.

    The per-status template/variable/number config itself now lives
    behind the JSON API in web/kryx_bookings_router.py (POST
    /api/kryx-bookings/templates), not a form POST on this page - see
    TestKryxBookingsTemplatesApi below for its own isolation coverage.
    This class covers only what KryxBookingsSettingsView itself renders/
    handles directly: the page shell and API key generation/deactivation."""

    def test_plain_staff_cannot_reach_page_when_module_not_enabled(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.staff_username)
        resp = client.get("/kryx-bookings-settings")
        assert resp.status_code == 403

    def test_org_admin_sees_only_own_orgs_units(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        storage.grant(tenant_a.org_id, storage.MODULE_KRYX_BOOKINGS)
        storage.enable(tenant_a.org_id, storage.MODULE_KRYX_BOOKINGS)
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/kryx-bookings-settings")
        assert resp.status_code == 200
        assert tenant_a.unit_name in resp.text
        assert tenant_b.unit_name not in resp.text

    def test_api_key_generation_blocked_for_other_orgs_unit(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        storage.grant(tenant_a.org_id, storage.MODULE_KRYX_BOOKINGS)
        storage.enable(tenant_a.org_id, storage.MODULE_KRYX_BOOKINGS)
        login_as(client, tenant_a.org_admin_username)
        resp = client.post(
            f"/kryx-bookings-settings/unit/{tenant_b.unit_id}/api-key",
            follow_redirects=False,
        )
        assert resp.status_code == 404
        assert storage.get_kryx_bookings_connection(tenant_b.unit_id) is None

    def test_plain_staff_cannot_generate_sibling_units_api_key_in_same_org(self, client, login_as, tenants):
        """Mirrors the Stitch same-org sibling-unit case -
        _visible_unit_ids_within_org must scope a plain staff member down
        to their own unit(s), not their whole org, even once the org's
        Kryx Bookings module is enabled."""
        tenant_a, _tenant_b = tenants
        storage.grant(tenant_a.org_id, storage.MODULE_KRYX_BOOKINGS)
        storage.enable(tenant_a.org_id, storage.MODULE_KRYX_BOOKINGS)
        with Session(engine) as session:
            sibling_unit = Unit(
                org_id=tenant_a.org_id, slug="kryx-bookings-sibling-unit", name="Kryx Bookings Sibling Unit",
                active=True, created_at="2024-01-01T00:00:00+00:00",
            )
            session.add(sibling_unit)
            session.commit()
            sibling_unit_id = sibling_unit.id

        login_as(client, tenant_a.staff_username)
        resp = client.post(
            f"/kryx-bookings-settings/unit/{sibling_unit_id}/api-key",
            follow_redirects=False,
        )
        assert resp.status_code == 404
        assert storage.get_kryx_bookings_connection(sibling_unit_id) is None

    def test_superadmin_can_view_and_generate_any_orgs_api_key(self, client, login_as, tenants, superadmin_username):
        _tenant_a, tenant_b = tenants
        login_as(client, superadmin_username)
        resp = client.get(f"/kryx-bookings-settings?org_id={tenant_b.org_id}")
        assert resp.status_code == 200
        assert tenant_b.unit_name in resp.text

        resp = client.post(
            f"/kryx-bookings-settings/unit/{tenant_b.unit_id}/api-key",
            follow_redirects=False,
        )
        assert resp.status_code == 303
        connection = storage.get_kryx_bookings_connection(tenant_b.unit_id)
        assert connection is not None
        assert connection["active"] is True


class TestKryxBookingsTemplatesApi:
    """/api/kryx-bookings/templates - web/kryx_bookings_router.py, backing
    the per-status tabs on the Kryx Bookings automations page. Reuses
    web.numbers_router's generic _check_unit_access/_check_number_access
    (the same helpers automations_router.py's registration-templates
    endpoint is built on), so this is the same cross-tenant attack shape
    as TestStitchSettingsPage above, just against a JSON API instead of a
    form POST. Status can now have more than one automation, each
    addressed by its own id (storage/kryx_bookings.py), so update/delete
    additionally need their own id-based ownership check
    (_check_automation_access, 404 if the id doesn't exist at all, 403 if
    it exists but belongs to a unit outside the caller's scope) alongside
    the unit/number checks a create already had."""

    def test_list_requires_kryx_bookings_module(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.staff_username)
        resp = client.get("/api/kryx-bookings/templates?status=pending")
        assert resp.status_code == 403

    def test_save_blocked_for_other_orgs_unit(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        storage.grant(tenant_a.org_id, storage.MODULE_KRYX_BOOKINGS)
        storage.enable(tenant_a.org_id, storage.MODULE_KRYX_BOOKINGS)
        login_as(client, tenant_a.org_admin_username)
        resp = client.post(
            "/api/kryx-bookings/templates",
            json={
                "unit_id": tenant_b.unit_id, "status": "pending", "template_name": "hijacked_template",
                "body_variable_order": ["first_name"], "whatsapp_number_id": tenant_b.number_id,
            },
        )
        assert resp.status_code == 403
        assert storage.list_active_booking_automations(tenant_b.unit_id, "pending") == []

    def test_save_blocked_for_other_orgs_number(self, client, login_as, tenants):
        """unit_id belongs to the caller's own org, but whatsapp_number_id
        is smuggled in from the other tenant - _check_number_access must
        catch this even though _check_unit_access alone would pass."""
        tenant_a, tenant_b = tenants
        storage.grant(tenant_a.org_id, storage.MODULE_KRYX_BOOKINGS)
        storage.enable(tenant_a.org_id, storage.MODULE_KRYX_BOOKINGS)
        login_as(client, tenant_a.org_admin_username)
        resp = client.post(
            "/api/kryx-bookings/templates",
            json={
                "unit_id": tenant_a.unit_id, "status": "pending", "template_name": "hijacked_template",
                "body_variable_order": ["first_name"], "whatsapp_number_id": tenant_b.number_id,
            },
        )
        assert resp.status_code == 403
        assert storage.list_active_booking_automations(tenant_a.unit_id, "pending") == []

    def test_update_blocked_for_other_orgs_automation_id(self, client, login_as, tenants):
        """Editing (POST with an id) an automation belonging to another
        org's unit must be rejected, not silently retarget it - the
        id-based ownership check (_check_automation_access) that create
        alone didn't need."""
        tenant_a, tenant_b = tenants
        storage.grant(tenant_a.org_id, storage.MODULE_KRYX_BOOKINGS)
        storage.enable(tenant_a.org_id, storage.MODULE_KRYX_BOOKINGS)
        storage.grant(tenant_b.org_id, storage.MODULE_KRYX_BOOKINGS)
        storage.enable(tenant_b.org_id, storage.MODULE_KRYX_BOOKINGS)
        automation_id = storage.upsert_booking_automation(
            None, tenant_b.unit_id, "pending", "original_template",
            ["first_name"], tenant_b.number_id, [], None, True,
        )
        login_as(client, tenant_a.org_admin_username)
        resp = client.post(
            "/api/kryx-bookings/templates",
            json={
                "id": automation_id, "unit_id": tenant_b.unit_id, "status": "pending",
                "template_name": "hijacked_template", "body_variable_order": ["first_name"],
                "whatsapp_number_id": tenant_b.number_id,
            },
        )
        assert resp.status_code == 403
        automations = storage.list_active_booking_automations(tenant_b.unit_id, "pending")
        assert automations[0]["template_name"] == "original_template"

    def test_delete_blocked_for_other_orgs_automation_id(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        storage.grant(tenant_b.org_id, storage.MODULE_KRYX_BOOKINGS)
        storage.enable(tenant_b.org_id, storage.MODULE_KRYX_BOOKINGS)
        automation_id = storage.upsert_booking_automation(
            None, tenant_b.unit_id, "pending", "original_template",
            ["first_name"], tenant_b.number_id, [], None, True,
        )
        login_as(client, tenant_a.org_admin_username)
        resp = client.delete(f"/api/kryx-bookings/templates/{automation_id}")
        assert resp.status_code == 403
        assert storage.get_booking_automation(automation_id) is not None

    def test_org_admin_can_delete_own_units_automation(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        storage.grant(tenant_a.org_id, storage.MODULE_KRYX_BOOKINGS)
        storage.enable(tenant_a.org_id, storage.MODULE_KRYX_BOOKINGS)
        automation_id = storage.upsert_booking_automation(
            None, tenant_a.unit_id, "pending", "to_delete",
            ["first_name"], tenant_a.number_id, [], None, True,
        )
        login_as(client, tenant_a.org_admin_username)
        resp = client.delete(f"/api/kryx-bookings/templates/{automation_id}")
        assert resp.status_code == 200
        assert storage.get_booking_automation(automation_id) is None

    def test_rejects_unknown_status(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        storage.grant(tenant_a.org_id, storage.MODULE_KRYX_BOOKINGS)
        storage.enable(tenant_a.org_id, storage.MODULE_KRYX_BOOKINGS)
        login_as(client, tenant_a.org_admin_username)
        resp = client.post(
            "/api/kryx-bookings/templates",
            json={
                "unit_id": tenant_a.unit_id, "status": "not-a-real-status", "template_name": "x",
                "body_variable_order": [], "whatsapp_number_id": tenant_a.number_id,
            },
        )
        assert resp.status_code == 400
        assert client.get("/api/kryx-bookings/templates?status=not-a-real-status").status_code == 400

    def test_org_admin_can_save_and_list_own_units_template(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        storage.grant(tenant_a.org_id, storage.MODULE_KRYX_BOOKINGS)
        storage.enable(tenant_a.org_id, storage.MODULE_KRYX_BOOKINGS)
        login_as(client, tenant_a.org_admin_username)
        resp = client.post(
            "/api/kryx-bookings/templates",
            json={
                "unit_id": tenant_a.unit_id, "status": "approved", "template_name": "booking_confirmed",
                "body_variable_order": ["first_name", "date_time"], "whatsapp_number_id": tenant_a.number_id,
            },
        )
        assert resp.status_code == 200

        listed = client.get("/api/kryx-bookings/templates?status=approved").json()
        assert any(r["unit_name"] == tenant_a.unit_name and r["template_name"] == "booking_confirmed" for r in listed)
        assert not any(r["unit_name"] == tenant_b.unit_name for r in listed)


class TestInboxConversations:
    """/api/conversations/* (web/conversations_router.py) - scoped via
    web.auth.resolve_unit_ids, same choke point as campaigns_router.py/
    numbers_router.py. Conversations themselves scope by unit_id only
    (see storage/conversations.py), so this exercises the same
    guessed-pk-of-another-tenant's-row pattern as every other suite here."""

    def _seed_conversation(self, tenant, contact_wa_id: str) -> dict:
        return storage.get_or_create_conversation(
            unit_id=tenant.unit_id, whatsapp_number_id=tenant.number_id,
            contact_wa_id=contact_wa_id, contact_name=f"Contact for {tenant.unit_name}",
        )

    def test_create_conversation_succeeds_for_own_number(self, client, login_as, tenants):
        tenant_a, _ = tenants
        login_as(client, tenant_a.staff_username)
        resp = client.post(
            "/api/conversations",
            json={"whatsapp_number_id": tenant_a.number_id, "contact_wa_id": "27000000011"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["unit_id"] == tenant_a.unit_id
        assert body["contact_wa_id"] == "27000000011"

    def test_create_conversation_blocked_for_other_orgs_number(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        before = storage.list_conversations(None, whatsapp_number_id=tenant_b.number_id)

        login_as(client, tenant_a.staff_username)
        resp = client.post(
            "/api/conversations",
            json={"whatsapp_number_id": tenant_b.number_id, "contact_wa_id": "27000000012"},
        )
        assert resp.status_code == 403

        after = storage.list_conversations(None, whatsapp_number_id=tenant_b.number_id)
        assert after == before

    def test_create_conversation_org_admin_blocked_for_other_orgs_number(self, client, login_as, tenants):
        """Same crafted-request shape as the other isolation tests here,
        but from an org-admin rather than plain staff - org-admins get
        every unit in their own org resolved live (resolve_unit_ids), not
        a broader/null scope, so this must 403 exactly like plain staff."""
        tenant_a, tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.post(
            "/api/conversations",
            json={"whatsapp_number_id": tenant_b.number_id, "contact_wa_id": "27000000014"},
        )
        assert resp.status_code == 403

    def test_create_conversation_superadmin_can_use_any_orgs_number(
        self, client, login_as, tenants, superadmin_username,
    ):
        _, tenant_b = tenants
        login_as(client, superadmin_username)
        resp = client.post(
            "/api/conversations",
            json={"whatsapp_number_id": tenant_b.number_id, "contact_wa_id": "27000000013"},
        )
        assert resp.status_code == 200
        assert resp.json()["unit_id"] == tenant_b.unit_id

    def test_list_excludes_other_orgs_conversations(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        conv_a = self._seed_conversation(tenant_a, "27000000001")
        conv_b = self._seed_conversation(tenant_b, "27000000002")

        login_as(client, tenant_a.staff_username)
        resp = client.get("/api/conversations")
        assert resp.status_code == 200
        ids = {c["id"] for c in resp.json()}
        assert conv_a["id"] in ids
        assert conv_b["id"] not in ids

    def test_messages_blocked_for_guessed_pk_of_other_orgs_conversation(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        conv_b = self._seed_conversation(tenant_b, "27000000003")

        login_as(client, tenant_a.staff_username)
        resp = client.get(f"/api/conversations/{conv_b['id']}/messages")
        assert resp.status_code == 404

    def test_reply_blocked_for_guessed_pk_of_other_orgs_conversation(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        conv_b = self._seed_conversation(tenant_b, "27000000004")

        login_as(client, tenant_a.staff_username)
        resp = client.post(f"/api/conversations/{conv_b['id']}/reply", json={"text": "hijacked"})
        assert resp.status_code == 404
        assert storage.list_messages(conv_b["id"]) == []

    def test_send_draft_blocked_for_guessed_pk_of_other_orgs_conversation(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        conv_b = self._seed_conversation(tenant_b, "27000000009")
        message_id = storage.record_outbound_message(
            conv_b["id"], sender_type="ai", message_type="text", body="drafted reply", status="draft",
        )

        login_as(client, tenant_a.staff_username)
        resp = client.post(f"/api/conversations/{conv_b['id']}/messages/{message_id}/send")
        assert resp.status_code == 404
        assert storage.list_messages(conv_b["id"])[0]["status"] == "draft"

    def test_discard_draft_blocked_for_guessed_pk_of_other_orgs_conversation(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        conv_b = self._seed_conversation(tenant_b, "27000000010")
        message_id = storage.record_outbound_message(
            conv_b["id"], sender_type="ai", message_type="text", body="drafted reply", status="draft",
        )

        login_as(client, tenant_a.staff_username)
        resp = client.delete(f"/api/conversations/{conv_b['id']}/messages/{message_id}")
        assert resp.status_code == 404
        assert storage.list_messages(conv_b["id"])[0]["status"] == "draft"

    def test_mark_read_blocked_for_guessed_pk_of_other_orgs_conversation(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        conv_b = self._seed_conversation(tenant_b, "27000000005")
        storage.record_inbound_message(conv_b["id"], wamid="wamid.iso1", message_type="text", body="hi")
        assert storage.get_conversation(conv_b["id"])["unread_count"] == 1

        login_as(client, tenant_a.staff_username)
        resp = client.post(f"/api/conversations/{conv_b['id']}/read")
        assert resp.status_code == 404
        assert storage.get_conversation(conv_b["id"])["unread_count"] == 1

    def test_media_blocked_for_guessed_pk_of_other_orgs_message(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        conv_b = self._seed_conversation(tenant_b, "27000000006")
        message_id = storage.record_inbound_message(
            conv_b["id"], wamid="wamid.iso2", message_type="image", media_id="meta-media-id",
        )
        storage.update_message_media_download(message_id, status="downloaded", local_path="/tmp/does-not-matter.jpg")

        login_as(client, tenant_a.staff_username)
        resp = client.get(f"/api/conversations/media/{message_id}")
        assert resp.status_code == 404

    def test_superadmin_sees_both_orgs_conversations(self, client, login_as, tenants, superadmin_username):
        tenant_a, tenant_b = tenants
        conv_a = self._seed_conversation(tenant_a, "27000000007")
        conv_b = self._seed_conversation(tenant_b, "27000000008")

        login_as(client, superadmin_username)
        resp = client.get("/api/conversations")
        assert resp.status_code == 200
        ids = {c["id"] for c in resp.json()}
        assert conv_a["id"] in ids
        assert conv_b["id"] in ids


class TestAIAssistantModuleGating:
    """The AI Assistant module (storage.MODULE_AI_ASSISTANT) gates the
    Knowledge Base API for a non-superadmin - see web/knowledge_router.py's
    _require_module. AI Settings / Auto-Reply Rules reuse
    numbers_router._get_number_if_authorized for their unit-scoping, same
    as every other number-scoped router in this app."""

    def test_knowledge_base_blocked_without_module_grant(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.staff_username)
        resp = client.post(
            "/api/knowledge/entries/manual",
            json={"unit_id": tenant_a.unit_id, "title": "Q", "content": "A"},
        )
        assert resp.status_code == 403

    def test_knowledge_base_works_once_module_enabled(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        storage.grant(tenant_a.org_id, storage.MODULE_AI_ASSISTANT)
        storage.enable(tenant_a.org_id, storage.MODULE_AI_ASSISTANT)
        login_as(client, tenant_a.staff_username)
        resp = client.post(
            "/api/knowledge/entries/manual",
            json={"unit_id": tenant_a.unit_id, "title": "Q", "content": "A"},
        )
        assert resp.status_code == 200


class TestKnowledgeBaseIsolation:
    """knowledge_base_entries scopes by org_id (direct column) + a
    nullable unit_id ("org-wide" within that same org, never across
    orgs - see storage/knowledge_base.py's own docstring). Both tenants'
    orgs are granted+enabled for MODULE_AI_ASSISTANT in every test here,
    so what's actually under test is the org/unit scoping itself, not the
    module gate (covered separately above)."""

    def _enable_ai(self, org_id: int) -> None:
        storage.grant(org_id, storage.MODULE_AI_ASSISTANT)
        storage.enable(org_id, storage.MODULE_AI_ASSISTANT)

    def test_list_excludes_other_orgs_entries(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        self._enable_ai(tenant_a.org_id)
        self._enable_ai(tenant_b.org_id)
        entry_a = storage.create_knowledge_base_entry(tenant_a.org_id, tenant_a.unit_id, "A question", "An answer")
        storage.create_knowledge_base_entry(tenant_b.org_id, tenant_b.unit_id, "B question", "B answer")

        login_as(client, tenant_a.staff_username)
        resp = client.get("/api/knowledge/entries")
        assert resp.status_code == 200
        ids = {e["id"] for e in resp.json()}
        assert entry_a in ids

    def test_org_wide_entry_not_visible_to_other_org(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        self._enable_ai(tenant_a.org_id)
        self._enable_ai(tenant_b.org_id)
        org_wide_entry_a = storage.create_knowledge_base_entry(tenant_a.org_id, None, "Org-wide Q", "Org-wide A")

        login_as(client, tenant_b.staff_username)
        resp = client.get("/api/knowledge/entries")
        assert resp.status_code == 200
        assert all(e["id"] != org_wide_entry_a for e in resp.json())

    def test_update_blocked_for_guessed_pk_of_other_orgs_entry(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        self._enable_ai(tenant_a.org_id)
        self._enable_ai(tenant_b.org_id)
        entry_a = storage.create_knowledge_base_entry(tenant_a.org_id, tenant_a.unit_id, "Q", "A")

        login_as(client, tenant_b.staff_username)
        resp = client.patch(f"/api/knowledge/entries/{entry_a}", json={"title": "Hijacked", "content": "x"})
        assert resp.status_code in (403, 404)
        assert storage.get_knowledge_base_entry(entry_a)["title"] == "Q"

    def test_org_admin_cannot_manage_other_orgs_org_wide_entry(self, client, login_as, tenants):
        """The exact gap this suite exists to catch: an org-admin proving
        is_org_admin=True is not by itself enough to manage an org-wide
        (unit_id=NULL) entry - it must also be THAT entry's own org."""
        tenant_a, tenant_b = tenants
        self._enable_ai(tenant_a.org_id)
        self._enable_ai(tenant_b.org_id)
        org_wide_entry_a = storage.create_knowledge_base_entry(tenant_a.org_id, None, "Q", "A")

        login_as(client, tenant_b.org_admin_username)
        resp = client.patch(f"/api/knowledge/entries/{org_wide_entry_a}", json={"title": "Hijacked", "content": "x"})
        assert resp.status_code in (403, 404)
        assert storage.get_knowledge_base_entry(org_wide_entry_a)["title"] == "Q"

        resp = client.delete(f"/api/knowledge/entries/{org_wide_entry_a}")
        assert resp.status_code in (403, 404)
        assert storage.get_knowledge_base_entry(org_wide_entry_a) is not None

    def test_superadmin_sees_both_orgs_entries(self, client, login_as, tenants, superadmin_username):
        tenant_a, tenant_b = tenants
        self._enable_ai(tenant_a.org_id)
        self._enable_ai(tenant_b.org_id)
        entry_a = storage.create_knowledge_base_entry(tenant_a.org_id, tenant_a.unit_id, "Q", "A")
        entry_b = storage.create_knowledge_base_entry(tenant_b.org_id, tenant_b.unit_id, "Q", "A")

        login_as(client, superadmin_username)
        resp = client.get(f"/api/knowledge/entries?org_id={tenant_a.org_id}")
        assert resp.status_code == 200
        assert any(e["id"] == entry_a for e in resp.json())
        resp = client.get(f"/api/knowledge/entries?org_id={tenant_b.org_id}")
        assert resp.status_code == 200
        assert any(e["id"] == entry_b for e in resp.json())

    def test_list_source_chunks_excludes_other_orgs_rows(self, client, login_as, tenants):
        """A url/pdf document is many chunks sharing the same source_ref -
        even if both orgs happen to scrape the exact same URL as their own
        document, each org's chunks must stay confined to its own
        org_id/unit_id scope."""
        tenant_a, tenant_b = tenants
        self._enable_ai(tenant_a.org_id)
        self._enable_ai(tenant_b.org_id)
        same_url = "https://example.org/faq"
        storage.replace_knowledge_base_source_entries(
            tenant_a.org_id, tenant_a.unit_id, "url", same_url,
            [{"title": "A1", "content": "a1"}, {"title": "A2", "content": "a2"}],
        )
        storage.replace_knowledge_base_source_entries(
            tenant_b.org_id, tenant_b.unit_id, "url", same_url,
            [{"title": "B1", "content": "b1"}],
        )

        login_as(client, tenant_a.staff_username)
        resp = client.get(
            "/api/knowledge/sources/chunks",
            params={"source_type": "url", "source_ref": same_url, "unit_id": tenant_a.unit_id},
        )
        assert resp.status_code == 200
        chunks = resp.json()
        assert len(chunks) == 2
        assert {c["title"] for c in chunks} == {"A1", "A2"}

    def test_delete_source_blocked_for_guessed_other_orgs_source_ref(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        self._enable_ai(tenant_a.org_id)
        self._enable_ai(tenant_b.org_id)
        url = "https://example.org/private-doc"
        storage.replace_knowledge_base_source_entries(
            tenant_a.org_id, tenant_a.unit_id, "url", url,
            [{"title": "A1", "content": "a1"}, {"title": "A2", "content": "a2"}],
        )

        login_as(client, tenant_b.staff_username)
        resp = client.delete(
            "/api/knowledge/sources",
            params={"source_type": "url", "source_ref": url, "unit_id": tenant_b.unit_id},
        )
        assert resp.status_code == 404
        assert len(storage.list_knowledge_base_source_chunks(tenant_a.org_id, tenant_a.unit_id, "url", url)) == 2

    def test_toggle_source_blocked_for_guessed_other_orgs_source_ref(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        self._enable_ai(tenant_a.org_id)
        self._enable_ai(tenant_b.org_id)
        url = "https://example.org/private-doc-2"
        storage.replace_knowledge_base_source_entries(
            tenant_a.org_id, tenant_a.unit_id, "url", url,
            [{"title": "A1", "content": "a1"}],
        )

        login_as(client, tenant_b.org_admin_username)
        resp = client.post(
            "/api/knowledge/sources/active",
            json={"unit_id": tenant_b.unit_id, "source_type": "url", "source_ref": url, "is_active": False},
        )
        assert resp.status_code == 404
        chunks = storage.list_knowledge_base_source_chunks(tenant_a.org_id, tenant_a.unit_id, "url", url)
        assert chunks[0]["is_active"] == 1

    def test_org_admin_cannot_delete_other_orgs_org_wide_source(self, client, login_as, tenants):
        """The same is_org_admin-alone-is-not-enough gap the entry-level
        tests above cover, exercised through the whole-document delete
        path: an org-admin proving is_org_admin=True must still be pinned
        to their own org_id before an org-wide (unit_id=NULL) source can
        be touched."""
        tenant_a, tenant_b = tenants
        self._enable_ai(tenant_a.org_id)
        self._enable_ai(tenant_b.org_id)
        url = "https://example.org/global-doc"
        storage.replace_knowledge_base_source_entries(
            tenant_a.org_id, None, "url", url, [{"title": "A1", "content": "a1"}],
        )

        login_as(client, tenant_b.org_admin_username)
        resp = client.delete("/api/knowledge/sources", params={"source_type": "url", "source_ref": url})
        assert resp.status_code == 404
        assert len(storage.list_knowledge_base_source_chunks(tenant_a.org_id, None, "url", url)) == 1

    def test_delete_source_removes_every_chunk_for_own_org(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        self._enable_ai(tenant_a.org_id)
        url = "https://example.org/own-doc"
        storage.replace_knowledge_base_source_entries(
            tenant_a.org_id, tenant_a.unit_id, "url", url,
            [{"title": "A1", "content": "a1"}, {"title": "A2", "content": "a2"}],
        )

        login_as(client, tenant_a.org_admin_username)
        resp = client.delete(
            "/api/knowledge/sources",
            params={"source_type": "url", "source_ref": url, "unit_id": tenant_a.unit_id},
        )
        assert resp.status_code == 200
        assert storage.list_knowledge_base_source_chunks(tenant_a.org_id, tenant_a.unit_id, "url", url) == []


class TestAISettingsAndAutoReplyRulesIsolation:
    """/api/ai-settings/* - reuses numbers_router._get_number_if_authorized
    for unit-scoped access to a number, same precedent already covered by
    TestWhatsAppNumberAdmin/TestWhatsAppNumbersPage above for that helper's
    underlying scoping."""

    def _enable_ai(self, org_id: int) -> None:
        storage.grant(org_id, storage.MODULE_AI_ASSISTANT)
        storage.enable(org_id, storage.MODULE_AI_ASSISTANT)

    def test_ai_settings_blocked_for_guessed_pk_of_other_orgs_number(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        self._enable_ai(tenant_a.org_id)
        self._enable_ai(tenant_b.org_id)
        login_as(client, tenant_a.staff_username)
        resp = client.get(f"/api/ai-settings/{tenant_b.number_id}")
        assert resp.status_code == 403

    def test_auto_reply_rule_blocked_for_guessed_pk_of_other_orgs_number(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        self._enable_ai(tenant_a.org_id)
        self._enable_ai(tenant_b.org_id)
        rule_id = storage.create_ai_auto_reply_rule(tenant_b.number_id, "hours", "We're open 9-5.")

        login_as(client, tenant_a.staff_username)
        resp = client.get(f"/api/ai-settings/{tenant_b.number_id}/rules")
        assert resp.status_code == 403
        resp = client.patch(
            f"/api/ai-settings/{tenant_b.number_id}/rules/{rule_id}",
            json={"keyword": "hours", "response_text": "Hijacked"},
        )
        assert resp.status_code == 403
        assert storage.get_ai_auto_reply_rule(rule_id)["response_text"] == "We're open 9-5."


class TestRegistrationEventTemplatesIsolation:
    """/api/automations/registration-event-templates (Custom Registrations)
    - shares _check_unit_access/_check_number_access with the older
    form-mappings/registration-templates endpoints in automations_router.py,
    which predate this suite and were never themselves covered - this is
    first-time coverage for that shared scoping path, exercised through the
    newer endpoint."""

    def _enable_pco(self, org_id: int) -> None:
        storage.grant(org_id, storage.MODULE_PCO)
        storage.enable(org_id, storage.MODULE_PCO)

    def test_list_excludes_other_orgs_rows(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        self._enable_pco(tenant_a.org_id)
        self._enable_pco(tenant_b.org_id)
        mapping_a = storage.upsert_registration_event_template(
            mapping_id=None, unit_id=tenant_a.unit_id, pco_signup_id="signup-a",
            pco_signup_name="Event A", template_name="tmpl_a", body_variable_order=[],
            whatsapp_number_id=tenant_a.number_id, active=True,
        )
        mapping_b = storage.upsert_registration_event_template(
            mapping_id=None, unit_id=tenant_b.unit_id, pco_signup_id="signup-b",
            pco_signup_name="Event B", template_name="tmpl_b", body_variable_order=[],
            whatsapp_number_id=tenant_b.number_id, active=True,
        )

        login_as(client, tenant_a.staff_username)
        resp = client.get("/api/automations/registration-event-templates")
        assert resp.status_code == 200
        ids = {row["id"] for row in resp.json()}
        assert mapping_a in ids
        assert mapping_b not in ids

    def test_save_blocked_for_other_orgs_unit_and_number(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        self._enable_pco(tenant_a.org_id)
        self._enable_pco(tenant_b.org_id)
        login_as(client, tenant_a.staff_username)

        # Crafted unit_id belonging to org B.
        resp = client.post("/api/automations/registration-event-templates", json={
            "id": None, "unit_id": tenant_b.unit_id, "pco_signup_id": "signup-x",
            "pco_signup_name": "Event X", "template_name": "tmpl_x", "body_variable_order": [],
            "whatsapp_number_id": tenant_a.number_id, "button_variables": [], "header_image_url": None,
            "active": True, "language": "en",
        })
        assert resp.status_code == 403
        assert storage.get_registration_event_template(tenant_b.unit_id, "signup-x") is None

        # Own unit, but a crafted whatsapp_number_id belonging to org B -
        # the exact "dropdown was filtered but the POST wasn't" shape the
        # module docstring above warns about.
        resp = client.post("/api/automations/registration-event-templates", json={
            "id": None, "unit_id": tenant_a.unit_id, "pco_signup_id": "signup-y",
            "pco_signup_name": "Event Y", "template_name": "tmpl_y", "body_variable_order": [],
            "whatsapp_number_id": tenant_b.number_id, "button_variables": [], "header_image_url": None,
            "active": True, "language": "en",
        })
        assert resp.status_code == 403
        assert storage.get_registration_event_template(tenant_a.unit_id, "signup-y") is None

    def test_delete_blocked_for_guessed_pk_of_other_orgs_mapping(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        self._enable_pco(tenant_a.org_id)
        self._enable_pco(tenant_b.org_id)
        mapping_b = storage.upsert_registration_event_template(
            mapping_id=None, unit_id=tenant_b.unit_id, pco_signup_id="signup-b2",
            pco_signup_name="Event B2", template_name="tmpl_b2", body_variable_order=[],
            whatsapp_number_id=tenant_b.number_id, active=True,
        )

        login_as(client, tenant_a.staff_username)
        resp = client.delete(f"/api/automations/registration-event-templates/{mapping_b}")
        assert resp.status_code == 404
        assert storage.get_registration_event_template(tenant_b.unit_id, "signup-b2") is not None


class TestWabaUsageView:
    """/usage - superadmin-only, spans every org's send volume by design
    (see WabaUsageView's own docstring in admin_pages.py), so there's no
    per-tenant scoping to attack the way ScopedModelView-backed CRUD views
    have - the isolation property to check here is simply that a non-superadmin
    can't reach it at all, and that a superadmin's aggregate view genuinely
    spans both seeded orgs' real send data (storage.send_totals_by_number/
    daily_send_counts, not the old per-WABA message_log query)."""

    def test_org_admin_cannot_reach_usage(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.org_admin_username)
        resp = client.get("/usage")
        assert resp.status_code == 403

    def test_plain_staff_cannot_reach_usage(self, client, login_as, tenants):
        tenant_a, _tenant_b = tenants
        login_as(client, tenant_a.staff_username)
        resp = client.get("/usage")
        assert resp.status_code == 403

    def test_superadmin_sees_both_orgs_send_totals(self, client, login_as, tenants, superadmin_username):
        tenant_a, tenant_b = tenants
        storage.record_send(tenant_a.unit_id, "form_webhook", "sent", whatsapp_number_id=tenant_a.number_id)
        storage.record_send(tenant_b.unit_id, "form_webhook", "sent", whatsapp_number_id=tenant_b.number_id)

        login_as(client, superadmin_username)
        resp = client.get("/usage", params={"days": 1})
        assert resp.status_code == 200
        assert tenant_a.number_label in resp.text
        assert tenant_b.number_label in resp.text
        assert tenant_a.unit_name in resp.text
        assert tenant_b.unit_name in resp.text

    def test_superadmin_sees_both_orgs_ai_usage(self, client, login_as, tenants, superadmin_username):
        tenant_a, tenant_b = tenants
        storage.record_ai_reply(
            whatsapp_number_id=tenant_a.number_id, conversation_id=None, inbound_message_id=None,
            source="live", sent=True, prompt_tokens=11111, completion_tokens=2222, model="claude-sonnet-5",
        )
        storage.record_ai_reply(
            whatsapp_number_id=tenant_b.number_id, conversation_id=None, inbound_message_id=None,
            source="keyword", sent=True,
        )
        storage.record_ai_ingestion(
            org_id=tenant_a.org_id, unit_id=None, source_type="scrape", source_ref="https://example.com",
            prompt_tokens=33333, completion_tokens=4444, model="claude-sonnet-5",
        )
        storage.record_ai_ingestion(
            org_id=tenant_b.org_id, unit_id=None, source_type="pdf", source_ref="doc.pdf",
            prompt_tokens=55555, completion_tokens=6666, model="claude-sonnet-5",
        )

        login_as(client, superadmin_username)
        resp = client.get("/usage", params={"days": 1})
        assert resp.status_code == 200
        # AI Auto-Reply card: tenant A's live-reply tokens.
        assert tenant_a.org_name in resp.text
        assert "11,111" in resp.text
        # Keyword Auto-Replies card: tenant B's keyword reply count.
        assert tenant_b.org_name in resp.text
        # Knowledge Base Ingestion card: both orgs' ingestion tokens.
        assert "33,333" in resp.text
        assert "55,555" in resp.text


class TestAIPlaygroundIsolation:
    """/api/ai/units and /api/ai/playground (web/ai_playground_router.py) -
    scoped by reusing web/numbers_router.py's own
    _accessible_units/_check_unit_access rather than a local copy (see
    that router module's docstring). Both tenants' orgs are granted+
    enabled for MODULE_AI_ASSISTANT in every test here, so what's under
    test is the unit scoping itself, not the module gate (covered
    separately elsewhere)."""

    def _enable_ai(self, org_id: int) -> None:
        storage.grant(org_id, storage.MODULE_AI_ASSISTANT)
        storage.enable(org_id, storage.MODULE_AI_ASSISTANT)

    def test_units_list_excludes_other_orgs_unit(self, client, login_as, tenants):
        tenant_a, tenant_b = tenants
        self._enable_ai(tenant_a.org_id)
        self._enable_ai(tenant_b.org_id)

        login_as(client, tenant_a.staff_username)
        resp = client.get("/api/ai/units")
        assert resp.status_code == 200
        ids = {u["id"] for u in resp.json()}
        assert tenant_a.unit_id in ids
        assert tenant_b.unit_id not in ids

    def test_playground_blocked_for_guessed_other_orgs_unit(self, client, login_as, tenants):
        """The exact bug class this page exists to guard against: a staff
        member from org B crafting a POST with org A's unit_id directly -
        the dropdown on the page itself would never offer it, but a raw
        POST must still be rejected server-side, before org A's knowledge
        base is ever touched (never reaches generate_ai_response)."""
        tenant_a, tenant_b = tenants
        self._enable_ai(tenant_a.org_id)
        self._enable_ai(tenant_b.org_id)
        storage.create_knowledge_base_entry(tenant_a.org_id, tenant_a.unit_id, "Secret Q", "Secret A")

        login_as(client, tenant_b.staff_username)
        resp = client.post("/api/ai/playground", json={"unit_id": tenant_a.unit_id, "message": "hello"})
        assert resp.status_code == 403

    def test_playground_blocked_for_own_unit_but_other_orgs_number(self, client, login_as, tenants):
        """Same 'dropdown filtered but POST wasn't' shape covered elsewhere
        in this suite - a legitimately accessible unit_id paired with a
        crafted whatsapp_number_id from another org must still be
        rejected, before any AI call is made."""
        tenant_a, tenant_b = tenants
        self._enable_ai(tenant_a.org_id)
        self._enable_ai(tenant_b.org_id)

        login_as(client, tenant_a.staff_username)
        resp = client.post("/api/ai/playground", json={
            "unit_id": tenant_a.unit_id, "whatsapp_number_id": tenant_b.number_id, "message": "hello",
        })
        assert resp.status_code == 400

    def test_org_admin_cannot_reach_other_orgs_unit_via_playground(self, client, login_as, tenants):
        """is_org_admin alone is not enough to prove access - it must also
        be THAT unit's own org (same shape TestKnowledgeBaseIsolation's
        org-wide-entry test guards, applied to this page)."""
        tenant_a, tenant_b = tenants
        self._enable_ai(tenant_a.org_id)
        self._enable_ai(tenant_b.org_id)

        login_as(client, tenant_b.org_admin_username)
        resp = client.post("/api/ai/playground", json={"unit_id": tenant_a.unit_id, "message": "hello"})
        assert resp.status_code == 403

    def test_superadmin_sees_every_orgs_unit(self, client, login_as, tenants, superadmin_username):
        tenant_a, tenant_b = tenants
        self._enable_ai(tenant_a.org_id)
        self._enable_ai(tenant_b.org_id)

        login_as(client, superadmin_username)
        resp = client.get("/api/ai/units")
        assert resp.status_code == 200
        ids = {u["id"] for u in resp.json()}
        assert tenant_a.unit_id in ids
        assert tenant_b.unit_id in ids

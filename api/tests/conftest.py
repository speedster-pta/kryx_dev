"""Shared fixtures for the admin test suite.

DATABASE_PATH must be redirected to a throwaway sqlite file *before*
anything under `autosend` is imported for the first time - admin_models.py
builds its SQLAlchemy `engine` at import time from
autosend.storage.DB_PATH, which is itself resolved from settings at import
time. There is no supported way to repoint an already-constructed engine,
so this has to happen at module load of this conftest, which pytest
always imports before any test module.
"""
import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

_tmp_dir = tempfile.mkdtemp(prefix="kryx-admin-tests-")
os.environ["DATABASE_PATH"] = os.path.join(_tmp_dir, "test.db")
os.environ.setdefault("ENABLE_POLLER", "false")

# config.py's Settings requires these (no defaults, by design - see
# config.py) so every environment, including tests, must supply real-shaped
# values explicitly. These are test-only, not read anywhere outside this
# process.
os.environ.setdefault("ADMIN_API_KEY", "test-admin-api-key")
os.environ.setdefault("SESSION_SECRET_KEY", "test-session-secret-key")
os.environ.setdefault(
    "TOKEN_ENCRYPTION_KEY", "Wk1Yc0RROVR5VjFmUTZkU1lHdHhzVjA0THNudVJxd3E="
)
os.environ.setdefault("EMAIL_WA_WEBHOOK_SECRET", "test-email-wa-webhook-secret")
os.environ.setdefault(
    "GENERIC_EMAIL_WA_WEBHOOK_SECRET", "test-generic-email-wa-webhook-secret"
)

import bcrypt  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from autosend.core.db_init import init_db  # noqa: E402
from autosend.main import app  # noqa: E402
from autosend.admin_models import (  # noqa: E402
    engine,
    BillingPlan,
    Organisation,
    PCOOrganizationSettings,
    Subscription,
    Unit,
    User,
    WhatsAppNumber,
    user_units_table,
)

init_db()

# One fixed password for every test user - simpler than hashing a unique
# password per user, and there's nothing under test that cares what the
# password actually is.
PASSWORD = "correct-horse-battery-staple"
_PASSWORD_HASH = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt()).decode()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Tenant:
    """Plain-data snapshot of one organisation's fixture rows - not live
    ORM instances, so nothing here can trigger a DetachedInstanceError or
    an accidental lazy-load once the seeding session has closed."""

    org_id: int
    org_name: str
    unit_id: int
    unit_name: str
    number_id: int
    number_label: str
    pco_settings_id: int
    staff_username: str
    staff_id: int
    org_admin_username: str
    org_admin_id: int


def _get_or_create_test_plan(session: Session) -> BillingPlan:
    """A generously-limited plan reused across every seeded tenant, so
    billing/entitlements.py's per-org resource limits (added alongside
    this fixture's original provisioning/isolation tests) don't collide
    with what these tests actually seed/exercise per org (a unit + a
    number + two users already at seed time, plus whatever the test
    itself then tries to add on top). Not what real orgs get by default
    (see entitlements.py's own DEFAULT_* constants) - just a fixture
    convenience so tenants behave like a well-provisioned, paying org
    rather than one sitting exactly at the standard 1/1/1 entitlement."""
    plan = session.query(BillingPlan).filter_by(key="test-fixture-unlimited").one_or_none()
    if plan is not None:
        return plan
    plan = BillingPlan(
        key="test-fixture-unlimited",
        name="Test Fixture (effectively unlimited)",
        price_cents=0,
        interval="monthly",
        active=True,
        base_users=1000,
        base_numbers=1000,
        base_units=1000,
        message_quota=1_000_000,
        quota_period_days=30,
        created_at=_now(),
    )
    session.add(plan)
    session.flush()
    return plan


def _seed_tenant(session: Session, tag: str) -> Tenant:
    # Deliberately no subscription/plan row here - tenants must start
    # with NO subscription at all, since test_billing.py::TestIsOrgCurrent
    # exercises exactly that state (see its test_no_subscription_is_not_current
    # etc). Tests that need to exercise billing/entitlements.py's resource
    # limits at something other than the standard 1/1/1 defaults should
    # use the grant_unlimited_capacity fixture below on a per-test basis
    # instead of this shared seeding helper.
    org = Organisation(name=f"Org {tag}", slug=f"org-{tag}", active=True, created_at=_now())
    session.add(org)
    session.flush()

    unit = Unit(org_id=org.id, slug=f"unit-{tag}", name=f"Unit {tag}", active=True, created_at=_now())
    session.add(unit)
    session.flush()

    number = WhatsAppNumber(
        unit_id=unit.id,
        label=f"Number {tag}",
        phone_number_id=f"phone-{tag}",
        active=True,
        created_at=_now(),
    )
    session.add(number)

    pco_settings = PCOOrganizationSettings(org_id=org.id, pco_token_id=f"pco-token-{tag}", created_at=_now())
    session.add(pco_settings)

    staff = User(
        org_id=org.id,
        username=f"staff-{tag}",
        password_hash=_PASSWORD_HASH,
        is_superadmin=False,
        is_org_admin=False,
        active=True,
        created_at=_now(),
    )
    org_admin = User(
        org_id=org.id,
        username=f"orgadmin-{tag}",
        password_hash=_PASSWORD_HASH,
        is_superadmin=False,
        is_org_admin=True,
        active=True,
        created_at=_now(),
    )
    session.add(staff)
    session.add(org_admin)
    session.flush()

    session.execute(user_units_table.insert().values(user_id=staff.id, unit_id=unit.id))
    session.commit()

    return Tenant(
        org_id=org.id,
        org_name=org.name,
        unit_id=unit.id,
        unit_name=unit.name,
        number_id=number.id,
        number_label=number.label,
        pco_settings_id=pco_settings.id,
        staff_username=staff.username,
        staff_id=staff.id,
        org_admin_username=org_admin.username,
        org_admin_id=org_admin.id,
    )


@pytest.fixture()
def tenants():
    """Two fully independent organisations (a, b), each with its own unit,
    WhatsApp number, PCO settings row, plain unit-scoped staff user and
    org-admin user. Freshly tagged with a uuid on every call so tests never
    share rows (and therefore never interfere with each other) even though
    they all run against the one long-lived sqlite file set up above."""
    tag = uuid.uuid4().hex[:8]
    with Session(engine) as session:
        tenant_a = _seed_tenant(session, f"a-{tag}")
        tenant_b = _seed_tenant(session, f"b-{tag}")
    return tenant_a, tenant_b


@pytest.fixture()
def grant_unlimited_capacity():
    """Opt-in helper (not applied by the tenants fixture itself - see its
    own docstring) for tests that need an org to be able to provision more
    than the standard 1 user / 1 number / 1 unit entitlement (see
    billing/entitlements.py) without that being the thing under test.
    Returns a callable(org_id) that gives org_id an active subscription
    on a generously-limited plan; upserts (via storage.create_subscription's
    own org_id-unique upsert) so it's safe to call more than once for the
    same org."""
    def _grant(org_id: int) -> None:
        with Session(engine) as session:
            plan = _get_or_create_test_plan(session)
            plan_id = plan.id
            session.commit()
        from autosend import storage

        storage.create_subscription(org_id, plan_id=plan_id, status="active")

    return _grant


@pytest.fixture()
def superadmin_username():
    tag = uuid.uuid4().hex[:8]
    username = f"root-{tag}"
    with Session(engine) as session:
        session.add(
            User(
                org_id=None,
                username=username,
                password_hash=_PASSWORD_HASH,
                is_superadmin=True,
                is_org_admin=False,
                active=True,
                created_at=_now(),
            )
        )
        session.commit()
    return username


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def login_as():
    """Returns a helper that logs the given TestClient in as `username`
    (fixed PASSWORD for every fixture user) via the app's one real login
    path (SQLAdmin's own /login, see admin_auth.AdminAuth.login) and
    returns that same client, now holding an authenticated session
    cookie."""

    def _login(client: TestClient, username: str) -> TestClient:
        response = client.post(
            "/login",
            data={"username": username, "password": PASSWORD},
            follow_redirects=False,
        )
        assert response.status_code in (302, 303), (
            f"login failed for {username!r}: {response.status_code} {response.text[:300]}"
        )
        return client

    return _login


def db_session() -> Session:
    return Session(engine)

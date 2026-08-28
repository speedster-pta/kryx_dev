"""Signup email verification (web/signup_router.py's /signup/verify route
and storage/email_verification.py) - covers that verifying doesn't block
login, that an invalid/expired/reused token is rejected, and that
verifying after a successful payment finishes activating the org (the
other half of billing/engine.py's email-verification gate)."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from autosend import storage
from autosend.admin_models import engine


_SIGNUP_PASSWORD = "Correct-Horse-Battery-9"


def _signup(client, tag):
    org_name = f"Signup Org {tag}"
    username = f"signup-user-{tag}"
    email = f"{username}@example.com"
    response = client.post(
        "/signup",
        data={
            "org_name": org_name,
            "username": username,
            "email": email,
            "password": _SIGNUP_PASSWORD,
            "confirm_password": _SIGNUP_PASSWORD,
            "accept_terms": "on",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text[:300]
    user = storage.get_user(username)
    assert user is not None
    return user, email


def test_signup_creates_unverified_user_but_still_logs_in(client):
    tag = uuid.uuid4().hex[:8]
    user, _ = _signup(client, tag)
    assert user["email_verified_at"] is None
    # Login already happened as part of signup - /signup/plan is reachable,
    # proving the session was established despite being unverified.
    plan_response = client.get("/signup/plan", follow_redirects=False)
    assert plan_response.status_code == 200


def test_valid_token_verifies_email_and_can_only_be_used_once(client):
    tag = uuid.uuid4().hex[:8]
    user, _ = _signup(client, tag)
    token = storage.create_email_verification_token(user["id"])

    response = client.get(f"/signup/verify?token={token}", follow_redirects=False)
    assert response.status_code == 200
    assert "verified" in response.text.lower()

    refreshed = storage.get_user_by_id(user["id"])
    assert refreshed["email_verified_at"] is not None

    # Same token again - already used, must be rejected, not silently re-accepted.
    replay_response = client.get(f"/signup/verify?token={token}", follow_redirects=False)
    assert replay_response.status_code == 400


def test_invalid_token_is_rejected(client):
    response = client.get("/signup/verify?token=not-a-real-token", follow_redirects=False)
    assert response.status_code == 400


def test_expired_token_is_rejected(client):
    tag = uuid.uuid4().hex[:8]
    user, _ = _signup(client, tag)
    token = storage.create_email_verification_token(user["id"])
    # Force the token into the past directly - simpler and faster than
    # waiting out the real 24h TTL.
    with engine.connect() as conn:
        conn.exec_driver_sql(
            "UPDATE email_verification_tokens SET expires_at = ? WHERE token = ?",
            ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(), token),
        )
        conn.commit()

    response = client.get(f"/signup/verify?token={token}", follow_redirects=False)
    assert response.status_code == 400
    assert storage.get_user_by_id(user["id"])["email_verified_at"] is None


def test_verifying_after_payment_activates_org(client):
    """Mirrors billing/engine.py's confirm_payment(): a subscription already
    marked 'active' (payment succeeded) couldn't flip is_org_active because
    the org wasn't verified yet - verifying now should finish the job."""
    tag = uuid.uuid4().hex[:8]
    user, _ = _signup(client, tag)
    org_id = user["org_id"]
    assert storage.is_org_active(org_id) is False

    storage.create_subscription(org_id, plan_id=None, status="active")
    assert storage.is_org_active(org_id) is False  # payment alone isn't enough

    token = storage.create_email_verification_token(user["id"])
    response = client.get(f"/signup/verify?token={token}", follow_redirects=False)
    assert response.status_code == 200
    assert storage.is_org_active(org_id) is True


def test_resend_verification_requires_login(client):
    response = client.post("/api/account/resend-verification", follow_redirects=False)
    assert response.status_code == 303


def test_resend_verification_sends_new_token_for_logged_in_user(client):
    # /signup itself already establishes a logged-in session (see
    # signup_router.signup_submit) - no separate login_as() needed, and
    # login_as's fixed fixture PASSWORD wouldn't match this user anyway.
    tag = uuid.uuid4().hex[:8]
    user, email = _signup(client, tag)

    response = client.post("/api/account/resend-verification")
    # Mailtrap isn't configured in the test DB, so the send itself fails -
    # this only asserts the endpoint reaches that point (auth + rate-limit
    # + lookup all passed) rather than asserting a real email went out.
    assert response.status_code in (200, 502)

"""The "Activar vigilancia" single-button flow: someone leaves an email
right after a free check (index.html's wireEmail -> POST /api/waitlist
with the checked value), we pre-attach that DNI/NIE/matrícula to their
account as an inactive MonitoredId the moment they first log in
(main._pre_attach_waitlist_target), and billing.apply_event activates it
(plus runs the first check) the moment a real checkout completes — instead
of asking them to type the same value a second time on the dashboard.
See main._has_pending_target for the /api/auth/me flag cuenta.html reads
to show the single-button CTA instead of the full "add a target" form."""
from __future__ import annotations

import os
from unittest.mock import patch

from cryptography.fernet import Fernet

os.environ.setdefault("TARGET_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ["DATABASE_URL"] = "sqlite:////tmp/test_pending_target.db"
if os.path.exists("/tmp/test_pending_target.db"):
    os.remove("/tmp/test_pending_target.db")

import pytest
from fastapi.testclient import TestClient

from app import auth, billing, crypto, main
from app.db import SessionLocal, get_db, init_db
from app.models import CheckoutAttempt, MonitoredId, Plan, SubscriptionStatus, User, WaitlistSignup


@pytest.fixture()
def db_session():
    init_db()
    session = SessionLocal()
    try:
        yield session
    finally:
        session.query(MonitoredId).delete()
        session.query(WaitlistSignup).delete()
        session.query(CheckoutAttempt).delete()
        session.query(User).delete()
        session.commit()
        session.close()


@pytest.fixture()
def client(db_session):
    def _get_db_override():
        yield db_session

    main.app.dependency_overrides[get_db] = _get_db_override
    yield TestClient(main.app)
    main.app.dependency_overrides.pop(get_db, None)


def test_waitlist_stores_the_encrypted_value_when_given_one(db_session, client):
    resp = client.post("/api/waitlist", json={"email": "a@example.com", "context": "ok", "value": "12345678Z"})
    assert resp.status_code == 200

    row = db_session.query(WaitlistSignup).filter_by(email="a@example.com").first()
    assert row.value_encrypted is not None
    assert crypto.decrypt_value(row.value_encrypted) == "12345678Z"


def test_waitlist_without_a_value_still_works_like_before(db_session, client):
    # The pricing-card email capture (no free check behind it) never sends one.
    resp = client.post("/api/waitlist", json={"email": "b@example.com", "context": "plan_individual"})
    assert resp.status_code == 200

    row = db_session.query(WaitlistSignup).filter_by(email="b@example.com").first()
    assert row.value_encrypted is None


def test_waitlist_with_an_unvalidatable_value_still_captures_the_email(db_session, client):
    resp = client.post("/api/waitlist", json={"email": "c@example.com", "context": "ok", "value": "not-a-real-id"})
    assert resp.status_code == 200

    row = db_session.query(WaitlistSignup).filter_by(email="c@example.com").first()
    assert row is not None
    assert row.value_encrypted is None


def test_first_login_pre_attaches_the_latest_waitlist_value_as_an_inactive_target(db_session):
    db_session.add(WaitlistSignup(email="d@example.com", context="ok", value_encrypted=crypto.encrypt_value("11111111H")))
    db_session.add(WaitlistSignup(email="d@example.com", context="alert", value_encrypted=crypto.encrypt_value("22222222J")))
    db_session.commit()

    profile = User(id="u-d", email="d@example.com")
    db_session.add(profile)
    db_session.commit()

    main._pre_attach_waitlist_target(db_session, profile)

    targets = db_session.query(MonitoredId).filter_by(user_id="u-d").all()
    assert len(targets) == 1
    assert targets[0].active is False
    assert crypto.decrypt_value(targets[0].value_encrypted) == "22222222J"  # most recent signup wins


def test_pre_attach_assigns_an_ab_test_variant(db_session):
    db_session.add(WaitlistSignup(email="ab@example.com", context="ok", value_encrypted=crypto.encrypt_value("77777777Q")))
    db_session.commit()
    profile = User(id="u-ab", email="ab@example.com")
    db_session.add(profile)
    db_session.commit()

    main._pre_attach_waitlist_target(db_session, profile)

    assert profile.ab_pending_cta_variant in ("button", "direct")


def test_pre_attach_without_a_matching_value_never_assigns_a_variant(db_session):
    profile = User(id="u-noab", email="noab@example.com")
    db_session.add(profile)
    db_session.commit()

    main._pre_attach_waitlist_target(db_session, profile)

    assert profile.ab_pending_cta_variant is None


def test_pre_attach_is_a_noop_without_a_matching_waitlist_value(db_session):
    profile = User(id="u-e", email="e@example.com")
    db_session.add(profile)
    db_session.commit()

    main._pre_attach_waitlist_target(db_session, profile)

    assert db_session.query(MonitoredId).filter_by(user_id="u-e").count() == 0


def test_pre_attach_never_duplicates_on_a_second_call(db_session):
    db_session.add(WaitlistSignup(email="f@example.com", context="ok", value_encrypted=crypto.encrypt_value("33333333P")))
    db_session.commit()
    profile = User(id="u-f", email="f@example.com")
    db_session.add(profile)
    db_session.commit()

    main._pre_attach_waitlist_target(db_session, profile)
    main._pre_attach_waitlist_target(db_session, profile)

    assert db_session.query(MonitoredId).filter_by(user_id="u-f").count() == 1


def test_has_pending_target_true_only_with_no_plan_and_an_existing_target(db_session):
    no_plan_user = User(id="u-g", email="g@example.com")
    db_session.add(no_plan_user)
    db_session.add(MonitoredId(user_id="u-g", value_encrypted=crypto.encrypt_value("44444444X"), active=False))

    subscribed_user = User(
        id="u-h", email="h@example.com", plan=Plan.individual, subscription_status=SubscriptionStatus.trialing,
    )
    db_session.add(subscribed_user)
    db_session.add(MonitoredId(user_id="u-h", value_encrypted=crypto.encrypt_value("55555555Z"), active=True))
    db_session.commit()

    assert main._has_pending_target(db_session, no_plan_user) is True
    assert main._has_pending_target(db_session, subscribed_user) is False
    assert main._has_pending_target(db_session, None) is False


def test_me_endpoint_reports_the_pending_target_and_its_variant(db_session, client):
    profile = User(id="u-me", email="me@example.com")
    db_session.add(profile)
    db_session.add(MonitoredId(user_id="u-me", value_encrypted=crypto.encrypt_value("88888888W"), active=False))
    profile.ab_pending_cta_variant = "direct"
    db_session.commit()

    main.app.dependency_overrides[auth.get_current_user] = lambda: auth.AuthUser(id="u-me", email="me@example.com")
    try:
        resp = client.get("/api/auth/me")
    finally:
        main.app.dependency_overrides.pop(auth.get_current_user, None)

    assert resp.status_code == 200
    data = resp.json()
    assert data["has_pending_target"] is True
    assert data["pending_target_variant"] == "direct"


def test_me_endpoint_picks_up_a_waitlist_value_left_after_the_account_already_existed(db_session, client):
    # Exactly the bug a real user hit: already logged in (session cookie
    # still valid, so no fresh /api/auth/session -> _ensure_profile call
    # happens at all), does ANOTHER free check with the same email, and
    # only then hits /api/auth/me on a page reload. This must still pick
    # up the new value — pre-attaching can't be limited to the moment the
    # account was first created.
    profile = User(id="u-returning", email="returning@example.com")
    db_session.add(profile)
    db_session.commit()

    main.app.dependency_overrides[auth.get_current_user] = lambda: auth.AuthUser(
        id="u-returning", email="returning@example.com"
    )
    try:
        resp = client.get("/api/auth/me")
        assert resp.json()["has_pending_target"] is False  # nothing waiting yet

        # A free check + email capture happens *after* that first check-in.
        db_session.add(WaitlistSignup(
            email="returning@example.com", context="ok", value_encrypted=crypto.encrypt_value("99999999R"),
        ))
        db_session.commit()

        resp = client.get("/api/auth/me")
    finally:
        main.app.dependency_overrides.pop(auth.get_current_user, None)

    data = resp.json()
    assert data["has_pending_target"] is True
    target = db_session.query(MonitoredId).filter_by(user_id="u-returning").first()
    assert crypto.decrypt_value(target.value_encrypted) == "99999999R"
    assert target.active is False


def test_me_endpoint_variant_is_null_without_a_pending_target(db_session, client):
    profile = User(id="u-me2", email="me2@example.com")
    db_session.add(profile)
    db_session.commit()

    main.app.dependency_overrides[auth.get_current_user] = lambda: auth.AuthUser(id="u-me2", email="me2@example.com")
    try:
        resp = client.get("/api/auth/me")
    finally:
        main.app.dependency_overrides.pop(auth.get_current_user, None)

    data = resp.json()
    assert data["has_pending_target"] is False
    assert data["pending_target_variant"] is None


def test_admin_billing_reports_ab_test_stats_per_variant(db_session, client):
    # button: 2 assigned, 1 started checkout, 0 activated
    b1 = User(id="u-b1", email="b1@example.com", ab_pending_cta_variant="button")
    b2 = User(id="u-b2", email="b2@example.com", ab_pending_cta_variant="button")
    # direct: 1 assigned, 1 started checkout, 1 activated (trialing)
    d1 = User(
        id="u-d1", email="d1@example.com", ab_pending_cta_variant="direct",
        subscription_status=SubscriptionStatus.trialing, plan=Plan.individual,
    )
    db_session.add_all([b1, b2, d1])
    db_session.add(CheckoutAttempt(user_id="u-b1", plan="individual"))
    db_session.add(CheckoutAttempt(user_id="u-d1", plan="individual"))
    db_session.commit()

    main.app.dependency_overrides[auth.require_admin] = lambda: auth.AuthUser(id="admin", email="admin@example.com")
    try:
        resp = client.get("/api/admin/billing")
    finally:
        main.app.dependency_overrides.pop(auth.require_admin, None)

    assert resp.status_code == 200
    by_variant = {row["variant"]: row for row in resp.json()["ab_pending_cta"]}
    assert by_variant["button"] == {"variant": "button", "assigned": 2, "checkout_started": 1, "activated": 0}
    assert by_variant["direct"] == {"variant": "direct", "assigned": 1, "checkout_started": 1, "activated": 1}


def test_checkout_completion_activates_the_pending_target_and_checks_it(db_session):
    user = User(id="u-i", email="i@example.com")
    db_session.add(user)
    db_session.add(MonitoredId(id="target-i", user_id="u-i", value_encrypted=crypto.encrypt_value("66666666B"), active=False))
    db_session.commit()

    event = {
        "type": "checkout.session.completed",
        "data": {"object": {
            "metadata": {"user_id": "u-i", "plan": "individual"},
            "customer": "cus_i",
            "customer_details": {},
        }},
    }
    with patch.object(billing.monitoring, "check_target") as fake_check:
        billing.apply_event(db_session, event)
        assert fake_check.call_count == 1
        assert fake_check.call_args[0][1].id == "target-i"

    target = db_session.query(MonitoredId).filter_by(id="target-i").first()
    assert target.active is True

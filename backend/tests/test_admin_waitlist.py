"""The free-check email-capture funnel (waitlist_signups, context
"ok"/"alert" — set client-side from the exact check result) is folded
into /api/admin/users rather than living in its own endpoint/table — see
main._latest_waitlist_by_email and admin_users's docstring. Every row
(real account, pending signup, or waitlist-only) can carry
free_check_context if that email ever left one; a waitlist-only email
(never even requested a magic link) gets its own row with
has_account=False."""
from __future__ import annotations

import os
from datetime import datetime

from cryptography.fernet import Fernet

os.environ.setdefault("TARGET_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ["DATABASE_URL"] = "sqlite:////tmp/test_admin_waitlist.db"
if os.path.exists("/tmp/test_admin_waitlist.db"):
    os.remove("/tmp/test_admin_waitlist.db")

import pytest
from fastapi.testclient import TestClient

from app import auth, main
from app.db import SessionLocal, get_db, init_db
from app.models import Plan, SubscriptionStatus, User, WaitlistSignup


@pytest.fixture()
def db_session():
    init_db()
    session = SessionLocal()
    try:
        yield session
    finally:
        session.query(WaitlistSignup).delete()
        session.query(User).delete()
        session.commit()
        session.close()


@pytest.fixture()
def client(db_session):
    main.app.dependency_overrides[auth.require_admin] = lambda: auth.AuthUser(id="admin", email="admin@test.com")

    def _get_db_override():
        yield db_session

    main.app.dependency_overrides[get_db] = _get_db_override
    yield TestClient(main.app)
    main.app.dependency_overrides.pop(auth.require_admin, None)
    main.app.dependency_overrides.pop(get_db, None)


def test_waitlist_context_attaches_to_a_matching_real_account(db_session, client):
    db_session.add(WaitlistSignup(email="ALERT@example.com", context="alert", created_at=datetime.utcnow()))
    db_session.add(
        User(
            id="u-1",
            email="alert@example.com",  # different case, must still match
            created_at=datetime.utcnow(),
            plan=Plan.individual,
            subscription_status=SubscriptionStatus.active,
        )
    )
    db_session.commit()

    resp = client.get("/api/admin/users")
    assert resp.status_code == 200
    rows = {r["email"].lower(): r for r in resp.json()}

    row = rows["alert@example.com"]
    assert row["free_check_context"] == "alert"
    assert row["has_account"] is True
    assert row["email_confirmed"] is True
    assert row["plan"] == "individual"


def test_waitlist_only_email_gets_its_own_sin_cuenta_row(db_session, client):
    db_session.add(WaitlistSignup(email="ok@example.com", context="ok", created_at=datetime.utcnow()))
    db_session.commit()

    resp = client.get("/api/admin/users")
    rows = {r["email"]: r for r in resp.json()}

    row = rows["ok@example.com"]
    assert row["free_check_context"] == "ok"
    assert row["has_account"] is False
    assert row["plan"] is None
    assert row["targets_count"] == 0


def test_user_with_no_waitlist_signup_has_no_free_check_context(db_session, client):
    db_session.add(
        User(
            id="u-2",
            email="nobody@example.com",
            created_at=datetime.utcnow(),
            subscription_status=SubscriptionStatus.none,
        )
    )
    db_session.commit()

    resp = client.get("/api/admin/users")
    rows = {r["email"]: r for r in resp.json()}

    assert rows["nobody@example.com"]["free_check_context"] is None
    assert rows["nobody@example.com"]["has_account"] is True

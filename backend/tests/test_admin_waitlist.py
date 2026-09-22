"""main.admin_waitlist links a free-check email capture (waitlist_signups,
context "ok"/"alert" — set client-side from the exact check result) to
whether that email went on to create a real account. See its docstring
in app/main.py for why context doubles as the found/not-found signal
without needing a separate correlation key."""
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


def test_waitlist_shows_context_and_account_status(db_session, client):
    db_session.add(WaitlistSignup(email="alert@example.com", context="alert", created_at=datetime.utcnow()))
    db_session.add(WaitlistSignup(email="ok@example.com", context="ok", created_at=datetime.utcnow()))
    db_session.add(
        User(
            id="u-1",
            email="ALERT@example.com",  # different case, must still match
            created_at=datetime.utcnow(),
            plan=Plan.individual,
            subscription_status=SubscriptionStatus.active,
        )
    )
    db_session.commit()

    resp = client.get("/api/admin/waitlist")
    assert resp.status_code == 200
    rows = {r["email"]: r for r in resp.json()}

    assert rows["alert@example.com"]["context"] == "alert"
    assert rows["alert@example.com"]["has_account"] is True
    assert rows["alert@example.com"]["plan"] == "individual"
    assert rows["alert@example.com"]["subscription_status"] == "active"

    assert rows["ok@example.com"]["context"] == "ok"
    assert rows["ok@example.com"]["has_account"] is False
    assert rows["ok@example.com"]["plan"] is None
    assert rows["ok@example.com"]["subscription_status"] is None

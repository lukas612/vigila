"""Someone who requests a magic link but never clicks it never gets a
public.users row (see main._ensure_profile), so without main._pending_signups
they'd be invisible in the admin panel even though they're a real signup
attempt. See main.admin_users and its docstring."""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from types import SimpleNamespace

from cryptography.fernet import Fernet

os.environ.setdefault("TARGET_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ["DATABASE_URL"] = "sqlite:////tmp/test_admin_pending_signups.db"
if os.path.exists("/tmp/test_admin_pending_signups.db"):
    os.remove("/tmp/test_admin_pending_signups.db")

import pytest
from fastapi.testclient import TestClient

from app import auth, main
from app.db import SessionLocal, init_db
from app.main import _pending_signups, app
from app.models import User
from app.schemas import AdminUserOut


@pytest.fixture()
def db_session():
    init_db()
    session = SessionLocal()
    try:
        yield session
    finally:
        session.query(User).delete()
        session.commit()
        session.close()


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


def _auth_row(user_id: str, email: str, days_ago: int = 1):
    return SimpleNamespace(id=user_id, email=email, created_at=datetime.utcnow() - timedelta(days=days_ago))


def test_pending_signups_excludes_ids_that_already_have_a_profile(db_session, monkeypatch):
    monkeypatch.setattr(
        db_session,
        "execute",
        lambda *a, **k: _FakeResult([_auth_row("has-profile", "has-profile@example.com"),
                                      _auth_row("no-profile", "no-profile@example.com")]),
    )

    pending = _pending_signups(db_session, known_ids={"has-profile"})

    assert [p.email for p in pending] == ["no-profile@example.com"]
    assert pending[0].email_confirmed is False
    assert pending[0].plan is None
    assert pending[0].subscription_status == "none"


def test_pending_signups_returns_empty_list_when_auth_schema_unavailable(db_session):
    # Real behavior on SQLite (no `auth` schema) — must not raise.
    pending = _pending_signups(db_session, known_ids=set())
    assert pending == []


def test_admin_users_merges_and_sorts_confirmed_and_pending_by_created_at(db_session, monkeypatch):
    older = User(id="u-old", email="old@example.com", created_at=datetime.utcnow() - timedelta(days=5))
    db_session.add(older)
    db_session.commit()

    pending_row = AdminUserOut(
        id="pending-new",
        email="pending-new@example.com",
        name=None,
        phone=None,
        created_at=datetime.utcnow().isoformat(),
        plan=None,
        subscription_status="none",
        stripe_customer_id=None,
        targets_count=0,
        notifications_count=0,
        email_confirmed=False,
    )
    monkeypatch.setattr(main, "_pending_signups", lambda db, known_ids: [pending_row])

    app.dependency_overrides[auth.require_admin] = lambda: auth.AuthUser(id="admin", email="admin@test.com")

    def _get_db_override():
        yield db_session

    from app.db import get_db
    app.dependency_overrides[get_db] = _get_db_override
    try:
        client = TestClient(app)
        resp = client.get("/api/admin/users")
        assert resp.status_code == 200
        data = resp.json()
        emails = [row["email"] for row in data]
        assert emails == ["pending-new@example.com", "old@example.com"]
        assert data[0]["email_confirmed"] is False
        assert data[1]["email_confirmed"] is True
    finally:
        app.dependency_overrides.pop(auth.require_admin, None)
        app.dependency_overrides.pop(get_db, None)

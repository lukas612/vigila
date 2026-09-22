"""Since 2026-09-22, checks_free stores the searched DNI/NIE/matrícula
encrypted (value_encrypted), not just the one-way value_hash — an explicit
account-owner decision to let the admin panel's search history show what
was actually searched. See app/main.py's admin_checks_list and
CheckFree's docstring, and privacidad.html sections 2/7 for the
retention/legal-basis language this implies.

Also covers AdminUserOut.targets_full (main.admin_users), which similarly
now shows a monitored target fully decrypted rather than masked."""
from __future__ import annotations

import os
from datetime import datetime

from cryptography.fernet import Fernet

_KEY = Fernet.generate_key().decode()
os.environ["TARGET_ENCRYPTION_KEY"] = _KEY
os.environ["DATABASE_URL"] = "sqlite:////tmp/test_admin_checks_list.db"
if os.path.exists("/tmp/test_admin_checks_list.db"):
    os.remove("/tmp/test_admin_checks_list.db")

import pytest
from fastapi.testclient import TestClient

from app import auth, crypto, main
from app.db import SessionLocal, get_db, init_db
from app.models import CheckFree, CheckResultEnum, MonitoredId, User


@pytest.fixture()
def db_session():
    init_db()
    session = SessionLocal()
    try:
        yield session
    finally:
        session.query(CheckFree).delete()
        session.query(MonitoredId).delete()
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


def test_checks_list_decrypts_the_stored_value(db_session, client):
    db_session.add(
        CheckFree(
            ip_hash="ip-hash",
            value_hash="value-hash",
            value_encrypted=crypto.encrypt_value("12345678Z"),
            result=CheckResultEnum.found,
            created_at=datetime.utcnow(),
        )
    )
    db_session.commit()

    resp = client.get("/api/admin/checks/list")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["value"] == "12345678Z"


def test_checks_list_shows_none_for_rows_predating_value_encrypted(db_session, client):
    db_session.add(
        CheckFree(
            ip_hash="ip-hash",
            value_hash="value-hash",
            value_encrypted=None,
            result=CheckResultEnum.not_found,
            created_at=datetime.utcnow(),
        )
    )
    db_session.commit()

    resp = client.get("/api/admin/checks/list")
    items = resp.json()["items"]
    assert items[0]["value"] is None
    assert items[0]["value_ref"] == "value-hash"[:10]


def test_admin_users_shows_the_full_monitored_target(db_session, client):
    user = User(id="u-1", email="u1@example.com", created_at=datetime.utcnow())
    db_session.add(user)
    db_session.add(
        MonitoredId(user_id="u-1", value_encrypted=crypto.encrypt_value("1234BCD"), label=None)
    )
    db_session.commit()

    resp = client.get("/api/admin/users")
    assert resp.status_code == 200
    rows = [r for r in resp.json() if r["id"] == "u-1"]
    assert len(rows) == 1
    assert rows[0]["targets_full"] == ["1234BCD"]

"""GET /api/social-proof feeds the landing page's floating social-proof
toasts — real, aggregate-only numbers, never a specific person or a
locality tied to our own (tiny) customer base. See main.social_proof and
SocialProofResponse's docstring."""
from __future__ import annotations

import os
from datetime import datetime, timedelta

from cryptography.fernet import Fernet

os.environ.setdefault("TARGET_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ["DATABASE_URL"] = "sqlite:////tmp/test_social_proof.db"
if os.path.exists("/tmp/test_social_proof.db"):
    os.remove("/tmp/test_social_proof.db")

import pytest
from fastapi.testclient import TestClient

from app import main
from app.db import SessionLocal, get_db, init_db
from app.models import CheckFree, CheckResultEnum, MonitoredId, Notification, User


@pytest.fixture()
def db_session():
    init_db()
    session = SessionLocal()
    try:
        yield session
    finally:
        session.query(Notification).delete()
        session.query(MonitoredId).delete()
        session.query(User).delete()
        session.query(CheckFree).delete()
        session.commit()
        session.close()


@pytest.fixture()
def client(db_session):
    def _get_db_override():
        yield db_session

    main.app.dependency_overrides[get_db] = _get_db_override
    yield TestClient(main.app)
    main.app.dependency_overrides.pop(get_db, None)


def test_social_proof_counts_only_todays_checks_and_this_months_notifications(db_session, client):
    now = datetime.utcnow()
    yesterday = now - timedelta(days=1)

    db_session.add(CheckFree(ip_hash="a", value_hash="a", result=CheckResultEnum.found, created_at=now))
    db_session.add(CheckFree(ip_hash="b", value_hash="b", result=CheckResultEnum.not_found, created_at=now))
    db_session.add(CheckFree(ip_hash="c", value_hash="c", result=CheckResultEnum.found, created_at=yesterday))

    user = User(id="u-1", email="u@example.com", created_at=now)
    db_session.add(user)
    target = MonitoredId(id="t-1", user_id="u-1", value_encrypted="x")
    db_session.add(target)
    db_session.add(Notification(monitored_id="t-1", boe_ref="ref-1", created_at=now))
    last_month = now.replace(day=1) - timedelta(days=1)
    db_session.add(Notification(monitored_id="t-1", boe_ref="ref-2", created_at=last_month))
    db_session.commit()

    resp = client.get("/api/social-proof")
    assert resp.status_code == 200
    data = resp.json()

    assert data["today_checks"] == 2
    assert data["today_found"] == 1
    assert data["notifications_this_month"] == 1

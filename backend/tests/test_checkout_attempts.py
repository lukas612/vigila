"""CheckoutAttempt is the ground truth for "who clicked to subscribe" —
recorded server-side on every POST /api/billing/checkout, independent of
whether Stripe's API call succeeds and independent of the client-side GA4
event (silently dropped by ad blockers / declined cookie consent). See
models.CheckoutAttempt and billing.apply_event's checkout.session.completed
branch for how it's later marked completed."""
from __future__ import annotations

import os
from datetime import datetime

from cryptography.fernet import Fernet

os.environ.setdefault("TARGET_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ["DATABASE_URL"] = "sqlite:////tmp/test_checkout_attempts.db"
if os.path.exists("/tmp/test_checkout_attempts.db"):
    os.remove("/tmp/test_checkout_attempts.db")

import pytest
from fastapi.testclient import TestClient

from app import auth, billing, main
from app.db import SessionLocal, get_db, init_db
from app.models import CheckoutAttempt, Plan, SubscriptionStatus, User


@pytest.fixture()
def db_session():
    init_db()
    session = SessionLocal()
    try:
        yield session
    finally:
        session.query(CheckoutAttempt).delete()
        session.query(User).delete()
        session.commit()
        session.close()


@pytest.fixture()
def client(db_session, monkeypatch):
    monkeypatch.setattr(billing, "create_checkout_session", lambda *a, **k: "https://checkout.stripe.com/fake")

    def _get_db_override():
        yield db_session

    main.app.dependency_overrides[get_db] = _get_db_override
    yield TestClient(main.app)
    main.app.dependency_overrides.pop(auth.get_current_user, None)
    main.app.dependency_overrides.pop(auth.require_admin, None)
    main.app.dependency_overrides.pop(get_db, None)


def _as_user(user_id: str, email: str) -> auth.AuthUser:
    return auth.AuthUser(id=user_id, email=email)


def test_checkout_click_is_recorded_even_before_stripe_is_called(db_session, client):
    main.app.dependency_overrides[auth.get_current_user] = lambda: _as_user("u-1", "click@example.com")

    resp = client.post("/api/billing/checkout", json={"plan": "individual"})

    assert resp.status_code == 200
    attempts = db_session.query(CheckoutAttempt).filter(CheckoutAttempt.user_id == "u-1").all()
    assert len(attempts) == 1
    assert attempts[0].plan == "individual"
    assert attempts[0].completed_subscription is False


def test_stripe_error_still_leaves_the_click_recorded(db_session, client, monkeypatch):
    def _boom(*a, **k):
        raise billing.StripeError("Stripe rechazó la solicitud")

    monkeypatch.setattr(billing, "create_checkout_session", _boom)
    main.app.dependency_overrides[auth.get_current_user] = lambda: _as_user("u-2", "fails@example.com")

    resp = client.post("/api/billing/checkout", json={"plan": "familiar"})

    assert resp.status_code == 502
    attempts = db_session.query(CheckoutAttempt).filter(CheckoutAttempt.user_id == "u-2").all()
    assert len(attempts) == 1
    assert attempts[0].plan == "familiar"


def test_completed_checkout_session_marks_the_most_recent_attempt(db_session):
    user = User(
        id="u-3",
        email="paid@example.com",
        created_at=datetime.utcnow(),
        subscription_status=SubscriptionStatus.none,
    )
    db_session.add(user)
    db_session.add(CheckoutAttempt(user_id="u-3", plan="individual"))
    db_session.commit()

    event = {
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_test_1",
                "customer": "cus_test_1",
                "customer_details": {"name": "Test User"},
                "metadata": {"user_id": "u-3", "plan": "individual"},
            }
        },
    }
    billing.apply_event(db_session, event)

    attempt = db_session.query(CheckoutAttempt).filter(CheckoutAttempt.user_id == "u-3").first()
    assert attempt.completed_subscription is True
    assert db_session.query(User).filter(User.id == "u-3").first().subscription_status == SubscriptionStatus.active


def test_admin_billing_reports_checkout_totals_and_rate(db_session, client):
    db_session.add(User(id="u-4", email="a@example.com", created_at=datetime.utcnow()))
    db_session.add(User(id="u-5", email="b@example.com", created_at=datetime.utcnow()))
    db_session.add(CheckoutAttempt(user_id="u-4", plan="individual", completed_subscription=True))
    db_session.add(CheckoutAttempt(user_id="u-5", plan="individual", completed_subscription=False))
    db_session.commit()

    main.app.dependency_overrides[auth.require_admin] = lambda: _as_user("admin", "admin@example.com")

    resp = client.get("/api/admin/billing")

    assert resp.status_code == 200
    data = resp.json()
    assert data["checkout_attempts_total"] == 2
    assert data["checkout_attempts_completed"] == 1

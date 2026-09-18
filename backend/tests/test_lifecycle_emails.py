"""The welcome/conversion email series (app/lifecycle_emails.py) must never
double-send — LifecycleEmailLog is the only thing standing between a cron
re-run or a retried Stripe webhook and someone getting the same "activate
your plan" email five times. See app/lifecycle_emails.py and
scripts/send_lifecycle_emails.py."""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from unittest.mock import patch

from cryptography.fernet import Fernet

os.environ.setdefault("TARGET_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ["DATABASE_URL"] = "sqlite:////tmp/test_lifecycle_emails.db"
if os.path.exists("/tmp/test_lifecycle_emails.db"):
    os.remove("/tmp/test_lifecycle_emails.db")

import pytest

from app import billing, lifecycle_emails
from app.db import SessionLocal, init_db
from app.models import LifecycleEmailLog, Plan, SubscriptionStatus, User
from scripts import send_lifecycle_emails


@pytest.fixture()
def db_session():
    init_db()
    session = SessionLocal()
    try:
        yield session
    finally:
        session.query(LifecycleEmailLog).delete()
        session.query(User).delete()
        session.commit()
        session.close()


@pytest.fixture()
def sent(monkeypatch):
    log: list[tuple[str, str]] = []

    def fake_send(to, subject, html):
        log.append((to, subject))
        return True

    monkeypatch.setattr(lifecycle_emails.email, "send_email", fake_send)
    return log


def _make_user(db, **kwargs):
    user = User(id=kwargs.pop("id", "u1"), email=kwargs.pop("email", "u1@example.com"), **kwargs)
    db.add(user)
    db.commit()
    return user


def test_welcome_no_plan_0_sends_once_per_user(db_session, sent):
    user = _make_user(db_session)
    assert lifecycle_emails.send_welcome_no_plan_0(db_session, user) is True
    assert lifecycle_emails.send_welcome_no_plan_0(db_session, user) is False
    assert len(sent) == 1


def test_paid_3_cross_sell_only_targets_individual_plan(db_session, sent):
    familiar_user = _make_user(db_session, id="u-familiar", email="familiar@example.com", plan=Plan.familiar)
    individual_user = _make_user(db_session, id="u-individual", email="individual@example.com", plan=Plan.individual)

    assert lifecycle_emails.send_welcome_paid_3(db_session, familiar_user) is False
    assert lifecycle_emails.send_welcome_paid_3(db_session, individual_user) is True
    assert sent == [("individual@example.com", "¿Vigilamos también a tu pareja, tu hijo o tu furgoneta?")]


def test_checkout_completed_sends_welcome_paid_0_only_on_first_activation(db_session, sent):
    user = _make_user(db_session)
    event = {
        "type": "checkout.session.completed",
        "data": {"object": {
            "metadata": {"user_id": user.id, "plan": "individual"},
            "customer": "cus_1",
            "customer_details": {},
        }},
    }
    billing.apply_event(db_session, event)
    assert len(sent) == 1

    # A later renewal for the same customer must not re-trigger the welcome.
    renewal = {
        "type": "customer.subscription.updated",
        "data": {"object": {"customer": "cus_1", "status": "active", "items": {"data": []}}},
    }
    billing.apply_event(db_session, renewal)
    assert len(sent) == 1


def test_fresh_transition_into_past_due_sends_payment_failed_but_retries_dont(db_session, sent):
    user = _make_user(db_session, plan=Plan.individual, subscription_status=SubscriptionStatus.active,
                       subscribed_at=datetime.utcnow(), stripe_customer_id="cus_2")
    db_session.commit()

    past_due_event = {
        "type": "customer.subscription.updated",
        "data": {"object": {"customer": "cus_2", "status": "past_due", "items": {"data": []}}},
    }
    billing.apply_event(db_session, past_due_event)
    assert sent == [(user.email, "Hemos pausado tu vigilancia — pago fallido")]

    # A retried webhook delivery of the same status must not resend it.
    billing.apply_event(db_session, past_due_event)
    assert len(sent) == 1

    # But a real recovery-then-fail cycle should.
    recovered_event = {
        "type": "customer.subscription.updated",
        "data": {"object": {"customer": "cus_2", "status": "active", "items": {"data": []}}},
    }
    billing.apply_event(db_session, recovered_event)
    billing.apply_event(db_session, past_due_event)
    assert len(sent) == 2


def test_cron_only_sends_steps_whose_threshold_has_elapsed(db_session, sent):
    now = datetime.utcnow()
    _make_user(db_session, id="fresh", email="fresh@example.com", created_at=now - timedelta(days=3))
    _make_user(db_session, id="stale", email="stale@example.com", created_at=now - timedelta(days=12))

    send_lifecycle_emails.run()

    by_user: dict[str, list[str]] = {}
    for to, subject in sent:
        by_user.setdefault(to, []).append(subject)

    assert len(by_user["fresh@example.com"]) == 1  # only the day-2 step
    assert len(by_user["stale@example.com"]) == 3  # day 2, 5, and 10 all elapsed


def test_cron_is_idempotent_across_runs(db_session, sent):
    _make_user(db_session, created_at=datetime.utcnow() - timedelta(days=12))
    send_lifecycle_emails.run()
    first_run_count = len(sent)
    assert first_run_count > 0

    send_lifecycle_emails.run()
    assert len(sent) == first_run_count, "a second run on the same day must not resend anything"

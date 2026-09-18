"""A canceled/lapsed subscriber's monitored targets must stop getting
checked by the cron — otherwise it's free monitoring for someone who isn't
paying, and the plan-based limits built into /api/targets don't mean much.
See scripts.check_monitored_targets and main._max_targets."""
from __future__ import annotations

import os

from cryptography.fernet import Fernet

os.environ.setdefault("TARGET_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ["DATABASE_URL"] = "sqlite:////tmp/test_check_monitored_targets.db"
if os.path.exists("/tmp/test_check_monitored_targets.db"):
    os.remove("/tmp/test_check_monitored_targets.db")

import pytest

from app import crypto
from app.db import SessionLocal, init_db
from app.models import MonitoredId, Plan, SubscriptionStatus, User
from scripts import check_monitored_targets


@pytest.fixture()
def db_session():
    init_db()
    session = SessionLocal()
    try:
        yield session
    finally:
        # Each test starts from a clean slate — these tests care about which
        # rows the cron's query selects, so leftover rows from a previous
        # test would silently change the count being asserted on.
        session.query(MonitoredId).delete()
        session.query(User).delete()
        session.commit()
        session.close()


def _make_user(db, user_id, status, plan=Plan.individual):
    user = User(id=user_id, email=f"{user_id}@example.com", subscription_status=status, plan=plan)
    db.add(user)
    db.flush()
    return user


def _make_target(db, user_id, value="12345678A"):
    target = MonitoredId(user_id=user_id, value_encrypted=crypto.encrypt_value(value), active=True)
    db.add(target)
    db.flush()
    return target


def test_run_only_checks_targets_owned_by_an_entitled_subscriber(db_session, monkeypatch):
    active_user = _make_user(db_session, "u-active", SubscriptionStatus.active)
    trialing_user = _make_user(db_session, "u-trialing", SubscriptionStatus.trialing)
    canceled_user = _make_user(db_session, "u-canceled", SubscriptionStatus.canceled)
    past_due_user = _make_user(db_session, "u-past-due", SubscriptionStatus.past_due)
    none_user = _make_user(db_session, "u-none", SubscriptionStatus.none)

    checked_target = _make_target(db_session, active_user.id, "11111111H")
    trialing_target = _make_target(db_session, trialing_user.id, "22222222J")
    canceled_target = _make_target(db_session, canceled_user.id, "33333333P")
    past_due_target = _make_target(db_session, past_due_user.id, "44444444C")
    none_target = _make_target(db_session, none_user.id, "55555555K")
    db_session.commit()

    checked_ids = []
    monkeypatch.setattr(
        check_monitored_targets.monitoring,
        "check_target",
        lambda db, target: checked_ids.append(target.id) or [],
    )

    check_monitored_targets.run()

    assert checked_ids == [checked_target.id, trialing_target.id]
    assert canceled_target.id not in checked_ids
    assert past_due_target.id not in checked_ids
    assert none_target.id not in checked_ids


def test_run_skips_an_inactive_target_even_for_a_paying_owner(db_session, monkeypatch):
    user = _make_user(db_session, "u-active-2", SubscriptionStatus.active)
    target = _make_target(db_session, user.id)
    target.active = False
    db_session.commit()

    checked_ids = []
    monkeypatch.setattr(
        check_monitored_targets.monitoring,
        "check_target",
        lambda db, target: checked_ids.append(target.id) or [],
    )

    check_monitored_targets.run()

    assert checked_ids == []

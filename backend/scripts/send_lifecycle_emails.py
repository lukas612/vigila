"""Sends the day-based steps of the welcome/conversion email series (see
app/lifecycle_emails.py) that can't fire from a single event — "still
hasn't subscribed 2 days after signup" or "has been paying for 30 days" is
only knowable by checking elapsed time, not by reacting to one webhook.
The "day 0" steps of both series fire immediately elsewhere (see
main._ensure_profile and billing.apply_event) and are never touched here.

Meant to run once a day from GitHub Actions (see
.github/workflows/send_lifecycle_emails.yml), same pattern as
check_monitored_targets.py. Safe to run more than once a day, or to miss a
day entirely — every step is idempotent per user (LifecycleEmailLog) and
gated on ">= N days", so a late run still catches up.

Usage:
    python -m scripts.send_lifecycle_emails
"""
from __future__ import annotations

import logging
from datetime import datetime

from app import lifecycle_emails
from app.db import SessionLocal, init_db
from app.models import SubscriptionStatus, User

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("send_lifecycle_emails")

_NO_PLAN_STEPS = (
    (2, lifecycle_emails.send_welcome_no_plan_2),
    (5, lifecycle_emails.send_welcome_no_plan_5),
    (10, lifecycle_emails.send_welcome_no_plan_10),
)
_PAID_STEPS = (
    (2, lifecycle_emails.send_welcome_paid_no_target_2),
    (3, lifecycle_emails.send_welcome_paid_3),
    (30, lifecycle_emails.send_welcome_paid_30),
)


def run() -> None:
    init_db()
    db = SessionLocal()
    now = datetime.utcnow()
    sent = 0
    try:
        no_plan_users = db.query(User).filter(User.subscription_status == SubscriptionStatus.none).all()
        for user in no_plan_users:
            age_days = (now - user.created_at).days
            for threshold, sender in _NO_PLAN_STEPS:
                if age_days >= threshold and sender(db, user):
                    sent += 1

        paying_users = (
            db.query(User)
            .filter(User.subscription_status.in_((SubscriptionStatus.active, SubscriptionStatus.trialing)))
            .filter(User.subscribed_at.isnot(None))
            .all()
        )
        for user in paying_users:
            age_days = (now - user.subscribed_at).days
            for threshold, sender in _PAID_STEPS:
                if age_days >= threshold and sender(db, user):
                    sent += 1

        logger.info("lifecycle emails: %d sent this run", sent)
    finally:
        db.close()


if __name__ == "__main__":
    run()

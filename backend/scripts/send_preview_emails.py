"""One-off dev/ops tool: sends every lifecycle email template for real, via
Resend, to a single inbox — so the actual "From" address, deliverability and
rendering can be checked in a real mail client, not just read as raw HTML.

Deliberately never touches the database: it calls the pure `_content_*`
builders in app.lifecycle_emails directly (not the `send_*` wrappers), so it
can't accidentally write a LifecycleEmailLog row against a user that doesn't
really exist, and needs no DATABASE_URL to run.

Never runs on a schedule — see .github/workflows/send_preview_emails.yml,
which is workflow_dispatch-only.

Usage:
    PREVIEW_TO_EMAIL=you@example.com python -m scripts.send_preview_emails
"""
from __future__ import annotations

import logging
import os

from app import email, lifecycle_emails
from app.models import Plan

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("send_preview_emails")


class _FakeUser:
    """Enough of models.User for the `_content_*` builders to read — never
    goes near the database."""

    def __init__(self, email_: str, plan: Plan | None = None):
        self.email = email_
        self.plan = plan


def run() -> None:
    to = os.environ.get("PREVIEW_TO_EMAIL")
    if not to:
        raise SystemExit("Set PREVIEW_TO_EMAIL to the inbox that should receive the previews")

    no_plan_user = _FakeUser(to)
    paid_individual_user = _FakeUser(to, plan=Plan.individual)

    previews = [
        ("[Preview A1 · día 0] ", lifecycle_emails._content_welcome_no_plan_0(no_plan_user)),
        ("[Preview A2 · día 2] ", lifecycle_emails._content_welcome_no_plan_2(no_plan_user)),
        ("[Preview A3 · día 5] ", lifecycle_emails._content_welcome_no_plan_5(no_plan_user)),
        ("[Preview A4 · día 10] ", lifecycle_emails._content_welcome_no_plan_10(no_plan_user)),
        ("[Preview B1 · día 0] ", lifecycle_emails._content_welcome_paid_0(paid_individual_user)),
        ("[Preview B2 · día 3] ", lifecycle_emails._content_welcome_paid_3()),
        # Sample stats (12 checks, one match found) — the real send queries
        # NotificationRun for the actual numbers, see send_welcome_paid_30.
        ("[Preview B3 · día 30] ", lifecycle_emails._content_welcome_paid_30(12, True)),
        ("[Preview BONUS · pago fallido] ", lifecycle_emails._content_payment_failed()),
    ]

    sent = 0
    for prefix, (subject, inner) in previews:
        html = lifecycle_emails._wrap(inner, to)
        ok = email.send_email(to, prefix + subject, html)
        logger.info("%s -> %s", prefix + subject, "sent" if ok else "FAILED")
        if ok:
            sent += 1

    logger.info("preview run: %d/%d sent to %s", sent, len(previews), to)


if __name__ == "__main__":
    run()

"""Re-runs the same BOE check pipeline used by the free checker against
every active monitored DNI/NIE/matrícula, and records any new matches.

Meant to run on a schedule from GitHub Actions (see
.github/workflows/check_targets.yml), for the same reason as
crawl_daily_stats.py: each check can mean downloading and parsing several
PDFs, and doing that for every monitored target inside a web request isn't
viable on Render's free tier. (A brand-new target does get one immediate
check from main.py itself, right when it's created — see
app.monitoring.check_target — so this cron is what re-checks it going
forward, not what checks it the very first time.)

Pull-based for now: a new match is written to `notifications` and shows up
next time the user opens their dashboard, but nothing emails them yet.
Sending an actual "you were fined" email needs a transactional email
provider (Supabase Auth's mailer only sends its own login emails, not
arbitrary content) — that's a deliberate follow-up, not an oversight.

Usage:
    python -m scripts.check_monitored_targets
"""
from __future__ import annotations

import logging

from app import monitoring
from app.db import SessionLocal, init_db
from app.models import CheckResultEnum, MonitoredId, NotificationRun

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("check_monitored_targets")


def run() -> None:
    init_db()
    db = SessionLocal()
    try:
        targets = db.query(MonitoredId).filter(MonitoredId.active.is_(True)).all()
        logger.info("checking %d active monitored target(s)", len(targets))

        for target in targets:
            try:
                new_hits = monitoring.check_target(db, target)
            except Exception:
                logger.exception("check failed for target %s", target.id)
                db.add(NotificationRun(monitored_id=target.id, result=CheckResultEnum.error))
                db.commit()
                continue

            if new_hits:
                logger.info("target %s: %d new match(es)", target.id, new_hits)

        logger.info("done")
    finally:
        db.close()


if __name__ == "__main__":
    run()

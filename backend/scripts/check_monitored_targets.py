"""Re-runs the same BOE check pipeline used by the free checker against
every active monitored DNI/NIE/matrícula, and records any new matches.

Meant to run on a schedule from GitHub Actions (see
.github/workflows/check_targets.yml), for the same reason as
crawl_daily_stats.py: each check can mean downloading and parsing several
PDFs, and doing that for every monitored target inside a web request isn't
viable on Render's free tier.

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
from datetime import datetime

from app import check_service, crypto
from app.db import SessionLocal, init_db
from app.models import CheckResultEnum, MonitoredId, Notification, NotificationRun

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("check_monitored_targets")


def _to_number(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value.replace(",", "."))
    except ValueError:
        return None


def _record_hits(db, target: MonitoredId, matches) -> int:
    new_hits = 0
    for row in matches:
        exists = (
            db.query(Notification)
            .filter(
                Notification.monitored_id == target.id,
                Notification.boe_ref == row.boe_ref,
                Notification.expediente == row.expediente,
            )
            .first()
        )
        if exists:
            continue
        db.add(
            Notification(
                monitored_id=target.id,
                boe_ref=row.boe_ref,
                expediente=row.expediente,
                matricula=row.matricula,
                localidad=row.localidad,
                importe=_to_number(row.importe),
                fecha=row.fecha,
                precepto=row.precepto,
                articulo=row.articulo,
                plazo_alegacion_fin=row.plazo_alegacion_fin,
            )
        )
        new_hits += 1
    return new_hits


def run() -> None:
    init_db()
    db = SessionLocal()
    try:
        targets = db.query(MonitoredId).filter(MonitoredId.active.is_(True)).all()
        logger.info("checking %d active monitored target(s)", len(targets))

        for target in targets:
            try:
                value = crypto.decrypt_value(target.value_encrypted)
                result = check_service.run_check(value)
            except Exception:
                logger.exception("check failed for target %s", target.id)
                db.add(NotificationRun(monitored_id=target.id, result=CheckResultEnum.error))
                db.commit()
                continue

            new_hits = _record_hits(db, target, result.matches)
            target.last_checked_at = datetime.utcnow()
            db.add(
                NotificationRun(
                    monitored_id=target.id,
                    result=CheckResultEnum.found if result.matches else CheckResultEnum.not_found,
                )
            )
            db.commit()
            if new_hits:
                logger.info("target %s: %d new match(es)", target.id, new_hits)

        logger.info("done")
    finally:
        db.close()


if __name__ == "__main__":
    run()

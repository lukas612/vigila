"""Runs the check pipeline against a single monitored target and records
any new matches — shared by the twice-daily cron
(scripts/check_monitored_targets.py) and the immediate check main.py runs
right after a target is created, so "found" and de-duplication behave
identically regardless of which one triggered the check.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from . import check_service, crypto
from .models import CheckResultEnum, MonitoredId, Notification, NotificationRun


def _to_number(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value.replace(",", "."))
    except ValueError:
        return None


def record_hits(db: Session, target: MonitoredId, matches) -> int:
    """Insert any match not already stored for this target, de-duped on
    (monitored_id, boe_ref, expediente). Returns how many were new."""
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


def check_target(db: Session, target: MonitoredId) -> int:
    """Decrypt, run the pipeline, record hits, stamp last_checked_at and a
    NotificationRun — everything one check of one target needs to do.
    Returns how many new matches were found. Raises on a pipeline failure
    (network/parse error) — the caller decides how to surface that;
    the cron logs a NotificationRun(error) and moves on, the API endpoint
    lets the target still get created even if this first check fails."""
    value = crypto.decrypt_value(target.value_encrypted)
    result = check_service.run_check(value)

    new_hits = record_hits(db, target, result.matches)
    target.last_checked_at = datetime.utcnow()
    db.add(
        NotificationRun(
            monitored_id=target.id,
            result=CheckResultEnum.found if result.matches else CheckResultEnum.not_found,
        )
    )
    db.commit()
    return new_hits

"""Runs the check pipeline against a single monitored target, records any
new matches, and emails the owner about them — shared by the twice-daily
cron (scripts/check_monitored_targets.py) and the immediate check main.py
runs right after a target is created, so "found", de-duplication and the
alert email all behave identically regardless of which one triggered the
check.
"""
from __future__ import annotations

import logging
from datetime import date, datetime

from sqlalchemy.orm import Session

from . import check_service, crypto, email
from .models import CheckResultEnum, MonitoredId, Notification, NotificationRun

logger = logging.getLogger("vigila")


def _to_number(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value.replace(",", "."))
    except ValueError:
        return None


def record_hits(db: Session, target: MonitoredId, matches) -> list[Notification]:
    """Insert any match not already stored for this target, de-duped on
    (monitored_id, boe_ref, expediente). Returns the newly created rows."""
    new_rows: list[Notification] = []
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
        notif = Notification(
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
        db.add(notif)
        new_rows.append(notif)
    return new_rows


def _row_html(row: Notification) -> str:
    plazo = row.plazo_alegacion_fin.strftime("%d/%m/%Y") if row.plazo_alegacion_fin else "—"
    dias = (row.plazo_alegacion_fin - date.today()).days if row.plazo_alegacion_fin else None
    plazo_extra = f" ({dias} días)" if dias is not None else ""
    importe = f"{row.importe:.0f} €" if row.importe is not None else "—"
    return f"""
      <tr>
        <td style="padding:10px 12px;border-bottom:1px solid #E3E9F2;">{row.localidad or '—'}</td>
        <td style="padding:10px 12px;border-bottom:1px solid #E3E9F2;">{importe}</td>
        <td style="padding:10px 12px;border-bottom:1px solid #E3E9F2;color:#D93025;font-weight:600;">{plazo}{plazo_extra}</td>
      </tr>
    """


def _alert_html(target_label: str | None, new_rows: list[Notification]) -> str:
    label = target_label or "tu vigilancia"
    rows_html = "".join(_row_html(r) for r in new_rows)
    n = len(new_rows)
    return f"""
    <div style="font-family:-apple-system,sans-serif;max-width:520px;margin:0 auto;color:#182233;">
      <h2 style="color:#1B4D8C;margin-bottom:4px;">Hemos encontrado algo nuevo</h2>
      <p>En <b>{label}</b> hemos detectado {n} notificación{'es' if n != 1 else ''} nueva{'s' if n != 1 else ''} publicada{'s' if n != 1 else ''} en el BOE:</p>
      <table style="width:100%;border-collapse:collapse;font-size:14px;margin-top:12px;">
        <thead>
          <tr>
            <th style="text-align:left;padding:8px 12px;border-bottom:2px solid #1B4D8C;">Localidad</th>
            <th style="text-align:left;padding:8px 12px;border-bottom:2px solid #1B4D8C;">Importe</th>
            <th style="text-align:left;padding:8px 12px;border-bottom:2px solid #1B4D8C;">Plazo de alegación</th>
          </tr>
        </thead>
        <tbody>{rows_html}</tbody>
      </table>
      <p style="margin-top:24px;">
        <a href="https://vigilamultas.com/cuenta.html"
           style="background:#F5A623;color:#2A1B00;padding:12px 22px;border-radius:8px;text-decoration:none;font-weight:700;">
          Ver detalles en tu cuenta
        </a>
      </p>
      <p style="font-size:12px;color:#8B93A6;margin-top:28px;">
        Recibes esto porque tienes una vigilancia activa en Vigila. Puedes eliminarla cuando quieras desde tu cuenta.
      </p>
    </div>
    """


def _send_alert_email(target: MonitoredId, new_rows: list[Notification]) -> None:
    try:
        owner_email = target.user.email
    except Exception:  # noqa: BLE001 - never let an email problem break the check
        logger.exception("could not resolve owner email for target %s", target.id)
        return
    subject = "Nueva multa encontrada en el BOE" + (f" — {target.label}" if target.label else "")
    email.send_email(owner_email, subject, _alert_html(target.label, new_rows))


def check_target(db: Session, target: MonitoredId) -> list[Notification]:
    """Decrypt, run the pipeline, record hits, stamp last_checked_at and a
    NotificationRun, and email the owner if anything new turned up.
    Returns the newly created rows. Raises on a pipeline failure
    (network/parse error) — the caller decides how to surface that;
    the cron logs a NotificationRun(error) and moves on, the API endpoint
    lets the target still get created even if this first check fails."""
    value = crypto.decrypt_value(target.value_encrypted)
    result = check_service.run_check(value)

    new_rows = record_hits(db, target, result.matches)
    target.last_checked_at = datetime.utcnow()
    db.add(
        NotificationRun(
            monitored_id=target.id,
            result=CheckResultEnum.found if result.matches else CheckResultEnum.not_found,
        )
    )
    db.commit()

    if new_rows:
        _send_alert_email(target, new_rows)

    return new_rows

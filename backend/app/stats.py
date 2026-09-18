"""Aggregation logic for the public stats endpoints, shared by two callers:

- The daily crawl (scripts/crawl_daily_stats.py) uses this to precompute
  the `stats_summary` cache rows right after it finishes writing a day's
  `daily_stats` rows.
- main.py uses the exact same functions as a live fallback for any request
  the cache doesn't cover (a non-default `days`, or a specific `date`),
  so the cached and live paths can never disagree on what a number means.

Returns plain dicts (not the Pydantic response models) since the cached
path stores them as JSON text and replays it verbatim — building a
Pydantic model just to immediately re-serialize it would be pointless.
"""
from __future__ import annotations

from datetime import date as date_type

from sqlalchemy import func
from sqlalchemy.orm import Session

from .models import DailyStat


def compute_daily(db: Session, target_date: date_type) -> dict | None:
    rows = (
        db.query(DailyStat)
        .filter(DailyStat.stat_date == target_date)
        .order_by(DailyStat.expedientes_count.desc())
        .all()
    )
    if not rows:
        return None
    return {
        "stat_date": target_date.isoformat(),
        "total_expedientes": sum(r.expedientes_count for r in rows),
        "total_importe": sum(float(r.importe_total or 0) for r in rows),
        "total_con_dni": sum(r.con_dni_count for r in rows),
        "total_con_matricula": sum(r.con_matricula_count for r in rows),
        "localidades": [
            {
                "localidad": r.localidad,
                "expedientes_count": r.expedientes_count,
                "importe_total": float(r.importe_total) if r.importe_total is not None else None,
            }
            for r in rows
        ],
    }


def compute_weekly(db: Session, days: int = 7) -> dict | None:
    window_dates = [
        r[0] for r in db.query(DailyStat.stat_date).distinct().order_by(DailyStat.stat_date.desc()).limit(days).all()
    ]
    if not window_dates:
        return None

    rows = (
        db.query(
            DailyStat.localidad,
            func.sum(DailyStat.expedientes_count).label("count"),
            func.sum(DailyStat.importe_total).label("importe"),
        )
        .filter(DailyStat.stat_date.in_(window_dates))
        .group_by(DailyStat.localidad)
        .order_by(func.sum(DailyStat.expedientes_count).desc())
        .all()
    )
    totals = (
        db.query(
            func.sum(DailyStat.con_dni_count).label("dni"),
            func.sum(DailyStat.con_matricula_count).label("matricula"),
        )
        .filter(DailyStat.stat_date.in_(window_dates))
        .one()
    )

    return {
        "date_from": min(window_dates).isoformat(),
        "date_to": max(window_dates).isoformat(),
        "days_included": [d.isoformat() for d in sorted(window_dates, reverse=True)],
        "total_expedientes": sum(r.count for r in rows),
        "total_importe": sum(float(r.importe or 0) for r in rows),
        "total_con_dni": int(totals.dni or 0),
        "total_con_matricula": int(totals.matricula or 0),
        "localidades": [
            {
                "localidad": r.localidad,
                "expedientes_count": r.count,
                "importe_total": float(r.importe) if r.importe is not None else None,
            }
            for r in rows
        ],
    }

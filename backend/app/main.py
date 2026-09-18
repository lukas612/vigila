from __future__ import annotations

import asyncio
import logging
from datetime import date

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import check_service
from .db import get_db, init_db
from .models import CheckFree, CheckResultEnum, DailyStat, WaitlistSignup
from .rate_limit import RateLimiter, hash_identifier
from .schemas import (
    CheckRequest,
    CheckResponse,
    DailyStatsResponse,
    LocalityStat,
    NotificationOut,
    WaitlistRequest,
    WeeklyStatsResponse,
)
from .validators import InvalidIdentifier, validate_identifier

logger = logging.getLogger("vigila")

app = FastAPI(title="Vigila API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://lukas612.github.io",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

free_check_limiter = RateLimiter(max_requests=5, window_seconds=3600)


@app.on_event("startup")
def _startup() -> None:
    init_db()


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@app.post("/api/check", response_model=CheckResponse)
async def check(payload: CheckRequest, request: Request, db: Session = Depends(get_db)) -> CheckResponse:
    if not payload.consent:
        raise HTTPException(status_code=400, detail="Consentimiento requerido")

    try:
        value = validate_identifier(payload.value)
    except InvalidIdentifier as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    ip_key = hash_identifier(_client_ip(request))
    if not free_check_limiter.is_allowed(ip_key):
        raise HTTPException(
            status_code=429,
            detail="Has alcanzado el límite de comprobaciones gratuitas. Inténtalo más tarde.",
        )
    free_check_limiter.hit(ip_key)

    try:
        result = await asyncio.to_thread(check_service.run_check, value)
    except Exception:  # noqa: BLE001 - never leak internals to the client
        logger.exception("check pipeline failed")
        raise HTTPException(status_code=502, detail="No se pudo consultar el BOE, inténtalo de nuevo") from None

    db.add(
        CheckFree(
            ip_hash=ip_key,
            value_hash=hash_identifier(value),
            result=CheckResultEnum.found if result.found else CheckResultEnum.not_found,
        )
    )
    db.commit()

    today = date.today()
    notifications = [
        NotificationOut(
            boe_ref=row.boe_ref,
            expediente=row.expediente,
            localidad=row.localidad,
            fecha=row.fecha,
            matricula=row.matricula,
            importe=row.importe,
            precepto=row.precepto,
            articulo=row.articulo,
            puntos=row.puntos,
            fecha_publicacion=row.published_on.strftime("%d/%m/%Y") if row.published_on else None,
            plazo_alegacion_fin=row.plazo_alegacion_fin.strftime("%d/%m/%Y") if row.plazo_alegacion_fin else None,
            dias_restantes=(row.plazo_alegacion_fin - today).days if row.plazo_alegacion_fin else None,
        )
        for row in result.matches
    ]
    return CheckResponse(found=result.found, notifications=notifications)


@app.get("/api/stats/dates")
def stats_dates(db: Session = Depends(get_db)) -> list[str]:
    """Every day a background crawl has stored data for, most recent first —
    powers the day picker on the Estadísticas page."""
    rows = db.query(DailyStat.stat_date).distinct().order_by(DailyStat.stat_date.desc()).all()
    return [r[0].isoformat() for r in rows]


@app.get("/api/stats/latest", response_model=DailyStatsResponse)
def stats_latest(
    stat_date: str | None = Query(None, alias="date", description="AAAA-MM-DD; por defecto el día más reciente"),
    db: Session = Depends(get_db),
) -> DailyStatsResponse:
    """Aggregate-only: counts and totals per locality for a given day (or the
    most recent one a background crawl has stored — see
    scripts/crawl_daily_stats.py). Never exposes anything at the level of an
    individual expediente or DNI."""
    if stat_date:
        try:
            target_date = date.fromisoformat(stat_date)
        except ValueError:
            raise HTTPException(status_code=422, detail="Fecha inválida, usa AAAA-MM-DD") from None
    else:
        target_date = db.query(DailyStat.stat_date).order_by(DailyStat.stat_date.desc()).limit(1).scalar()
        if target_date is None:
            raise HTTPException(status_code=404, detail="Todavía no hay datos agregados disponibles")

    rows = (
        db.query(DailyStat)
        .filter(DailyStat.stat_date == target_date)
        .order_by(DailyStat.expedientes_count.desc())
        .all()
    )
    if not rows:
        raise HTTPException(status_code=404, detail="No hay datos agregados para esa fecha")

    return DailyStatsResponse(
        stat_date=target_date.isoformat(),
        total_expedientes=sum(r.expedientes_count for r in rows),
        total_importe=sum(float(r.importe_total or 0) for r in rows),
        localidades=[
            LocalityStat(localidad=r.localidad, expedientes_count=r.expedientes_count, importe_total=r.importe_total)
            for r in rows
        ],
    )


@app.get("/api/stats/weekly", response_model=WeeklyStatsResponse)
def stats_weekly(
    days: int = Query(7, ge=1, le=31, description="Tamaño de la ventana en días naturales con crawl"),
    db: Session = Depends(get_db),
) -> WeeklyStatsResponse:
    """Aggregate-only totals per locality over the most recent `days` days that
    have a stored crawl. Smooths out day-to-day noise (weekends with zero
    bulletins, an occasional backlog dump) for display — the underlying daily
    rows in `daily_stats` are untouched, so single-day drilldown (see
    /api/stats/latest) and future re-slicing stay possible."""
    window_dates = [
        r[0]
        for r in db.query(DailyStat.stat_date).distinct().order_by(DailyStat.stat_date.desc()).limit(days).all()
    ]
    if not window_dates:
        raise HTTPException(status_code=404, detail="Todavía no hay datos agregados disponibles")

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

    return WeeklyStatsResponse(
        date_from=min(window_dates).isoformat(),
        date_to=max(window_dates).isoformat(),
        days_included=[d.isoformat() for d in sorted(window_dates, reverse=True)],
        total_expedientes=sum(r.count for r in rows),
        total_importe=sum(float(r.importe or 0) for r in rows),
        localidades=[
            LocalityStat(localidad=r.localidad, expedientes_count=r.count, importe_total=r.importe) for r in rows
        ],
    )


@app.post("/api/waitlist")
def waitlist(payload: WaitlistRequest, db: Session = Depends(get_db)) -> dict[str, bool]:
    if "@" not in payload.email:
        raise HTTPException(status_code=422, detail="Email inválido")
    db.add(WaitlistSignup(email=payload.email.strip().lower(), context=payload.context))
    db.commit()
    return {"ok": True}


# Serve the static landing page for convenience when running the whole stack
# locally (`uvicorn app.main:app`). In production the frontend is typically
# hosted separately (e.g. a CDN) and only calls the API via CORS.
app.mount("/", StaticFiles(directory="../frontend", html=True), name="frontend")

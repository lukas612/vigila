from __future__ import annotations

import asyncio
import logging

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from . import check_service
from .db import get_db, init_db
from .models import CheckFree, CheckResultEnum, WaitlistSignup
from .rate_limit import RateLimiter, hash_identifier
from .schemas import CheckRequest, CheckResponse, NotificationOut, WaitlistRequest
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
        )
        for row in result.matches
    ]
    return CheckResponse(found=result.found, notifications=notifications)


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

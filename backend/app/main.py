from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from datetime import date, datetime, timedelta

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session

from . import auth, billing, check_service, crypto, lifecycle_emails, monitoring, stats
from .db import get_db, init_db
from .models import (
    CheckFree,
    CheckResultEnum,
    DailyStat,
    MonitoredId,
    Notification,
    Plan,
    StatsSummary,
    SubscriptionStatus,
    User,
    WaitlistSignup,
)
from .rate_limit import RateLimiter, hash_identifier
from scripts import crawl_daily_stats
from .schemas import (
    AdminBackfillStatus,
    AdminBillingDayCount,
    AdminBillingResponse,
    AdminCheckOut,
    AdminChecksPage,
    AdminChecksResponse,
    AdminDayCount,
    AdminUserOut,
    CheckoutRequest,
    CheckoutResponse,
    CheckRequest,
    CheckResponse,
    CreateTargetRequest,
    DailyStatsResponse,
    MeResponse,
    NotificationHitOut,
    NotificationOut,
    PortalResponse,
    SessionRequest,
    TargetOut,
    WaitlistRequest,
    WeeklyStatsResponse,
)
from .validators import InvalidIdentifier, validate_identifier

logger = logging.getLogger("vigila")

app = FastAPI(title="VigilaMultas API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://vigilamultas.com",
        "https://www.vigilamultas.com",
        # Old GitHub Pages URL — kept working during the move to the
        # custom domain rather than cut off immediately.
        "https://lukas612.github.io",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ],
    allow_credentials=True,  # required for the sb_access_token/sb_refresh_token session cookies
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

free_check_limiter = RateLimiter(max_requests=5, window_seconds=3600)


def _max_targets(profile: User | None) -> int:
    """0 with no profile, no plan, or a subscription that isn't
    active/trialing (past_due/canceled/none all mean "can't add more") —
    otherwise the plan's own limit, see billing.PLAN_TARGET_LIMITS."""
    if profile is None or profile.plan is None:
        return 0
    if profile.subscription_status not in (SubscriptionStatus.active, SubscriptionStatus.trialing):
        return 0
    return billing.PLAN_TARGET_LIMITS.get(profile.plan, 0)


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
            value_encrypted=crypto.encrypt_value(value),
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


def _cached_summary(db: Session, key: str) -> dict | None:
    row = db.query(StatsSummary).filter(StatsSummary.key == key).first()
    return json.loads(row.payload) if row else None


@app.get("/api/stats/latest", response_model=DailyStatsResponse)
def stats_latest(
    stat_date: str | None = Query(None, alias="date", description="AAAA-MM-DD; por defecto el día más reciente"),
    db: Session = Depends(get_db),
) -> DailyStatsResponse:
    """Aggregate-only: counts and totals per locality for a given day (or the
    most recent one a background crawl has stored — see
    scripts/crawl_daily_stats.py). Never exposes anything at the level of an
    individual expediente or DNI.

    With no `date` (the common case — the landing page's hook always wants
    "the latest day"), this serves a precomputed row the crawl already
    wrote to `stats_summary` instead of re-aggregating `daily_stats` on
    every visit. A specific `date` always computes live — that path is
    rare enough it doesn't need caching."""
    if not stat_date:
        cached = _cached_summary(db, "daily_latest")
        if cached:
            return DailyStatsResponse(**cached)

    if stat_date:
        try:
            target_date = date.fromisoformat(stat_date)
        except ValueError:
            raise HTTPException(status_code=422, detail="Fecha inválida, usa AAAA-MM-DD") from None
    else:
        target_date = db.query(DailyStat.stat_date).order_by(DailyStat.stat_date.desc()).limit(1).scalar()
        if target_date is None:
            raise HTTPException(status_code=404, detail="Todavía no hay datos agregados disponibles")

    payload = stats.compute_daily(db, target_date)
    if payload is None:
        raise HTTPException(status_code=404, detail="No hay datos agregados para esa fecha")
    return DailyStatsResponse(**payload)


@app.get("/api/stats/weekly", response_model=WeeklyStatsResponse)
def stats_weekly(
    days: int = Query(7, ge=1, le=31, description="Tamaño de la ventana en días naturales con crawl"),
    db: Session = Depends(get_db),
) -> WeeklyStatsResponse:
    """Aggregate-only totals per locality over the most recent `days` days that
    have a stored crawl. Smooths out day-to-day noise (weekends with zero
    bulletins, an occasional backlog dump) for display — the underlying daily
    rows in `daily_stats` are untouched, so single-day drilldown (see
    /api/stats/latest) and future re-slicing stay possible.

    The default (`days=7`, what both the landing page and Estadísticas
    call) is served from the `stats_summary` cache the crawl precomputes;
    any other window size falls back to a live query."""
    if days == 7:
        cached = _cached_summary(db, "weekly")
        if cached:
            return WeeklyStatsResponse(**cached)

    payload = stats.compute_weekly(db, days)
    if payload is None:
        raise HTTPException(status_code=404, detail="Todavía no hay datos agregados disponibles")
    return WeeklyStatsResponse(**payload)


def _ensure_profile(db: Session, user: auth.AuthUser) -> bool:
    """Create the billing-fields profile row on first login. Identity
    itself already exists in Supabase's auth.users — this just gives it
    somewhere to hang plan/subscription_status/stripe_customer_id.
    Returns True the one time this actually created the row (i.e. a real
    first-time signup) — create_session uses that to fire the "Registro"
    ad conversion only once per person, not on every later login."""
    profile = db.query(User).filter(User.id == user.id).first()
    if profile:
        return False
    profile = User(id=user.id, email=user.email)
    db.add(profile)
    db.commit()
    lifecycle_emails.send_welcome_no_plan_0(db, profile)
    return True


@app.post("/api/auth/session", response_model=MeResponse)
def create_session(payload: SessionRequest, response: Response, db: Session = Depends(get_db)) -> MeResponse:
    """Called once by the frontend right after Supabase's JS client
    completes a magic-link login. Verifies the tokens it hands us are
    real (rather than trusting the client), then wraps them in the
    HttpOnly cookies every other /api/* call reads."""
    try:
        user = auth.fetch_user(payload.access_token)
    except auth.InvalidSession:
        raise HTTPException(status_code=401, detail="Token inválido") from None

    auth.set_session_cookies(response, payload.access_token, payload.refresh_token, payload.expires_in)
    is_new_user = _ensure_profile(db, user)
    profile = db.query(User).filter(User.id == user.id).first()
    return MeResponse(
        email=user.email,
        plan=profile.plan.value if profile.plan else None,
        subscription_status=profile.subscription_status.value,
        max_targets=_max_targets(profile),
        is_new_user=is_new_user,
    )


@app.post("/api/auth/logout")
def logout(response: Response) -> dict[str, bool]:
    auth.clear_session_cookies(response)
    return {"ok": True}


@app.get("/api/auth/me", response_model=MeResponse)
def me(user: auth.AuthUser = Depends(auth.get_current_user), db: Session = Depends(get_db)) -> MeResponse:
    profile = db.query(User).filter(User.id == user.id).first()
    return MeResponse(
        email=user.email,
        plan=profile.plan.value if profile and profile.plan else None,
        subscription_status=profile.subscription_status.value if profile else SubscriptionStatus.none.value,
        max_targets=_max_targets(profile),
    )


def _mask(value: str) -> str:
    if len(value) <= 3:
        return value
    return "•" * (len(value) - 3) + value[-3:]


def _target_out(target: MonitoredId, db: Session, monitoring_paused: bool = False) -> TargetOut:
    today = date.today()
    hits = (
        db.query(Notification)
        .filter(Notification.monitored_id == target.id)
        .order_by(Notification.created_at.desc())
        .all()
    )
    return TargetOut(
        id=target.id,
        label=target.label,
        value_masked=_mask(crypto.decrypt_value(target.value_encrypted)),
        active=target.active,
        monitoring_paused=monitoring_paused,
        created_at=target.created_at.isoformat(),
        last_checked_at=target.last_checked_at.isoformat() if target.last_checked_at else None,
        notifications=[
            NotificationHitOut(
                boe_ref=h.boe_ref,
                expediente=h.expediente,
                matricula=h.matricula,
                localidad=h.localidad,
                importe=float(h.importe) if h.importe is not None else None,
                fecha=h.fecha,
                precepto=h.precepto,
                articulo=h.articulo,
                plazo_alegacion_fin=h.plazo_alegacion_fin.strftime("%d/%m/%Y") if h.plazo_alegacion_fin else None,
                dias_restantes=(h.plazo_alegacion_fin - today).days if h.plazo_alegacion_fin else None,
            )
            for h in hits
        ],
    )


@app.get("/api/targets", response_model=list[TargetOut])
def list_targets(
    user: auth.AuthUser = Depends(auth.get_current_user), db: Session = Depends(get_db)
) -> list[TargetOut]:
    rows = (
        db.query(MonitoredId).filter(MonitoredId.user_id == user.id).order_by(MonitoredId.created_at.desc()).all()
    )
    profile = db.query(User).filter(User.id == user.id).first()
    # A canceled/lapsed subscription doesn't delete existing targets, but
    # the cron (scripts/check_monitored_targets.py) skips them the same
    # way — surface that here instead of letting last_checked_at silently
    # go stale with no explanation.
    paused = _max_targets(profile) == 0
    return [_target_out(row, db, monitoring_paused=paused) for row in rows]


@app.post("/api/targets", response_model=TargetOut, status_code=201)
async def create_target(
    payload: CreateTargetRequest,
    user: auth.AuthUser = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
) -> TargetOut:
    if not payload.consent:
        raise HTTPException(status_code=400, detail="Consentimiento requerido")
    try:
        value = validate_identifier(payload.value)
    except InvalidIdentifier as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    _ensure_profile(db, user)
    profile = db.query(User).filter(User.id == user.id).first()

    count = db.query(MonitoredId).filter(MonitoredId.user_id == user.id).count()
    limit = _max_targets(profile)
    if count >= limit:
        detail = (
            "Necesitas una suscripción activa para añadir vigilancias — hazte con un plan desde tu cuenta"
            if limit == 0
            else "Has alcanzado el límite de vigilancias de tu plan"
        )
        raise HTTPException(status_code=400, detail=detail)

    target = MonitoredId(user_id=user.id, value_encrypted=crypto.encrypt_value(value), label=payload.label)
    db.add(target)
    db.commit()
    db.refresh(target)

    # Check it right away instead of leaving it for the next twice-daily
    # cron run — the whole point of adding a target is finding out now, not
    # in up to 12 hours. A failure here (BOE unreachable, etc.) shouldn't
    # stop the target from having been created; it'll just get picked up
    # by the next cron pass like normal.
    try:
        await asyncio.to_thread(monitoring.check_target, db, target)
    except Exception:  # noqa: BLE001 - target creation must still succeed
        logger.exception("initial check failed for new target %s", target.id)

    return _target_out(target, db)


@app.delete("/api/targets/{target_id}", status_code=204)
def delete_target(
    target_id: str, user: auth.AuthUser = Depends(auth.get_current_user), db: Session = Depends(get_db)
) -> Response:
    target = db.query(MonitoredId).filter(MonitoredId.id == target_id, MonitoredId.user_id == user.id).first()
    if not target:
        raise HTTPException(status_code=404, detail="No encontrado")
    db.delete(target)
    db.commit()
    return Response(status_code=204)


@app.post("/api/billing/checkout", response_model=CheckoutResponse)
def billing_checkout(
    payload: CheckoutRequest,
    user: auth.AuthUser = Depends(auth.get_current_user),
    db: Session = Depends(get_db),
) -> CheckoutResponse:
    try:
        plan = Plan(payload.plan)
    except ValueError:
        raise HTTPException(status_code=422, detail="Plan desconocido") from None

    _ensure_profile(db, user)
    profile = db.query(User).filter(User.id == user.id).first()
    try:
        url = billing.create_checkout_session(
            profile,
            plan,
            success_url="https://vigilamultas.com/cuenta.html?billing=success",
            cancel_url="https://vigilamultas.com/cuenta.html?billing=cancel",
        )
    except billing.StripeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return CheckoutResponse(url=url)


@app.post("/api/billing/portal", response_model=PortalResponse)
def billing_portal(
    user: auth.AuthUser = Depends(auth.get_current_user), db: Session = Depends(get_db)
) -> PortalResponse:
    profile = db.query(User).filter(User.id == user.id).first()
    if not profile or not profile.stripe_customer_id:
        raise HTTPException(status_code=400, detail="Todavía no tienes ninguna suscripción")
    try:
        url = billing.create_portal_session(profile.stripe_customer_id, "https://vigilamultas.com/cuenta.html")
    except billing.StripeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return PortalResponse(url=url)


@app.post("/api/billing/webhook")
async def billing_webhook(request: Request, db: Session = Depends(get_db)) -> dict[str, bool]:
    """Stripe calls this directly (no browser involved, so no CORS/cookie
    concerns) whenever a subscribed event happens. The raw body has to be
    read before any parsing — the signature is computed over the exact
    bytes Stripe sent, not a re-serialized version of them."""
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    try:
        event = billing.verify_webhook_signature(payload, sig_header)
    except billing.StripeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    billing.apply_event(db, event)
    return {"received": True}


def _pending_signups(db: Session, known_ids: set[str]) -> list[AdminUserOut]:
    """Someone who requested a magic link but never clicked it never gets a
    public.users row (see main._ensure_profile — it only ever runs from
    /api/auth/session, right after a real login), so without this they're
    invisible in the admin panel even though they're a real signup attempt.
    auth.users is Supabase Auth's own identity table, in the same Postgres
    database under a different schema — reachable because DATABASE_URL
    connects as the `postgres` role, which owns every schema, not just
    public. Best-effort: local dev runs on SQLite, which has no `auth`
    schema at all, so any failure here just means an empty list, never a
    broken admin panel."""
    try:
        rows = db.execute(text("SELECT id, email, created_at FROM auth.users ORDER BY created_at DESC")).all()
    except Exception:
        logger.warning("could not read auth.users for pending signups (expected on non-Postgres DBs)")
        return []
    return [
        AdminUserOut(
            id=str(row.id),
            email=row.email,
            name=None,
            phone=None,
            created_at=row.created_at.isoformat(),
            plan=None,
            subscription_status=SubscriptionStatus.none.value,
            stripe_customer_id=None,
            targets_count=0,
            notifications_count=0,
            email_confirmed=False,
        )
        for row in rows
        if str(row.id) not in known_ids
    ]


@app.get("/api/admin/users", response_model=list[AdminUserOut])
def admin_users(
    _admin: auth.AuthUser = Depends(auth.require_admin), db: Session = Depends(get_db)
) -> list[AdminUserOut]:
    """Every account, plan/billing state, and how many targets/matches they
    have — the profile row (see models.User) plus counts, and each
    monitored value fully decrypted (admin-only, explicitly requested —
    see AdminUserOut.targets_full). Also includes pending signups that
    never finished logging in — see _pending_signups."""
    users = db.query(User).order_by(User.created_at.desc()).all()
    out = []
    for u in users:
        targets = db.query(MonitoredId).filter(MonitoredId.user_id == u.id).all()
        notifications_count = (
            db.query(Notification)
            .join(MonitoredId, Notification.monitored_id == MonitoredId.id)
            .filter(MonitoredId.user_id == u.id)
            .count()
        )
        targets_full = []
        for t in targets:
            try:
                targets_full.append(crypto.decrypt_value(t.value_encrypted))
            except Exception:
                logger.exception("failed to decrypt target %s for admin listing", t.id)
                targets_full.append("(error)")
        out.append(
            AdminUserOut(
                id=u.id,
                email=u.email,
                name=u.name,
                phone=u.phone,
                created_at=u.created_at.isoformat(),
                plan=u.plan.value if u.plan else None,
                subscription_status=u.subscription_status.value,
                stripe_customer_id=u.stripe_customer_id,
                targets_count=len(targets),
                notifications_count=notifications_count,
                targets_full=targets_full,
                email_confirmed=True,
            )
        )
    out.extend(_pending_signups(db, {u.id for u in users}))
    out.sort(key=lambda u: u.created_at, reverse=True)
    return out


@app.get("/api/admin/checks", response_model=AdminChecksResponse)
def admin_checks(
    _admin: auth.AuthUser = Depends(auth.require_admin), db: Session = Depends(get_db)
) -> AdminChecksResponse:
    """Aggregate view of the anonymous free-check funnel: how many people
    have used it, and how many of those checks actually found a fine.
    checks_free never stores the DNI/matrícula itself (only a hash — see
    CheckFree's docstring), so this can only ever show counts, never who
    checked what."""
    rows = db.query(CheckFree.result, CheckFree.created_at).order_by(CheckFree.created_at.desc()).all()

    total = len(rows)
    found = sum(1 for r, _ in rows if r == CheckResultEnum.found)
    not_found = sum(1 for r, _ in rows if r == CheckResultEnum.not_found)
    error = sum(1 for r, _ in rows if r == CheckResultEnum.error)

    by_day: dict[str, dict[str, int]] = {}
    for result, created_at in rows:
        day_key = created_at.date().isoformat()
        bucket = by_day.setdefault(day_key, {"total": 0, "found": 0})
        bucket["total"] += 1
        if result == CheckResultEnum.found:
            bucket["found"] += 1

    return AdminChecksResponse(
        total=total,
        found=found,
        not_found=not_found,
        error=error,
        by_day=[
            AdminDayCount(day=day, total=v["total"], found=v["found"])
            for day, v in sorted(by_day.items(), reverse=True)
        ],
    )


CHECKS_PAGE_SIZE = 20


@app.get("/api/admin/checks/list", response_model=AdminChecksPage)
def admin_checks_list(
    page: int = Query(1, ge=1),
    _admin: auth.AuthUser = Depends(auth.require_admin),
    db: Session = Depends(get_db),
) -> AdminChecksPage:
    """Paginated raw log of the anonymous free-check funnel, newest first.
    Each row's actual DNI/NIE/matrícula is decrypted from value_encrypted
    (see CheckFree's docstring for when/why that started being stored) —
    None for older rows that predate it, which only have the one-way
    value_hash (ip_ref stays hash-only either way, never the raw IP)."""
    total = db.query(CheckFree).count()
    pages = max(1, (total + CHECKS_PAGE_SIZE - 1) // CHECKS_PAGE_SIZE)
    rows = (
        db.query(CheckFree)
        .order_by(CheckFree.created_at.desc())
        .offset((page - 1) * CHECKS_PAGE_SIZE)
        .limit(CHECKS_PAGE_SIZE)
        .all()
    )

    def _value(r: CheckFree) -> str | None:
        if not r.value_encrypted:
            return None
        try:
            return crypto.decrypt_value(r.value_encrypted)
        except Exception:
            logger.exception("failed to decrypt checks_free row %s", r.id)
            return None

    return AdminChecksPage(
        items=[
            AdminCheckOut(
                id=r.id,
                created_at=r.created_at.isoformat(),
                result=r.result.value,
                value=_value(r),
                value_ref=r.value_hash[:10],
                ip_ref=r.ip_hash[:8],
            )
            for r in rows
        ],
        total=total,
        page=page,
        page_size=CHECKS_PAGE_SIZE,
        pages=pages,
    )


# The scheduled cron (daily_stats.yml) is known to run hours late on a
# low-traffic GitHub repo (Actions deprioritizes scheduled runs there) —
# this is the admin panel's manual fallback.
#
# This USED to crawl straight from the web process (~150-200 PDFs/day,
# fetched with 10 concurrent workers, x up to 7 days in one click). That
# crawl_daily_stats module even has its own docstring warning this is "far
# too slow/CPU-heavy to do inside a web request, especially on Render's
# free tier" — and it turned out to be right: it OOM'd the vigila-api web
# service and triggered a Render auto-restart, taking the whole API down
# for everyone mid-restart. So this now dispatches the *same* GitHub
# Actions workflow the cron uses (workflow_dispatch, one run per missing
# day) instead of doing the heavy work on the web dyno at all. Requires
# GITHUB_ACTIONS_TOKEN (a PAT with Actions: write on this repo) to be set.
#
# In-memory state — fine for a single web worker (Render's free/starter
# plan runs one), but a second replica wouldn't see another's progress.
_BACKFILL_MAX_DAYS = 7
_backfill_lock = threading.Lock()
_backfill_state: dict = {"running": False, "missing_days": [], "done_days": [], "started_at": None}

GITHUB_ACTIONS_TOKEN = os.environ.get("GITHUB_ACTIONS_TOKEN", "")
GITHUB_REPO = "lukas612/vigila"
GITHUB_WORKFLOW_FILE = "daily_stats.yml"


def _missing_stat_days(db: Session) -> list[date]:
    """Every day between the most recent daily_stats row and yesterday
    (Europe/Madrid) with no row yet, capped to the most recent
    _BACKFILL_MAX_DAYS. A day with no BOE bulletins (weekend/holiday)
    never gets a row either, so this can list days that come back empty —
    crawl_daily_stats.store already treats that as expected, not a
    failure (see its "no bulletins" branch)."""
    most_recent = db.query(DailyStat.stat_date).order_by(DailyStat.stat_date.desc()).limit(1).scalar()
    yesterday = crawl_daily_stats._yesterday_madrid()
    start = (most_recent + timedelta(days=1)) if most_recent else (yesterday - timedelta(days=_BACKFILL_MAX_DAYS - 1))
    if start > yesterday:
        return []
    days = []
    d = start
    while d <= yesterday:
        days.append(d)
        d += timedelta(days=1)
    return days[-_BACKFILL_MAX_DAYS:]


def _dispatch_backfill_workflow(day: date) -> None:
    """Fires workflow_dispatch on daily_stats.yml for a single day. This is
    a cheap HTTP POST — GitHub Actions does the actual PDF crawling on its
    own runner, not this web dyno."""
    resp = httpx.post(
        f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/{GITHUB_WORKFLOW_FILE}/dispatches",
        headers={
            "Authorization": f"Bearer {GITHUB_ACTIONS_TOKEN}",
            "Accept": "application/vnd.github+json",
        },
        json={"ref": "main", "inputs": {"date": day.isoformat()}},
        timeout=10,
    )
    resp.raise_for_status()


def _run_backfill(days: list[date]) -> None:
    for day in days:
        try:
            _dispatch_backfill_workflow(day)
        except Exception:
            logger.exception("failed to dispatch backfill workflow for %s", day)
        finally:
            # "done" here means dispatched, not crawled — the actual crawl
            # runs asynchronously on GitHub's runner, a few minutes behind.
            _backfill_state["done_days"].append(day.isoformat())
    _backfill_state["running"] = False


@app.post("/api/admin/backfill-stats", response_model=AdminBackfillStatus)
def admin_backfill_stats(
    background_tasks: BackgroundTasks,
    _admin: auth.AuthUser = Depends(auth.require_admin),
    db: Session = Depends(get_db),
) -> AdminBackfillStatus:
    if not GITHUB_ACTIONS_TOKEN:
        raise HTTPException(
            500,
            detail="Falta configurar GITHUB_ACTIONS_TOKEN en el backend para poder lanzar el backfill.",
        )

    with _backfill_lock:
        if _backfill_state["running"]:
            return AdminBackfillStatus(**_backfill_state, message="Ya hay un backfill en curso.")

        missing = _missing_stat_days(db)
        if not missing:
            return AdminBackfillStatus(running=False, message="No faltan días por procesar.")

        _backfill_state.update(
            running=True,
            missing_days=[d.isoformat() for d in missing],
            done_days=[],
            started_at=datetime.utcnow().isoformat(),
        )
        background_tasks.add_task(_run_backfill, missing)
        return AdminBackfillStatus(
            **_backfill_state,
            message=f"Lanzados {len(missing)} día(s) en GitHub Actions — tardarán unos minutos en aparecer.",
        )


@app.get("/api/admin/backfill-stats", response_model=AdminBackfillStatus)
def admin_backfill_status(_admin: auth.AuthUser = Depends(auth.require_admin)) -> AdminBackfillStatus:
    return AdminBackfillStatus(**_backfill_state)


@app.get("/api/admin/billing", response_model=AdminBillingResponse)
def admin_billing(
    _admin: auth.AuthUser = Depends(auth.require_admin), db: Session = Depends(get_db)
) -> AdminBillingResponse:
    """Altas, MRR y desglose por plan — main.PLAN_TARGET_LIMITS aside, this
    is the money view: how many people actually pay, how much, and (via
    subscribed_at, see billing.apply_event) when they signed up."""
    users = db.query(User).all()

    counts = {status: 0 for status in SubscriptionStatus}
    by_plan: dict[str, int] = {}
    mrr = 0.0
    for u in users:
        counts[u.subscription_status] += 1
        if u.subscription_status in (SubscriptionStatus.active, SubscriptionStatus.trialing) and u.plan:
            by_plan[u.plan.value] = by_plan.get(u.plan.value, 0) + 1
            mrr += billing.PLAN_PRICES_EUR.get(u.plan, 0.0)

    since = datetime.utcnow() - timedelta(days=13)
    signup_dates = (
        db.query(User.subscribed_at)
        .filter(User.subscribed_at.isnot(None), User.subscribed_at >= since)
        .all()
    )
    by_day: dict[str, int] = {}
    for (subscribed_at,) in signup_dates:
        key = subscribed_at.date().isoformat()
        by_day[key] = by_day.get(key, 0) + 1

    return AdminBillingResponse(
        active_count=counts[SubscriptionStatus.active],
        trialing_count=counts[SubscriptionStatus.trialing],
        past_due_count=counts[SubscriptionStatus.past_due],
        canceled_count=counts[SubscriptionStatus.canceled],
        by_plan=by_plan,
        mrr=round(mrr, 2),
        signups_by_day=[
            AdminBillingDayCount(day=day, count=count) for day, count in sorted(by_day.items(), reverse=True)
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

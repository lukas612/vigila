"""SQLAlchemy models mirroring the data model from VIGILA_BRIEF.md section 4.

Designed to run against Postgres (Supabase) in production; SQLite works
fine for local dev (see db.py). Uses SQLAlchemy 2.0 typed mapped columns.
"""
from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Enum, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _uuid() -> str:
    return str(uuid.uuid4())


class SubscriptionStatus(str, enum.Enum):
    none = "none"
    trialing = "trialing"
    active = "active"
    past_due = "past_due"
    canceled = "canceled"


class Plan(str, enum.Enum):
    individual = "individual"
    familiar = "familiar"


class CheckResultEnum(str, enum.Enum):
    found = "found"
    not_found = "not_found"
    error = "error"


class User(Base):
    """App-level profile, keyed by the same id Supabase Auth assigned the
    user (its uuid) — identity itself (email verification, magic-link
    tokens, sessions) lives entirely in Supabase Auth's own `auth.users`;
    this row only exists for the billing fields Supabase Auth doesn't
    track. Created lazily on first login (see main.create_session)."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    stripe_customer_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    subscription_status: Mapped[SubscriptionStatus] = mapped_column(
        Enum(SubscriptionStatus), default=SubscriptionStatus.none
    )
    plan: Mapped[Plan | None] = mapped_column(Enum(Plan), nullable=True)

    # Collected by Stripe Checkout itself (billing_address_collection +
    # phone_number_collection, see billing.create_checkout_session) and
    # copied here from the checkout.session.completed webhook — a name and
    # phone alongside the email makes a subscriber feel like a real,
    # accountable customer rather than an anonymous address.
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)

    # Set once, the first time subscription_status ever becomes active —
    # never overwritten on a later renewal/plan switch — so the admin
    # panel's "altas por día" reflects real signup dates, not created_at
    # (which is account creation, possibly weeks before they ever paid).
    subscribed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # "button" (see the single-button "Activar vigilancia" CTA) vs "direct"
    # (skip it, send them straight into Stripe Checkout) — assigned once,
    # 50/50, in main._pre_attach_waitlist_target, at the exact moment a
    # pending target gets attached (so it's only ever set for the accounts
    # this A/B test actually applies to; sticky for that account's whole
    # lifetime, never reassigned). Null for everyone outside the test —
    # includes every account that existed before this column did, and
    # cuenta.html treats null the same as "button" (the safer default: no
    # surprise auto-redirect for someone who's never seen the CTA).
    ab_pending_cta_variant: Mapped[str | None] = mapped_column(String(20), nullable=True)

    monitored_ids: Mapped[list["MonitoredId"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class MonitoredId(Base):
    __tablename__ = "monitored_ids"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Fernet ciphertext, not the DNI/NIE/matrícula itself — see crypto.py.
    # Unlike checks_free (which only ever needs a one-way hash), the daily
    # monitoring cron has to decrypt this to re-query the BOE.
    value_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="monitored_ids")
    notifications: Mapped[list["Notification"]] = relationship(
        back_populates="monitored_id_ref", cascade="all, delete-orphan"
    )


class CheckFree(Base):
    """Anonymous free-check log for rate limiting/analytics.

    value_hash is a one-way SHA-256, kept for fast dedup/abuse checks
    (rate_limit.hash_identifier) — it can never be reversed to the
    original DNI/matrícula.

    value_encrypted (added 2026-09) is the same value, reversibly
    encrypted with crypto.encrypt_value (the same Fernet key used for
    MonitoredId), so the admin panel's search history can show what was
    actually searched — see admin_checks_list. Nullable because rows
    created before this field existed have no value here. Explicit
    account-owner decision to start retaining this for anonymous
    checks too, not just paying users' monitored targets — see the
    privacy policy (privacidad.html) for the retention this implies.
    """

    __tablename__ = "checks_free"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    ip_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    fingerprint_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    value_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    value_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[CheckResultEnum] = mapped_column(Enum(CheckResultEnum), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, index=True)


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("monitored_id", "boe_ref", "expediente", name="uq_notifications_target_expediente"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    monitored_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("monitored_ids.id", ondelete="CASCADE"), nullable=False, index=True
    )
    boe_ref: Mapped[str] = mapped_column(String(40), nullable=False)
    expediente: Mapped[str | None] = mapped_column(String(40), nullable=True)
    matricula: Mapped[str | None] = mapped_column(String(20), nullable=True)
    localidad: Mapped[str | None] = mapped_column(String(120), nullable=True)
    importe: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    fecha: Mapped[str | None] = mapped_column(String(20), nullable=True)
    precepto: Mapped[str | None] = mapped_column(String(120), nullable=True)
    articulo: Mapped[str | None] = mapped_column(String(40), nullable=True)
    plazo_alegacion_fin: Mapped[date | None] = mapped_column(Date, nullable=True)
    seen_by_user: Mapped[bool] = mapped_column(Boolean, default=False)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    monitored_id_ref: Mapped["MonitoredId"] = relationship(back_populates="notifications")


class WaitlistSignup(Base):
    """Email captured from the landing page's post-result CTA, before a real
    account/subscription exists."""

    __tablename__ = "waitlist_signups"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    context: Mapped[str] = mapped_column(String(20), nullable=False)  # "ok" | "alert"
    # Fernet ciphertext of the DNI/NIE/matrícula that was just checked, when
    # this signup came right off a free check (index.html's wireEmail) — the
    # pricing-card email capture has no check behind it, so this stays null
    # there. Same key/scheme as MonitoredId.value_encrypted (see crypto.py),
    # so it can be copied straight across without a decrypt/re-encrypt round
    # trip — see main._ensure_profile, which does exactly that to pre-attach
    # this value to the account the first time this email ever logs in.
    value_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class CheckoutAttempt(Base):
    """One row per POST /api/billing/checkout — someone clicked to
    subscribe, whether or not they ever completed payment on Stripe's side.
    We had no server-side record of this funnel step at all before (only a
    client-side GA4 event, lost to ad blockers/declined consent) — this is
    the ground truth, independent of that. completed_subscription is
    updated after the fact by billing.apply_event's first activation for
    this user, so the admin panel can show real checkout->paid conversion,
    not just "checkout clicks" in isolation."""

    __tablename__ = "checkout_attempts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    plan: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, index=True)
    completed_subscription: Mapped[bool] = mapped_column(Boolean, default=False)


class DailyStat(Base):
    """Aggregate-only stats scraped from the BOE's full daily traffic listing
    (not the per-user check pipeline). Deliberately never stores anything at
    the level of an individual expediente/DNI — only counts and totals per
    locality per day, safe to show publicly on the landing page."""

    __tablename__ = "daily_stats"
    __table_args__ = (UniqueConstraint("stat_date", "localidad", name="uq_daily_stats_date_localidad"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    stat_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    localidad: Mapped[str] = mapped_column(String(120), nullable=False)
    expedientes_count: Mapped[int] = mapped_column(Integer, nullable=False)
    importe_total: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    # How many of this locality's expedientes that day carried a DNI/NIE vs
    # a matrícula (a row can have neither, either, or both — see
    # pdf_parser.NotificationRow) — still aggregate-only, this is just a
    # count, never the identifiers themselves.
    con_dni_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    con_matricula_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class StatsSummary(Base):
    """Precomputed response bodies for the hot stats endpoints
    (/api/stats/weekly and /api/stats/latest with no query params),
    refreshed once by the daily crawl instead of re-aggregating every row
    in daily_stats on every single page visit. See app/stats.py."""

    __tablename__ = "stats_summary"

    key: Mapped[str] = mapped_column(String(40), primary_key=True)  # "weekly" | "daily_latest"
    payload: Mapped[str] = mapped_column(Text, nullable=False)  # JSON-encoded response body
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class NotificationRun(Base):
    """Log of each cron cycle per monitored_id, for debugging."""

    __tablename__ = "notification_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    monitored_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("monitored_ids.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ran_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, index=True)
    result: Mapped[CheckResultEnum] = mapped_column(Enum(CheckResultEnum), nullable=False)
    raw_response_snippet: Mapped[str | None] = mapped_column(Text, nullable=True)


class LifecycleEmailKey(str, enum.Enum):
    """One entry per email in the welcome/conversion series (see
    app/lifecycle_emails.py). The '_N' suffix is days since the reference
    event (created_at for the no-plan series, subscribed_at for the paid
    series) — '0' means sent immediately from the event itself."""

    welcome_no_plan_0 = "welcome_no_plan_0"
    welcome_no_plan_2 = "welcome_no_plan_2"
    welcome_no_plan_5 = "welcome_no_plan_5"
    welcome_no_plan_10 = "welcome_no_plan_10"
    welcome_paid_0 = "welcome_paid_0"
    welcome_paid_no_target_2 = "welcome_paid_no_target_2"
    welcome_paid_3 = "welcome_paid_3"
    welcome_paid_30 = "welcome_paid_30"


class LifecycleEmailLog(Base):
    """Records that a given lifecycle email has already gone out to a given
    user, so the daily cron (scripts/send_lifecycle_emails.py) never sends
    the same step twice — it just checks "does a row exist?" every run
    instead of tracking cursors. payment_failed isn't tracked here: it
    fires straight from the Stripe webhook on each fresh transition into
    past_due (see billing.apply_event), which can legitimately happen more
    than once per user, unlike the rest of this onboarding series."""

    __tablename__ = "lifecycle_email_log"
    __table_args__ = (UniqueConstraint("user_id", "email_key", name="uq_lifecycle_email_user_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    email_key: Mapped[LifecycleEmailKey] = mapped_column(Enum(LifecycleEmailKey), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

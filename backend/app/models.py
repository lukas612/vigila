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

    Per the brief's privacy section: never store the raw DNI/matricula here,
    only a hash, unless the user goes on to subscribe.
    """

    __tablename__ = "checks_free"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    ip_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    fingerprint_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    value_hash: Mapped[str] = mapped_column(String(64), nullable=False)
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


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

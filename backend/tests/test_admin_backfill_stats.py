"""The scheduled daily_stats.yml cron is known to run hours late on this
low-traffic repo (GitHub Actions deprioritizes scheduled runs) — the admin
panel's manual backfill button is the fallback. main._missing_stat_days and
the /api/admin/backfill-stats endpoints must correctly find the gap, run
it in the background, and never double-run while one is already in
progress. See main.py's comment above _BACKFILL_MAX_DAYS."""
from __future__ import annotations

import os
import time
from datetime import timedelta
from unittest.mock import patch

from cryptography.fernet import Fernet

os.environ.setdefault("TARGET_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ["DATABASE_URL"] = "sqlite:////tmp/test_admin_backfill_stats.db"
if os.path.exists("/tmp/test_admin_backfill_stats.db"):
    os.remove("/tmp/test_admin_backfill_stats.db")

import pytest
from fastapi.testclient import TestClient

from app import auth, main
from app.db import SessionLocal, init_db
from app.models import DailyStat
from scripts import crawl_daily_stats


@pytest.fixture()
def db_session():
    init_db()
    session = SessionLocal()
    try:
        yield session
    finally:
        session.query(DailyStat).delete()
        session.commit()
        session.close()
        main._backfill_state.update(running=False, missing_days=[], done_days=[], started_at=None)


@pytest.fixture()
def client():
    main.app.dependency_overrides[auth.require_admin] = lambda: auth.AuthUser(id="admin", email="admin@test.com")
    yield TestClient(main.app)
    main.app.dependency_overrides.pop(auth.require_admin, None)


def _wait_until_idle(client, timeout=2.0):
    deadline = time.time() + timeout
    status = None
    while time.time() < deadline:
        status = client.get("/api/admin/backfill-stats").json()
        if not status["running"]:
            return status
        time.sleep(0.02)
    raise AssertionError(f"backfill never finished: {status}")


def test_missing_stat_days_defaults_to_a_full_window_when_table_is_empty(db_session):
    days = main._missing_stat_days(db_session)
    assert len(days) == main._BACKFILL_MAX_DAYS
    assert days[-1] == crawl_daily_stats._yesterday_madrid()


def test_missing_stat_days_starts_right_after_the_latest_row(db_session):
    yesterday = crawl_daily_stats._yesterday_madrid()
    seed_day = yesterday - timedelta(days=2)
    db_session.add(DailyStat(stat_date=seed_day, localidad="MADRID", expedientes_count=1, importe_total=10.0))
    db_session.commit()

    days = main._missing_stat_days(db_session)

    assert days == [seed_day + timedelta(days=1), seed_day + timedelta(days=2)]


def test_missing_stat_days_empty_when_already_up_to_date(db_session):
    yesterday = crawl_daily_stats._yesterday_madrid()
    db_session.add(DailyStat(stat_date=yesterday, localidad="MADRID", expedientes_count=1, importe_total=10.0))
    db_session.commit()

    assert main._missing_stat_days(db_session) == []


def test_backfill_endpoint_crawls_each_missing_day_and_skips_no_bulletin_days(db_session, client):
    yesterday = crawl_daily_stats._yesterday_madrid()
    seed_day = yesterday - timedelta(days=2)
    db_session.add(DailyStat(stat_date=seed_day, localidad="MADRID", expedientes_count=1, importe_total=10.0))
    db_session.commit()

    no_bulletin_day = seed_day + timedelta(days=1)

    def fake_crawl(day):
        if day == no_bulletin_day:
            return {}
        return {"MADRID": crawl_daily_stats.LocalityTotals(count=2, importe=50.0)}

    with patch("scripts.crawl_daily_stats.crawl", side_effect=fake_crawl):
        resp = client.post("/api/admin/backfill-stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["running"] is True
        assert data["missing_days"] == [no_bulletin_day.isoformat(), yesterday.isoformat()]

        status = _wait_until_idle(client)

    assert sorted(status["done_days"]) == sorted(data["missing_days"])
    stored_days = {r.stat_date for r in db_session.query(DailyStat).all()}
    assert no_bulletin_day not in stored_days  # nothing stored for a no-bulletins day
    assert yesterday in stored_days


def test_backfill_endpoint_reports_nothing_missing_once_caught_up(db_session, client):
    yesterday = crawl_daily_stats._yesterday_madrid()
    db_session.add(DailyStat(stat_date=yesterday, localidad="MADRID", expedientes_count=1, importe_total=10.0))
    db_session.commit()

    resp = client.post("/api/admin/backfill-stats")
    data = resp.json()

    assert data["running"] is False
    assert data["missing_days"] == []


def test_backfill_endpoint_refuses_to_start_a_second_run_while_one_is_in_progress(db_session, client):
    yesterday = crawl_daily_stats._yesterday_madrid()
    seed_day = yesterday - timedelta(days=1)
    db_session.add(DailyStat(stat_date=seed_day, localidad="MADRID", expedientes_count=1, importe_total=10.0))
    db_session.commit()

    main._backfill_state.update(running=True, missing_days=["2020-01-01"], done_days=[], started_at="earlier")
    try:
        resp = client.post("/api/admin/backfill-stats")
        data = resp.json()
        assert data["running"] is True
        assert data["missing_days"] == ["2020-01-01"]
        assert "en curso" in data["message"]
    finally:
        main._backfill_state.update(running=False, missing_days=[], done_days=[], started_at=None)

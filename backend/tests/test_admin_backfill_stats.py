"""The scheduled daily_stats.yml cron is known to run hours late on this
low-traffic repo (GitHub Actions deprioritizes scheduled runs) — the admin
panel's manual backfill button is the fallback. It dispatches the same
GitHub Actions workflow (workflow_dispatch) rather than crawling PDFs
in-process — an earlier in-process version OOM'd the Render web service.
main._missing_stat_days and the /api/admin/backfill-stats endpoints must
correctly find the gap, dispatch it in the background, and never
double-run while one is already in progress. See main.py's comment above
_BACKFILL_MAX_DAYS."""
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
    old_token = main.GITHUB_ACTIONS_TOKEN
    main.GITHUB_ACTIONS_TOKEN = "fake-token-for-tests"
    yield TestClient(main.app)
    main.app.dependency_overrides.pop(auth.require_admin, None)
    main.GITHUB_ACTIONS_TOKEN = old_token


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


def test_backfill_endpoint_dispatches_a_workflow_run_per_missing_day(db_session, client):
    """The endpoint must never crawl PDFs itself (that OOM'd the web
    service before) — it only fires workflow_dispatch once per missing day
    and lets GitHub's runner do the actual crawling."""
    yesterday = crawl_daily_stats._yesterday_madrid()
    seed_day = yesterday - timedelta(days=2)
    db_session.add(DailyStat(stat_date=seed_day, localidad="MADRID", expedientes_count=1, importe_total=10.0))
    db_session.commit()

    expected_day_1 = (seed_day + timedelta(days=1)).isoformat()
    expected_day_2 = yesterday.isoformat()
    dispatched_dates = []

    class _FakeResponse:
        def raise_for_status(self):
            pass

    def fake_post(url, headers=None, json=None, timeout=None):
        assert "workflows/daily_stats.yml/dispatches" in url
        assert headers["Authorization"] == "Bearer fake-token-for-tests"
        dispatched_dates.append(json["inputs"]["date"])
        return _FakeResponse()

    with patch("app.main.httpx.post", side_effect=fake_post) as mock_post:
        resp = client.post("/api/admin/backfill-stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["running"] is True
        assert data["missing_days"] == [expected_day_1, expected_day_2]

        status = _wait_until_idle(client)

    assert mock_post.call_count == 2
    assert sorted(dispatched_dates) == sorted([expected_day_1, expected_day_2])
    assert sorted(status["done_days"]) == sorted(data["missing_days"])
    # No local crawling ever happens — the DB is untouched by this endpoint.
    assert db_session.query(DailyStat).count() == 1


def test_backfill_endpoint_refuses_without_a_github_token(db_session, client):
    old_token = main.GITHUB_ACTIONS_TOKEN
    main.GITHUB_ACTIONS_TOKEN = ""
    try:
        resp = client.post("/api/admin/backfill-stats")
        assert resp.status_code == 500
    finally:
        main.GITHUB_ACTIONS_TOKEN = old_token


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

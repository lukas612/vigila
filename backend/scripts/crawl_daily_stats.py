"""Crawls a full day of BOE traffic bulletins nationally and stores
aggregate-only counts per locality — never anything at the level of an
individual expediente or DNI. Meant to run once a day from GitHub Actions
(see .github/workflows/daily_stats.yml), not from the FastAPI app itself:
downloading and parsing ~150-200 PDFs is far too slow/CPU-heavy to do inside
a web request, especially on Render's free tier.

Usage:
    python -m scripts.crawl_daily_stats [--date YYYY-MM-DD]

Defaults to yesterday (Europe/Madrid) since a day's bulletins are still
being published throughout "today".
"""
from __future__ import annotations

import argparse
import json
import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app import boe_client, pdf_parser, stats as stats_lib
from app.db import SessionLocal, init_db
from app.models import DailyStat, StatsSummary

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("crawl_daily_stats")

MAX_WORKERS = 10


@dataclass
class LocalityTotals:
    count: int = 0
    importe: float = 0.0
    con_dni: int = 0
    con_matricula: int = 0


def _yesterday_madrid() -> date:
    return (datetime.now(ZoneInfo("Europe/Madrid")) - timedelta(days=1)).date()


def _fetch_and_extract(candidate: boe_client.BoeCandidate) -> list[pdf_parser.NotificationRow]:
    try:
        pdf_bytes = pdf_parser.fetch_pdf_bytes(candidate.pdf_url)
        return pdf_parser.extract_rows(pdf_bytes, candidate.boe_ref, candidate.published_on)
    except pdf_parser.PdfFetchError as exc:
        logger.warning("failed to fetch/parse %s: %s", candidate.boe_ref, exc)
        return []


def crawl(day: date) -> dict[str, LocalityTotals]:
    """Returns {localidad: LocalityTotals} for every expediente published on
    `day`. con_dni/con_matricula count rows that carried an identified
    DNI/NIE or matrícula respectively — a row can have either, both or
    neither (see pdf_parser.NotificationRow) — still aggregate-only, these
    are just counts, never the identifiers themselves."""
    candidates = boe_client.search_by_date(day)
    logger.info("found %d bulletins published on %s", len(candidates), day)

    totals: dict[str, LocalityTotals] = defaultdict(LocalityTotals)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(_fetch_and_extract, c) for c in candidates]
        for i, future in enumerate(as_completed(futures), 1):
            rows = future.result()
            for row in rows:
                localidad = row.localidad.strip()
                if not localidad:
                    continue
                bucket = totals[localidad]
                bucket.count += 1
                try:
                    bucket.importe += float(row.importe.replace(",", "."))
                except (ValueError, AttributeError):
                    pass
                if row.identif:
                    bucket.con_dni += 1
                if row.matricula:
                    bucket.con_matricula += 1
            if i % 20 == 0 or i == len(candidates):
                logger.info("parsed %d/%d bulletins", i, len(candidates))

    return dict(totals)


def store(day: date, locality_totals: dict[str, LocalityTotals]) -> None:
    init_db()
    db = SessionLocal()
    try:
        db.query(DailyStat).filter(DailyStat.stat_date == day).delete()
        for localidad, t in locality_totals.items():
            db.add(
                DailyStat(
                    stat_date=day,
                    localidad=localidad,
                    expedientes_count=t.count,
                    importe_total=round(t.importe, 2),
                    con_dni_count=t.con_dni,
                    con_matricula_count=t.con_matricula,
                )
            )
        db.commit()
        logger.info("stored %d localities for %s", len(locality_totals), day)

        _refresh_summary_cache(db, day)
    finally:
        db.close()


def _upsert_summary(db, key: str, payload: dict | None) -> None:
    if payload is None:
        return
    row = db.query(StatsSummary).filter(StatsSummary.key == key).first()
    if row is None:
        row = StatsSummary(key=key, payload="{}")
        db.add(row)
    row.payload = json.dumps(payload)
    row.updated_at = datetime.utcnow()
    db.commit()


def _refresh_summary_cache(db, day: date) -> None:
    """Recomputes the cached response bodies /api/stats/weekly and
    /api/stats/latest serve by default, so the API never has to
    re-aggregate daily_stats on a page visit — see app/stats.py and
    main.py's _cached_summary."""
    _upsert_summary(db, "weekly", stats_lib.compute_weekly(db, days=7))

    most_recent = db.query(DailyStat.stat_date).order_by(DailyStat.stat_date.desc()).limit(1).scalar()
    if day == most_recent:
        # Only overwrite "the latest day" if this crawl actually IS the
        # latest — a backfill for an older date must never clobber it.
        _upsert_summary(db, "daily_latest", stats_lib.compute_daily(db, day))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=str, default=None, help="YYYY-MM-DD, defaults to yesterday (Europe/Madrid)")
    args = parser.parse_args()

    day = date.fromisoformat(args.date) if args.date else _yesterday_madrid()
    logger.info("crawling %s", day)

    locality_totals = crawl(day)
    if not locality_totals:
        # Expected on weekends/holidays — the BOE doesn't publish then, not
        # a crawl failure. Confirmed live: 2026-09-13 (a Sunday) has zero
        # MATERIA=43 bulletins nationally. Leave any existing row alone.
        logger.info("no bulletins published on %s (weekend/holiday?) — nothing to store", day)
        return

    store(day, locality_totals)
    total_count = sum(t.count for t in locality_totals.values())
    total_importe = sum(t.importe for t in locality_totals.values())
    logger.info(
        "done: %d expedientes, %.2f€ total, %d localities", total_count, total_importe, len(locality_totals)
    )


if __name__ == "__main__":
    main()

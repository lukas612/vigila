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
import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app import boe_client, pdf_parser
from app.db import SessionLocal, init_db
from app.models import DailyStat

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("crawl_daily_stats")

MAX_WORKERS = 10


def _yesterday_madrid() -> date:
    return (datetime.now(ZoneInfo("Europe/Madrid")) - timedelta(days=1)).date()


def _fetch_and_extract(candidate: boe_client.BoeCandidate) -> list[pdf_parser.NotificationRow]:
    try:
        pdf_bytes = pdf_parser.fetch_pdf_bytes(candidate.pdf_url)
        return pdf_parser.extract_rows(pdf_bytes, candidate.boe_ref, candidate.published_on)
    except pdf_parser.PdfFetchError as exc:
        logger.warning("failed to fetch/parse %s: %s", candidate.boe_ref, exc)
        return []


def crawl(day: date) -> dict[str, tuple[int, float]]:
    """Returns {localidad: (count, importe_total)} for every expediente
    published on `day`."""
    candidates = boe_client.search_by_date(day)
    logger.info("found %d bulletins published on %s", len(candidates), day)

    counts: dict[str, int] = defaultdict(int)
    totals: dict[str, float] = defaultdict(float)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(_fetch_and_extract, c) for c in candidates]
        for i, future in enumerate(as_completed(futures), 1):
            rows = future.result()
            for row in rows:
                localidad = row.localidad.strip()
                if not localidad:
                    continue
                counts[localidad] += 1
                try:
                    totals[localidad] += float(row.importe.replace(",", "."))
                except (ValueError, AttributeError):
                    pass
            if i % 20 == 0 or i == len(candidates):
                logger.info("parsed %d/%d bulletins", i, len(candidates))

    return {loc: (counts[loc], totals[loc]) for loc in counts}


def store(day: date, stats: dict[str, tuple[int, float]]) -> None:
    init_db()
    db = SessionLocal()
    try:
        db.query(DailyStat).filter(DailyStat.stat_date == day).delete()
        for localidad, (count, importe_total) in stats.items():
            db.add(
                DailyStat(
                    stat_date=day,
                    localidad=localidad,
                    expedientes_count=count,
                    importe_total=round(importe_total, 2),
                )
            )
        db.commit()
        logger.info("stored %d localities for %s", len(stats), day)
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=str, default=None, help="YYYY-MM-DD, defaults to yesterday (Europe/Madrid)")
    args = parser.parse_args()

    day = date.fromisoformat(args.date) if args.date else _yesterday_madrid()
    logger.info("crawling %s", day)

    stats = crawl(day)
    if not stats:
        # Expected on weekends/holidays — the BOE doesn't publish then, not
        # a crawl failure. Confirmed live: 2026-09-13 (a Sunday) has zero
        # MATERIA=43 bulletins nationally. Leave any existing row alone.
        logger.info("no bulletins published on %s (weekend/holiday?) — nothing to store", day)
        return

    store(day, stats)
    total_count = sum(c for c, _ in stats.values())
    total_importe = sum(t for _, t in stats.values())
    logger.info("done: %d expedientes, %.2f€ total, %d localities", total_count, total_importe, len(stats))


if __name__ == "__main__":
    main()

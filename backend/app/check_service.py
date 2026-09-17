"""Orchestrates the full check pipeline: BOE search -> PDF download -> exact-match verification.

This is what both the free (anonymous) check endpoint and the future
subscription cron job should call — the two must never diverge on what
counts as "found", since a false positive here means telling someone
they have a fine they don't have, and a false negative means silence on
a plazo that's actually running out.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from . import boe_client, pdf_parser

# Candidates are downloaded+parsed concurrently (each is a slow, independent
# network fetch) rather than one at a time — a value matching a dozen
# provincial bulletins was taking 50+ seconds serially, against a "check in
# 10 seconds" product promise.
MAX_CONCURRENT_CANDIDATES = 6


@dataclass(frozen=True)
class CheckResult:
    found: bool
    matches: list[pdf_parser.NotificationRow] = field(default_factory=list)
    candidates_checked: int = 0
    errors: list[str] = field(default_factory=list)


def _check_candidate(candidate: boe_client.BoeCandidate, normalized: str) -> list[pdf_parser.NotificationRow]:
    pdf_bytes = pdf_parser.fetch_pdf_bytes(candidate.pdf_url)
    return pdf_parser.find_matches(pdf_bytes, candidate.boe_ref, normalized)


def run_check(value: str) -> CheckResult:
    """Run the full pipeline for a single DNI/NIE/matrícula. Never raises for
    upstream failures — partial errors are reported in `errors` so the caller
    can decide whether to show a generic error to the user."""
    normalized = pdf_parser.normalize(value)
    if not normalized:
        raise ValueError("value must not be empty")

    errors: list[str] = []
    try:
        candidates = boe_client.search(normalized)
    except boe_client.BoeClientError as exc:
        return CheckResult(found=False, errors=[str(exc)])

    matches: list[pdf_parser.NotificationRow] = []
    if candidates:
        with ThreadPoolExecutor(max_workers=min(MAX_CONCURRENT_CANDIDATES, len(candidates))) as pool:
            future_to_candidate = {
                pool.submit(_check_candidate, candidate, normalized): candidate for candidate in candidates
            }
            for future in as_completed(future_to_candidate):
                candidate = future_to_candidate[future]
                try:
                    matches.extend(future.result())
                except pdf_parser.PdfFetchError as exc:
                    errors.append(f"{candidate.boe_ref}: {exc}")

    return CheckResult(
        found=bool(matches),
        matches=matches,
        candidates_checked=len(candidates),
        errors=errors,
    )

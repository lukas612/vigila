"""Orchestrates the full check pipeline: BOE search -> PDF download -> exact-match verification.

This is what both the free (anonymous) check endpoint and the future
subscription cron job should call — the two must never diverge on what
counts as "found", since a false positive here means telling someone
they have a fine they don't have, and a false negative means silence on
a plazo that's actually running out.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import boe_client, pdf_parser


@dataclass(frozen=True)
class CheckResult:
    found: bool
    matches: list[pdf_parser.NotificationRow] = field(default_factory=list)
    candidates_checked: int = 0
    errors: list[str] = field(default_factory=list)


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
    for candidate in candidates:
        try:
            pdf_bytes = pdf_parser.fetch_pdf_bytes(candidate.pdf_url)
        except pdf_parser.PdfFetchError as exc:
            errors.append(f"{candidate.boe_ref}: {exc}")
            continue
        matches.extend(pdf_parser.find_matches(pdf_bytes, candidate.boe_ref, normalized))

    return CheckResult(
        found=bool(matches),
        matches=matches,
        candidates_checked=len(candidates),
        errors=errors,
    )

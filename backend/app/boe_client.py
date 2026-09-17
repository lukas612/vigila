"""Client for the BOE Tablón Edictal Único (TEU) notifications search.

The public endpoint at boe.es/notificaciones/notificaciones.php is a plain
GET form, no login/CAPTCHA required. It does a full-text search over the
indexed PDF content, which means it can return false positives for short
numeric/alphanumeric values (confirmed live: a fake DNI matched 200+
unrelated documents). Treat every hit from `search` as a *candidate* only —
callers must verify an exact row match in the underlying PDF (see
`pdf_parser.py`) before treating anything as a real notification.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.boe.es"
SEARCH_PATH = "/notificaciones/notificaciones.php"

# MATERIA=43 => "TRAFICO, CIRCULACION Y SEGURIDAD VIAL"
MATERIA_TRAFICO = "43"

NOT_FOUND_TEXT = "No se han encontrado documentos"

USER_AGENT = "VigilaBot/0.1 (+monitorizacion de notificaciones de trafico)"


@dataclass(frozen=True)
class BoeCandidate:
    boe_ref: str
    pdf_url: str
    published_on: date | None = None


class BoeClientError(RuntimeError):
    """Raised when the BOE endpoint can't be reached or parsed."""


def _build_params(value: str, materia: str | None = MATERIA_TRAFICO) -> list[tuple[str, str]]:
    # Field/operator pairs mirror the exact shape confirmed against the live
    # endpoint. campo[4]/FPU (date range) is left empty (no date filtering).
    return [
        ("campo[0]", "DOC"),
        ("dato[0]", value),
        ("operador[0]", "and"),
        ("campo[1]", "DEM"),
        ("dato[1]", ""),
        ("operador[1]", "and"),
        ("campo[2]", "MATERIA"),
        ("dato[2]", materia or ""),
        ("operador[2]", "and"),
        ("campo[3]", "NBO"),
        ("dato[3]", ""),
        ("operador[4]", "and"),
        ("campo[4]", "FPU"),
        ("dato[4][0]", ""),
        ("dato[4][1]", ""),
        ("page_hits", "50"),
        ("sort_field[0]", "FPU"),
        ("sort_order[0]", "desc"),
        ("sort_field[1]", "id"),
        ("sort_order[1]", "asc"),
        ("accion", "Buscar"),
    ]


def search(value: str, *, materia: str | None = MATERIA_TRAFICO, timeout: float = 20.0) -> list[BoeCandidate]:
    """Query the BOE notifications search for `value` and return candidate PDFs.

    `value` should already be normalized (uppercase, no surrounding spaces).
    Results are *candidates* — always verify with `pdf_parser.find_matches`.
    """
    if not value or not value.strip():
        raise ValueError("value must not be empty")

    params = _build_params(value.strip())
    try:
        resp = httpx.get(
            urljoin(BASE_URL, SEARCH_PATH),
            params=params,
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
            follow_redirects=True,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise BoeClientError(f"failed to query BOE: {exc}") from exc

    return _parse_results(resp.text)


def _extract_date(pdf_href: str) -> date | None:
    """Pull the publication date out of the PDF path itself
    (.../dias/YYYY/MM/DD/not.php?...) — this is the edict's actual
    publication date, which is what the 20-day plazo counts from. The PDF's
    own text has no reliable machine-readable date (the footer text comes
    out reversed/garbled from pdfplumber's extraction)."""
    match = re.search(r"/dias/(\d{4})/(\d{2})/(\d{2})/", pdf_href)
    if not match:
        return None
    year, month, day = (int(g) for g in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _parse_results(html: str) -> list[BoeCandidate]:
    if NOT_FOUND_TEXT in html:
        return []

    soup = BeautifulSoup(html, "lxml")
    candidates: list[BoeCandidate] = []
    seen: set[str] = set()

    for link in soup.select("li.resultado-busqueda a[href*='not.php']"):
        href = link.get("href", "")
        match = re.search(r"id=(BOE-N-\d{4}-\d+)", href)
        if not match:
            continue
        boe_ref = match.group(1)
        if boe_ref in seen:
            continue
        seen.add(boe_ref)
        candidates.append(
            BoeCandidate(boe_ref=boe_ref, pdf_url=urljoin(BASE_URL, href), published_on=_extract_date(href))
        )

    return candidates

"""Downloads a BOE notification PDF and extracts the sanction table rows.

Search results from `boe_client.search` are only candidates (the BOE
full-text search can match unrelated numeric substrings). This module does
the authoritative check: it parses the real table inside the PDF and only
reports a match when a row's IDENTIF (DNI/NIF) or MATRICULA column is an
*exact* match (after normalization) to the value the user is monitoring.
"""
from __future__ import annotations

import io
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta

import httpx
import pdfplumber

from .boe_client import USER_AGENT

PLAZO_ALEGACION_DIAS = 20

# Column names vary slightly between provincial bulletins (e.g. some include
# a DENUNCIADO/A name column, some don't) so rows are mapped by header name,
# not position.
IDENTIF_COLUMNS = {"IDENTIF", "IDENTIF.", "DNI", "NIF"}
MATRICULA_COLUMNS = {"MATRICULA", "MATRÍCULA"}


class PdfFetchError(RuntimeError):
    """Raised when the PDF can't be downloaded or has no parseable table."""


@dataclass(frozen=True)
class NotificationRow:
    boe_ref: str
    expediente: str
    identif: str
    matricula: str
    localidad: str
    fecha: str
    importe: str
    precepto: str
    articulo: str
    puntos: str
    requerimiento: str
    # Publication date of the edict itself (not `fecha`, which is the
    # infraction date) — this is what the 20 días naturales plazo counts
    # from. None when the candidate's date couldn't be determined.
    published_on: date | None = None

    @property
    def plazo_alegacion_fin(self) -> date | None:
        if self.published_on is None:
            return None
        return self.published_on + timedelta(days=PLAZO_ALEGACION_DIAS)


def normalize(value: str) -> str:
    """Normalize a DNI/NIE/matrícula for exact comparison (case/space-insensitive)."""
    return re.sub(r"[\s\-]", "", value or "").upper()


def _header_index(header_row: list[str | None], names: set[str]) -> int | None:
    for idx, cell in enumerate(header_row):
        if cell and normalize(cell) in {normalize(n) for n in names}:
            return idx
    return None


def _cell(row: list[str | None], idx: int | None) -> str:
    if idx is None or idx >= len(row) or row[idx] is None:
        return ""
    return row[idx].strip()


def fetch_pdf_bytes(pdf_url: str, *, timeout: float = 30.0) -> bytes:
    try:
        resp = httpx.get(pdf_url, headers={"User-Agent": USER_AGENT}, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise PdfFetchError(f"failed to download PDF: {exc}") from exc
    return resp.content


def _iter_page_rows(
    pdf: pdfplumber.PDF, boe_ref: str, published_on: date | None
) -> Iterator[list[NotificationRow]]:
    """Yield the rows found on each page, one page at a time.

    Parsing a 14-page bulletin is the dominant cost of a check (each page's
    vector content has to be walked to detect the table) — on a CPU-limited
    host this alone can take a minute. Yielding per page lets `find_matches`
    stop as soon as it has what it needs instead of always parsing every
    page.
    """
    header: list[str | None] | None = None

    for page in pdf.pages:
        page_rows: list[NotificationRow] = []
        for table in page.extract_tables():
            if not table:
                continue
            start = 0
            if header is None:
                header = table[0]
                start = 1
            elif table[0] and normalize(" ".join(c or "" for c in table[0])) == normalize(
                " ".join(c or "" for c in header)
            ):
                # Repeated header on a later page.
                start = 1

            exp_idx = _header_index(header, {"EXPEDIENTE"})
            identif_idx = _header_index(header, IDENTIF_COLUMNS)
            matricula_idx = _header_index(header, MATRICULA_COLUMNS)
            localidad_idx = _header_index(header, {"LOCALIDAD"})
            fecha_idx = _header_index(header, {"FECHA"})
            importe_idx = _header_index(header, {"CUANTÍA EUROS", "CUANTIA EUROS", "IMPORTE"})
            precepto_idx = _header_index(header, {"PRECEPTO"})
            articulo_idx = _header_index(header, {"ARTICULO", "ART°", "ART"})
            puntos_idx = _header_index(header, {"PUNTOS", "PTOS"})
            req_idx = _header_index(header, {"REQ", "OBS"})

            for raw_row in table[start:]:
                if not raw_row or not any(raw_row):
                    continue
                identif = _cell(raw_row, identif_idx)
                matricula = _cell(raw_row, matricula_idx)
                if not identif and not matricula:
                    continue
                page_rows.append(
                    NotificationRow(
                        boe_ref=boe_ref,
                        expediente=_cell(raw_row, exp_idx),
                        identif=identif,
                        matricula=matricula,
                        localidad=_cell(raw_row, localidad_idx),
                        fecha=_cell(raw_row, fecha_idx),
                        importe=_cell(raw_row, importe_idx),
                        precepto=_cell(raw_row, precepto_idx),
                        articulo=_cell(raw_row, articulo_idx),
                        puntos=_cell(raw_row, puntos_idx),
                        requerimiento=_cell(raw_row, req_idx),
                        published_on=published_on,
                    )
                )
        yield page_rows


def extract_rows(pdf_bytes: bytes, boe_ref: str, published_on: date | None = None) -> list[NotificationRow]:
    """Extract every table row from the PDF, regardless of who it belongs to."""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        return [row for page_rows in _iter_page_rows(pdf, boe_ref, published_on) for row in page_rows]


def find_matches(pdf_bytes: bytes, boe_ref: str, value: str, published_on: date | None = None) -> list[NotificationRow]:
    """Return only the rows whose IDENTIF or MATRICULA exactly equals `value`.

    Stops parsing further pages as soon as a page yields a match, since a
    given identifier/plate appears at most once per bulletin in practice —
    this is what keeps a check fast when the match is early in a long PDF.
    """
    target = normalize(value)
    matches: list[NotificationRow] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page_rows in _iter_page_rows(pdf, boe_ref, published_on):
            page_matches = [
                row for row in page_rows if normalize(row.identif) == target or normalize(row.matricula) == target
            ]
            matches.extend(page_matches)
            if page_matches:
                break
    return matches

from pathlib import Path

from app import pdf_parser

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_PDF = (FIXTURES / "sample_notification.pdf").read_bytes()
BOE_REF = "BOE-N-2026-672611"


def test_extract_rows_finds_known_real_row():
    rows = pdf_parser.extract_rows(SAMPLE_PDF, BOE_REF)

    assert len(rows) > 0
    row = next(r for r in rows if r.expediente == "7001618595")
    assert row.identif == "09213675J"
    assert row.matricula == "5178BDR"
    assert row.localidad == "MÉRIDA"
    assert row.importe == "100"


def test_find_matches_by_exact_dni():
    matches = pdf_parser.find_matches(SAMPLE_PDF, BOE_REF, "09213675J")

    assert len(matches) == 1
    assert matches[0].matricula == "5178BDR"


def test_find_matches_by_exact_plate_is_case_and_space_insensitive():
    matches = pdf_parser.find_matches(SAMPLE_PDF, BOE_REF, "5178 bdr")

    assert len(matches) == 1
    assert matches[0].identif == "09213675J"


def test_find_matches_rejects_partial_number_match():
    # A substring of a real identifier must NOT match (this is exactly the
    # false-positive failure mode the BOE full-text search itself has).
    matches = pdf_parser.find_matches(SAMPLE_PDF, BOE_REF, "9213675J")

    assert matches == []


def test_find_matches_returns_empty_for_unrelated_value():
    matches = pdf_parser.find_matches(SAMPLE_PDF, BOE_REF, "9999ZZZ")

    assert matches == []

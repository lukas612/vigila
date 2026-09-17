from pathlib import Path

import httpx
import respx

from app import boe_client, check_service, pdf_parser

FIXTURES = Path(__file__).parent / "fixtures"
SEARCH_HTML = (FIXTURES / "search_results.html").read_text()
SEARCH_EMPTY_HTML = (FIXTURES / "search_not_found.html").read_text()
SAMPLE_PDF = (FIXTURES / "sample_notification.pdf").read_bytes()


@respx.mock
def test_run_check_reports_not_found_when_search_is_empty():
    respx.get(url__regex=r".*notificaciones\.php.*").mock(return_value=httpx.Response(200, text=SEARCH_EMPTY_HTML))

    result = check_service.run_check("11111111H")

    assert result.found is False
    assert result.matches == []
    assert result.candidates_checked == 0


@respx.mock
def test_run_check_verifies_candidates_against_pdf_before_reporting_found():
    respx.get(url__regex=r".*notificaciones\.php.*").mock(return_value=httpx.Response(200, text=SEARCH_HTML))
    respx.get(url__regex=r".*not\.php.*").mock(return_value=httpx.Response(200, content=SAMPLE_PDF))

    # This value is NOT in the sample PDF table, so even though the search
    # endpoint returns 14 candidates, the pipeline must not report "found".
    result = check_service.run_check("9999ZZZ")

    assert result.found is False
    assert result.candidates_checked == 14


@respx.mock
def test_run_check_reports_found_for_a_real_row_in_the_pdf():
    respx.get(url__regex=r".*notificaciones\.php.*").mock(return_value=httpx.Response(200, text=SEARCH_HTML))
    respx.get(url__regex=r".*not\.php.*").mock(return_value=httpx.Response(200, content=SAMPLE_PDF))

    result = check_service.run_check("09213675J")

    assert result.found is True
    assert any(m.matricula == "5178BDR" for m in result.matches)


@respx.mock
def test_run_check_survives_partial_pdf_download_failures():
    respx.get(url__regex=r".*notificaciones\.php.*").mock(return_value=httpx.Response(200, text=SEARCH_HTML))
    respx.get(url__regex=r".*not\.php.*").mock(return_value=httpx.Response(500))

    result = check_service.run_check("09213675J")

    assert result.found is False
    assert result.errors  # download failures surfaced, not swallowed silently

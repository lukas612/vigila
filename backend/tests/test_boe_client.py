from pathlib import Path

import httpx
import pytest
import respx

from app import boe_client

FIXTURES = Path(__file__).parent / "fixtures"


@respx.mock
def test_search_returns_candidates_from_real_result_page():
    html = (FIXTURES / "search_results.html").read_text()
    respx.get(url__regex=r".*notificaciones\.php.*").mock(return_value=httpx.Response(200, text=html))

    candidates = boe_client.search("43")

    assert len(candidates) == 14
    assert candidates[0].boe_ref == "BOE-N-2026-672611"
    assert candidates[0].pdf_url.endswith("not.php?id=BOE-N-2026-672611")
    assert candidates[0].pdf_url.startswith("https://www.boe.es")


@respx.mock
def test_search_returns_empty_on_no_results():
    html = (FIXTURES / "search_not_found.html").read_text()
    respx.get(url__regex=r".*notificaciones\.php.*").mock(return_value=httpx.Response(200, text=html))

    candidates = boe_client.search("ZZZZZZNOEXISTE9999")

    assert candidates == []


def test_search_rejects_empty_value():
    with pytest.raises(ValueError):
        boe_client.search("")


@respx.mock
def test_search_raises_on_http_error():
    respx.get(url__regex=r".*notificaciones\.php.*").mock(return_value=httpx.Response(503))

    with pytest.raises(boe_client.BoeClientError):
        boe_client.search("12345678Z")

"""Serving the report: the document, and the two ways of asking for it.

``cipherchain.reporting`` already decides what the document SAYS and proves it
without a database. What this route adds is delivery, so these tests cover the
delivery failures: asking for the wrong format, asking for a run that does not
exist, and — the one that matters — asking for a PDF on a host that cannot
print one.

That last case is why the PDF path exists in this shape at all. A zero-byte or
truncated attachment in a case record is worse than no attachment: it opens
blank, and a blank report reads as "the tool found nothing" rather than "the
tool could not print". So a missing browser is an error with words in it, and
the HTML report keeps working while it is missing.
"""

from __future__ import annotations

import httpx
import pytest

from cipherchain.reporting import find_chromium
from tests.investigation.conftest import CHAIN, ROOT

CAN_PRINT = find_chromium() is not None
needs_chromium = pytest.mark.skipif(CAN_PRINT is False, reason="no headless chromium on this host")

BODY = {"chain": CHAIN, "address": ROOT, "objectives": ["find_prev_vasp", "find_next_vasp"]}


@pytest.fixture
async def investigation(client: httpx.AsyncClient) -> str:
    started = await client.post("/investigations", json=BODY)
    assert started.status_code == 201, started.text
    return str(started.json()["investigation_id"])


async def test_the_report_is_html_by_default(
    client: httpx.AsyncClient, investigation: str
) -> None:
    """A browser following a link gets the document, not a download prompt."""
    response = await client.get(f"/investigations/{investigation}/report")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert ROOT in response.text
    assert "CipherChain" in response.text


@needs_chromium
async def test_asking_for_pdf_returns_a_real_pdf(
    client: httpx.AsyncClient, investigation: str
) -> None:
    """Not "a response labelled application/pdf" — a file that opens.

    The header and a size floor are both checked because the renderer's own
    failure mode is a plausible-looking stub: Chromium exits 0 more often than
    it should, and a 200 carrying nine bytes would be filed as a report.
    """
    response = await client.get(f"/investigations/{investigation}/report?format=pdf")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")
    assert len(response.content) > 1024
    assert f"cipherchain-{investigation}.pdf" in response.headers["content-disposition"]


@needs_chromium
async def test_an_accept_header_can_ask_for_the_pdf_too(
    client: httpx.AsyncClient, investigation: str
) -> None:
    """Content negotiation, for a client that speaks it. ``?format=`` still
    wins, because a link in an email cannot set headers."""
    negotiated = await client.get(
        f"/investigations/{investigation}/report", headers={"Accept": "application/pdf"}
    )
    overridden = await client.get(
        f"/investigations/{investigation}/report?format=html",
        headers={"Accept": "application/pdf"},
    )

    assert negotiated.headers["content-type"] == "application/pdf"
    assert overridden.headers["content-type"].startswith("text/html")


async def test_a_browsers_wishlist_accept_header_still_gets_html(
    client: httpx.AsyncClient, investigation: str
) -> None:
    """Firefox sends ``text/html,application/xhtml+xml,...`` and means "a page".
    Reading that as a request for a file download would make every link a
    surprise attachment."""
    response = await client.get(
        f"/investigations/{investigation}/report",
        headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
    )

    assert response.headers["content-type"].startswith("text/html")


async def test_a_host_with_no_browser_still_serves_the_html_report(
    client: httpx.AsyncClient, investigation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of the split. No browser costs the deployment a file
    format, not the document — and the PDF refusal names what it looked for, so
    the answer is "install a browser" rather than "file a bug"."""
    monkeypatch.setenv("CIPHERCHAIN_CHROMIUM", "/nonexistent/chrome")

    html = await client.get(f"/investigations/{investigation}/report")
    pdf = await client.get(f"/investigations/{investigation}/report?format=pdf")

    assert html.status_code == 200
    assert ROOT in html.text

    assert pdf.status_code == 503
    detail = pdf.json()["detail"]
    assert "Searched" in detail
    assert "CIPHERCHAIN_CHROMIUM" in detail
    # Never an empty file dressed as a report.
    assert not pdf.content.startswith(b"%PDF")


async def test_an_unknown_format_is_refused_rather_than_guessed(
    client: httpx.AsyncClient, investigation: str
) -> None:
    """``?format=docx`` is a caller expecting something this route cannot make.
    Silently serving HTML would put the wrong thing in a case file."""
    response = await client.get(f"/investigations/{investigation}/report?format=docx")

    assert response.status_code == 422
    assert "docx" in response.json()["detail"]


async def test_a_report_for_an_unknown_investigation_is_404(client: httpx.AsyncClient) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    response = await client.get(f"/investigations/{missing}/report")

    assert response.status_code == 404


async def test_the_report_covers_a_run_that_is_not_finished(client: httpx.AsyncClient) -> None:
    """A partial run is reported on quite happily — and says it was partial.

    An investigator asking for the document mid-case must get one; refusing
    until "completed" would mean the only way to see progress is the API, and
    the report is the artefact that carries the caveats.
    """
    started = await client.post(
        "/investigations",
        json={**BODY, "budgets": {"api_calls": 1, "seconds": 300, "max_depth": 6, "max_nodes": 5}},
    )
    investigation_id = started.json()["investigation_id"]
    assert started.json()["status"] == "partial"

    response = await client.get(f"/investigations/{investigation_id}/report")

    assert response.status_code == 200
    assert "partial" in response.text.lower()

"""Printing, and the failures that must never look like success.

A zero-byte PDF attached to a case record is worse than no PDF at all: it reads
as a document that was produced and filed. Every failure path below is therefore
checked twice — that it raises, and that it left nothing behind.
"""

from __future__ import annotations

import os
import re
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from cipherchain.reporting.html import render_html
from cipherchain.reporting.pdf import (
    CHROMIUM_ENV,
    ChromiumNotFound,
    PdfRenderError,
    find_chromium,
    render_pdf,
    render_report_pdf,
)
from tests.reporting.conftest import report_with_both_answers

A4_POINTS = (595.0, 842.0)


def _fake_browser(directory: Path, script: str) -> Path:
    """A stand-in for Chromium that behaves badly in one specific way."""
    if os.name != "nt":
        path = directory / "fake-chrome"
        path.write_text("#!/bin/sh\n" + script)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path
    stub = directory / "fake-chrome.py"
    stub.write_text(
        textwrap.dedent(
            f"""\
            import sys, time
            script = {script!r}
            args = sys.argv[1:]
            if "sleep 30" in script:
                time.sleep(30)
                raise SystemExit(0)
            if "cannot open display" in script:
                sys.stderr.write("cannot open display\\n")
                raise SystemExit(3)
            if "%PDF-1.4" in script:
                for arg in args:
                    if arg.startswith("--print-to-pdf="):
                        open(arg.split("=", 1)[1], "w", encoding="utf-8").write("%PDF-1.4")
                raise SystemExit(0)
            raise SystemExit(0)
            """
        )
    )
    cmd = directory / "fake-chrome.cmd"
    cmd.write_text(f'@echo off\r\n"{sys.executable}" "{stub}" %*\r\n', encoding="utf-8")
    return cmd


def test_no_browser_at_all_is_a_clear_error_naming_where_it_looked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'chromium not found' sends a reader hunting a bug; a path list does not."""
    monkeypatch.delenv(CHROMIUM_ENV, raising=False)
    monkeypatch.setattr("cipherchain.reporting.pdf.PLAYWRIGHT_ROOT", tmp_path / "nothing-here")
    monkeypatch.setattr("cipherchain.reporting.pdf._playwright_roots", lambda: [tmp_path / "nothing-here"])
    monkeypatch.setattr("cipherchain.reporting.pdf._installed_browser_candidates", lambda: [])
    monkeypatch.setattr("cipherchain.reporting.pdf.shutil.which", lambda _: None)
    assert find_chromium() is None

    destination = tmp_path / "report.pdf"
    with pytest.raises(ChromiumNotFound) as raised:
        render_pdf("<html></html>", destination)
    assert CHROMIUM_ENV in str(raised.value)
    assert "HTML report is unaffected" in str(raised.value)
    assert not destination.exists()


def test_the_newest_playwright_build_is_the_one_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Build ids are compared as numbers.

    Sorted as text, 'chromium-982' beats 'chromium-1234' and the renderer
    quietly downgrades itself every time the build number gains a digit.
    """
    monkeypatch.delenv(CHROMIUM_ENV, raising=False)
    monkeypatch.setattr("cipherchain.reporting.pdf.PLAYWRIGHT_ROOT", tmp_path)
    for build in ("982", "1234"):
        binary = tmp_path / f"chromium-{build}" / "chrome-linux64" / "chrome"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\n")
    assert find_chromium() == tmp_path / "chromium-1234" / "chrome-linux64" / "chrome"


def test_a_windows_playwright_layout_is_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(CHROMIUM_ENV, raising=False)
    monkeypatch.setattr("cipherchain.reporting.pdf.PLAYWRIGHT_ROOT", tmp_path)
    monkeypatch.setattr("cipherchain.reporting.pdf._playwright_roots", lambda: [tmp_path])
    monkeypatch.setattr("cipherchain.reporting.pdf._installed_browser_candidates", lambda: [])
    monkeypatch.setattr("cipherchain.reporting.pdf.shutil.which", lambda _: None)
    binary = tmp_path / "chromium-2000" / "chrome-win64" / "chrome.exe"
    binary.parent.mkdir(parents=True)
    binary.write_text("x")
    assert find_chromium() == binary


def test_a_system_chrome_install_is_found_when_playwright_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chrome = tmp_path / "chrome.exe"
    chrome.write_text("x")
    monkeypatch.delenv(CHROMIUM_ENV, raising=False)
    monkeypatch.setattr("cipherchain.reporting.pdf._playwright_roots", lambda: [tmp_path / "no-pw"])
    monkeypatch.setattr("cipherchain.reporting.pdf.PLAYWRIGHT_ROOT", tmp_path / "no-pw")
    monkeypatch.setattr("cipherchain.reporting.pdf.shutil.which", lambda _: None)
    monkeypatch.setattr(
        "cipherchain.reporting.pdf._installed_browser_candidates", lambda: [chrome]
    )
    assert find_chromium() == chrome


def test_a_browser_that_writes_no_file_never_leaves_a_report_behind(tmp_path: Path) -> None:
    browser = _fake_browser(tmp_path, "exit 0\n")
    destination = tmp_path / "report.pdf"
    with pytest.raises(PdfRenderError, match="wrote no PDF"):
        render_pdf("<html></html>", destination, chromium=browser)
    assert not destination.exists()


def test_a_truncated_pdf_is_refused_rather_than_written(tmp_path: Path) -> None:
    """The dangerous failure: a file that opens, and is empty.

    Chromium exits 0 more often than it should, so the bytes are checked and the
    destination is only written after they pass.
    """
    browser = _fake_browser(
        tmp_path,
        'for arg in "$@"; do case "$arg" in --print-to-pdf=*) printf "%%PDF-1.4" '
        '> "${arg#*=}";; esac; done\nexit 0\n',
    )
    destination = tmp_path / "report.pdf"
    with pytest.raises(PdfRenderError, match="unusable PDF"):
        render_pdf("<html></html>", destination, chromium=browser)
    assert not destination.exists()


def test_a_browser_that_fails_reports_what_it_said(tmp_path: Path) -> None:
    browser = _fake_browser(tmp_path, 'echo "cannot open display" >&2\nexit 3\n')
    destination = tmp_path / "report.pdf"
    with pytest.raises(PdfRenderError, match="cannot open display"):
        render_pdf("<html></html>", destination, chromium=browser)
    assert not destination.exists()


def test_a_browser_that_hangs_is_given_up_on(tmp_path: Path) -> None:
    """An investigation endpoint cannot wait on a wedged browser forever."""
    browser = _fake_browser(tmp_path, "sleep 30\n")
    destination = tmp_path / "report.pdf"
    with pytest.raises(PdfRenderError, match="did not finish printing"):
        render_pdf("<html></html>", destination, chromium=browser, timeout=1.0)
    assert not destination.exists()


def test_the_html_path_does_not_depend_on_a_browser_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Losing the PDF renderer must cost the PDF and nothing else."""
    monkeypatch.delenv(CHROMIUM_ENV, raising=False)
    monkeypatch.setattr("cipherchain.reporting.pdf.PLAYWRIGHT_ROOT", tmp_path / "nothing-here")
    monkeypatch.setattr("cipherchain.reporting.pdf._playwright_roots", lambda: [tmp_path / "nothing-here"])
    monkeypatch.setattr("cipherchain.reporting.pdf._installed_browser_candidates", lambda: [])
    monkeypatch.setattr("cipherchain.reporting.pdf.shutil.which", lambda _: None)
    html = render_html(report_with_both_answers())
    assert "Coverage and caveats" in html


@pytest.mark.skipif(find_chromium() is None, reason="no headless Chromium on this machine")
def test_a_real_browser_prints_an_a4_pdf(tmp_path: Path) -> None:
    """End to end, on the browser this repo already renders documents with."""
    destination = tmp_path / "nested" / "report.pdf"
    written = render_report_pdf(report_with_both_answers(), destination)

    assert written == destination and destination.is_file()
    payload = destination.read_bytes()
    assert payload.startswith(b"%PDF")
    assert len(payload) > 1024

    boxes = re.findall(rb"MediaBox\s*\[\s*0\s+0\s+([\d.]+)\s+([\d.]+)", payload)
    assert boxes, "no page box in the produced PDF"
    width, height = (float(v) for v in boxes[0])
    assert abs(width - A4_POINTS[0]) < 2 and abs(height - A4_POINTS[1]) < 2

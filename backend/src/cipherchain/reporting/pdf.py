"""HTML to PDF through headless Chromium, with no quiet failures.

The renderer is a browser because the document is already a browser document:
one engine lays out both formats, so what an investigator reads on screen and
what lands in the case file cannot diverge. The alternative — a second layout
engine for print — is two implementations of the same evidence table, and the
one nobody looks at is the one that ships wrong.

Everything else here exists because of one requirement: a PDF that is missing,
truncated or empty must be an ERROR, never a file. A zero-byte report attached
to a case record is worse than no report, because it looks like the document was
produced and read. So Chromium writes to a temporary path, the bytes are checked
for the ``%PDF`` header, and only then does the file move to its destination —
an interrupted render leaves nothing behind rather than a plausible-looking
stub. If Chromium is absent, ``ChromiumNotFound`` says which paths were searched
instead of failing further down as an unreadable output file.

The HTML path never depends on any of this. A build with no browser still
produces the full report; it just cannot print it.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from cipherchain.core.errors import CipherChainError
from cipherchain.reporting.html import render_html
from cipherchain.reporting.model import InvestigationReport

logger = logging.getLogger(__name__)

# Where Playwright puts the browser this repo already renders documents with.
PLAYWRIGHT_GLOB = "chromium-*/chrome-linux64/chrome"
PLAYWRIGHT_ROOT = Path.home() / ".cache" / "ms-playwright"

# Set to a browser binary to override discovery entirely.
CHROMIUM_ENV = "CIPHERCHAIN_CHROMIUM"

_ON_PATH = (
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "chrome",
)

_VERSION_DIGITS = re.compile(r"(\d+)")

# Chromium exits 0 on a failed print more often than it should, so the exit code
# is a first check and not the check. A real A4 report is tens of kilobytes; the
# floor only has to be high enough to reject a header-and-nothing file.
_MIN_PDF_BYTES = 1024


class PdfRenderError(CipherChainError):
    """A PDF could not be produced. The HTML rendering path is unaffected."""


class ChromiumNotFound(PdfRenderError):
    """No headless browser is installed, so nothing can print.

    Carries the searched locations: "chromium not found" sends a reader looking
    for a bug, while a list of paths sends them to install a browser.
    """

    def __init__(self, searched: Sequence[str]) -> None:
        super().__init__(
            "no headless Chromium was found, so the report cannot be rendered to PDF "
            "(the HTML report is unaffected). Searched: " + ", ".join(searched)
        )
        self.searched = tuple(searched)


def _playwright_candidates() -> list[Path]:
    """Playwright installs, newest build first.

    Sorted on the numeric build id rather than lexically — ``chromium-1234``
    sorts before ``chromium-982`` as text, which would pick an older browser
    whenever the build number gains a digit.
    """
    matches = list(PLAYWRIGHT_ROOT.glob(PLAYWRIGHT_GLOB))

    def build_id(path: Path) -> int:
        digits = _VERSION_DIGITS.findall(path.parts[-3])
        return int(digits[0]) if digits else 0

    return sorted(matches, key=build_id, reverse=True)


def find_chromium(explicit: str | Path | None = None) -> Path | None:
    """Locate a browser able to print, or None. Never raises."""
    if explicit:
        candidate = Path(explicit)
        return candidate if candidate.is_file() else None
    from_env = os.environ.get(CHROMIUM_ENV)
    if from_env:
        candidate = Path(from_env)
        return candidate if candidate.is_file() else None
    for candidate in _playwright_candidates():
        if candidate.is_file():
            return candidate
    for name in _ON_PATH:
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def _searched_locations() -> list[str]:
    return [
        f"${CHROMIUM_ENV}",
        str(PLAYWRIGHT_ROOT / PLAYWRIGHT_GLOB),
        f"PATH ({', '.join(_ON_PATH)})",
    ]


def render_pdf(
    html: str,
    destination: str | Path,
    *,
    chromium: str | Path | None = None,
    timeout: float = 120.0,
) -> Path:
    """Print an HTML document to A4 PDF at ``destination``.

    Raises ``ChromiumNotFound`` when no browser exists and ``PdfRenderError``
    when one exists but produced nothing usable. The destination file is only
    created on success, so a failed call can never be mistaken for a rendered
    report.

    A4 comes from the document's own ``@page`` rule, which Chromium honours;
    the default would be US Letter, and a report that reflows between the office
    that made it and the office that received it is a document nobody can cite a
    page of.
    """
    browser = find_chromium(chromium)
    if browser is None:
        raise ChromiumNotFound(_searched_locations())

    target = Path(destination).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    # Cleaned up by hand, tolerantly: a browser killed on timeout can still be
    # writing into this directory, and TemporaryDirectory would raise on the way
    # out — replacing "Chromium hung" with a confusing OSError from the cleanup.
    workspace = Path(tempfile.mkdtemp(prefix="cipherchain-report-"))
    try:
        source = workspace / "report.html"
        source.write_text(html, encoding="utf-8")
        produced = workspace / "report.pdf"
        command = [
            str(browser),
            "--headless",
            "--disable-gpu",
            # Chromium refuses to start as root without this, and a report
            # renderer that works locally and dies in a container is a bug
            # discovered at the worst possible moment.
            "--no-sandbox",
            # No --user-data-dir on purpose. Passing one made every print hang
            # until the timeout on the Playwright build this repo renders with
            # (chromium-1234): the browser never returns from profile setup and
            # a 120s wait ends in no PDF at all. Headless print already runs in a
            # throwaway profile — two concurrent renders were verified to both
            # succeed without the flag — so the flag bought nothing and cost
            # everything.
            # Otherwise every page carries Chromium's own header and the
            # file:// path of a temp directory, which is noise in a case file.
            "--no-pdf-header-footer",
            f"--print-to-pdf={produced}",
            source.as_uri(),
        ]
        try:
            result = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise PdfRenderError(
                f"Chromium did not finish printing within {timeout:.0f}s; no PDF was written"
            ) from exc
        except OSError as exc:
            raise PdfRenderError(f"could not run Chromium at {browser}: {exc}") from exc

        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", "replace").strip()[:500] or "no stderr output"
            raise PdfRenderError(f"Chromium exited {result.returncode} while printing: {detail}")
        if not produced.is_file():
            raise PdfRenderError("Chromium reported success but wrote no PDF file")
        payload = produced.read_bytes()
        if len(payload) < _MIN_PDF_BYTES or not payload.startswith(b"%PDF"):
            raise PdfRenderError(
                f"Chromium produced an unusable PDF ({len(payload)} bytes); refusing to write "
                f"it to {target}"
            )
        # Written whole, then moved: a reader can never open a half-flushed file.
        # Process-scoped scratch name, so two renders of the same report do not
        # stage over each other and hand one of them the other's bytes.
        staged = target.with_name(f".{target.name}.{os.getpid()}.part")
        staged.write_bytes(payload)
        staged.replace(target)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    logger.info("report PDF written to %s (%d bytes)", target, target.stat().st_size)
    return target


def render_report_pdf(
    report: InvestigationReport,
    destination: str | Path,
    *,
    chromium: str | Path | None = None,
    timeout: float = 120.0,
    now: datetime | None = None,
) -> Path:
    """Render a report straight to PDF — the same HTML, printed."""
    return render_pdf(render_html(report, now=now), destination, chromium=chromium, timeout=timeout)

"""Turning one investigation into a document somebody can act on.

CipherChain's conclusions were, until this package, only ever JSON on a wire. The
answer to "what do I hand the officer" was a screenshot of an API response, and
an evidence taxonomy that the whole engine is built to keep separate arrives at
a reader as four indistinguishable strings in a list.

What a report adds over the API response is judgement about presentation, and
all of it points one way — the reader must not be able to conclude more than the
evidence allows:

- where "nearest" and "nearest named" differ, both are shown AND the difference
  is explained, because one of them can be served with a legal request and the
  other cannot;
- a third-party claim always carries its date, so a fresh attribution and a
  three-year-stale one never look alike;
- coverage and caveats are a mandatory section that no code path can drop.

The API edge composes these three calls: ``collect_report`` to read the run,
``render_html`` for the document, ``render_report_pdf`` when a file is wanted.
The PDF path needs a headless browser and says so loudly when there is none; the
HTML path never depends on it.
"""

from __future__ import annotations

from cipherchain.reporting.collect import ReportNotFound, collect_coverage, collect_report
from cipherchain.reporting.html import render_html
from cipherchain.reporting.model import (
    AnswerEndpoint,
    AnswerSection,
    Caveat,
    DirectionVerdict,
    EvidenceGroup,
    InvestigationReport,
    ReportHeader,
    SummaryEndpoint,
    TraversalCoverage,
    VaspProfile,
    build_report,
    derive_caveats,
    group_evidence,
    summarise_answers,
)
from cipherchain.reporting.pdf import (
    ChromiumNotFound,
    PdfRenderError,
    find_chromium,
    render_pdf,
    render_report_pdf,
)
from cipherchain.reporting.vasp import VaspLookup, coerce_profile, default_vasp_lookup

__all__ = [
    "AnswerEndpoint",
    "AnswerSection",
    "Caveat",
    "ChromiumNotFound",
    "DirectionVerdict",
    "EvidenceGroup",
    "InvestigationReport",
    "PdfRenderError",
    "ReportHeader",
    "ReportNotFound",
    "SummaryEndpoint",
    "TraversalCoverage",
    "VaspLookup",
    "VaspProfile",
    "build_report",
    "coerce_profile",
    "collect_coverage",
    "collect_report",
    "default_vasp_lookup",
    "derive_caveats",
    "find_chromium",
    "group_evidence",
    "render_html",
    "render_pdf",
    "render_report_pdf",
    "summarise_answers",
]

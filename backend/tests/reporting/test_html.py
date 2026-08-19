"""The rendered document — what a reader actually holds.

The model decides what may be said; these check that none of it is lost on the
way to the page, including in the shapes where there is almost nothing to print.
"""

from __future__ import annotations

from html import unescape

import pytest

from cipherchain.core.models import Direction
from cipherchain.investigation.answers import RankedFinding
from cipherchain.reporting.html import render_html
from cipherchain.reporting.model import VaspProfile
from tests.reporting.conftest import (
    ALL_SHAPES,
    NOW,
    make_report,
    report_with_both_answers,
    report_with_both_answers_but_partial,
    report_with_caveats,
    report_with_only_a_lead,
    report_with_reference_data_offered_for_an_unnamed_endpoint,
    report_with_zero_findings,
    vasp_finding,
)


@pytest.mark.parametrize("shape", sorted(ALL_SHAPES), ids=lambda s: s.replace(" ", "_"))
def test_the_caveats_section_is_rendered_for_every_report_shape(shape: str) -> None:
    """No branch of the renderer may omit or collapse the coverage section.

    Including the empty shapes: a report with no findings and no answers is
    exactly where a "nothing to show" shortcut would drop it.
    """
    report = ALL_SHAPES[shape]()
    html = render_html(report)
    assert "Coverage and caveats" in html
    # Compared against the unescaped text: the assertion is about the caveat
    # reaching the page, not about how an apostrophe is spelled in markup.
    readable = unescape(html)
    for caveat in report.caveats:
        assert caveat.headline in readable, f"{shape} lost caveat {caveat.code}"
        assert caveat.detail in readable, f"{shape} lost the detail of {caveat.code}"


@pytest.mark.parametrize("shape", sorted(ALL_SHAPES), ids=lambda s: s.replace(" ", "_"))
def test_the_document_needs_nothing_from_the_network(shape: str) -> None:
    """It is emailed, filed and printed; anything fetched at open time is lost.

    A stylesheet that fails to load turns the evidence tables into undifferentiated
    text, where a heuristic inference looks exactly like a sourced claim.
    """
    html = render_html(ALL_SHAPES[shape]())
    for forbidden in ("http://", "https://", "<script", " src=", "@import", "url("):
        assert forbidden not in html, f"{shape} rendered an external dependency: {forbidden}"


def test_a_partial_run_is_visible_in_the_header_not_only_in_the_caveats() -> None:
    """A skimmer never reaches the last page, so the status is stated up front."""
    html = render_html(report_with_caveats())
    banner = html.index("coverage partial")
    assert banner < html.index("The answers")
    assert banner < html.index("Coverage and caveats")


def test_partial_says_whether_the_questions_were_actually_answered() -> None:
    """ "Partial" describes coverage, never the answers, and one sentence for
    both cases is what made it unreadable.

    The shipped case answered both directions with a real exchange and still
    ended `partial` on a budget. The reader saw the word "partial" and took it
    to mean no VASP was found. A run that stopped with nothing named needs the
    opposite warning, so the banner has to look at the answers before it speaks.
    """
    answered = render_html(report_with_both_answers_but_partial())
    assert "Answered · coverage partial" in answered
    assert "Every objective was answered with a named operator" in answered
    # and it must name them, so the reader never has to hunt for the payload
    assert "Binance" in answered[: answered.index("The answers")]

    nothing = render_html(report_with_caveats())  # partial, only an unnamed endpoint
    assert "Not answered · coverage partial" in nothing
    assert "No objective reached a named operator" in nothing
    assert "not a finding that no operator exists" in nothing


def test_the_named_operator_is_the_headline_not_the_footnote() -> None:
    """The name is what the report exists to deliver, so it prints first.

    Regression: the unnamed nearest endpoint printed first and pushed "Binance"
    onto page 2, so a run that answered both directions was read as having found
    no VASP at all. For a document handed to a regulator, the order of these two
    cards is the deliverable.
    """
    html = render_html(report_with_both_answers())
    named = html.index("Nearest NAMED endpoint")
    unnamed = html.index(">Nearest endpoint<")
    assert named < unnamed, "the named operator must come before the unnamed endpoint"
    # second is not suppressed — the unnamed endpoint is a true, different answer
    assert "Operator unnamed" in html


def test_a_named_and_an_unnamed_endpoint_are_never_rendered_alike() -> None:
    """The whole practical difference is whether anyone can be served a request."""
    html = render_html(report_with_both_answers())
    assert "Nearest endpoint" in html
    assert "Nearest NAMED endpoint" in html
    assert "Operator unnamed" in html
    assert "A legal request can be addressed to this operator." in html
    assert "this address is a lead, not a respondent" in html
    assert "Why two answers" in html


def test_every_third_party_claim_prints_its_date() -> None:
    """A fresh attribution and a three-year-stale one must never look alike."""
    html = render_html(report_with_both_answers())
    assert "dated 2026-08-10" in html
    assert "6 days old" in html


def test_an_undated_claim_says_so_where_the_date_would_be() -> None:
    """Silence in the date position reads as freshness, so it is never silent."""
    report = make_report(
        ranked=[RankedFinding(vasp_finding("0xbinance", named=True, claim_days_old=None), hop=2)]
    )
    html = render_html(report)
    assert "no source date recorded" in html


def test_operator_reference_data_is_printed_when_it_is_on_file() -> None:
    """Jurisdiction and request channel are what turn an answer into an action."""
    report = make_report(
        ranked=[RankedFinding(vasp_finding("0xbinance", named=True), hop=2)],
        profiles={
            "0xbinance": VaspProfile(
                entity="Binance",
                jurisdiction="Cayman Islands",
                legal_entity="Binance Holdings Ltd",
                le_request_channel="le.binance.com",
            )
        },
    )
    html = render_html(report)
    assert "Cayman Islands" in html
    assert "Binance Holdings Ltd" in html
    assert "le.binance.com" in html


def test_no_operator_reaches_an_unnamed_card_through_reference_data() -> None:
    """The page may not name who a card has just said nobody named.

    A metadata row exists at this address, and printing it would put "Operator
    reference data — Binance", a legal entity and a place to file directly under
    "Operator unnamed … this address is a lead, not a respondent". A reader
    takes the name and files against it.
    """
    html = render_html(report_with_reference_data_offered_for_an_unnamed_endpoint())

    assert "Operator unnamed" in html
    assert "this address is a lead, not a respondent" in html
    for leaked in (
        "Operator reference data",
        "Binance Holdings Ltd",
        "Cayman Islands",
        "le.binance.com",
    ):
        assert leaked not in html, f"an unnamed endpoint was handed {leaked!r}"


def test_missing_reference_data_prints_nothing_rather_than_empty_cells() -> None:
    """A blank 'jurisdiction' cell reads as 'there isn't one', which is a lie."""
    html = render_html(report_with_both_answers())
    assert "Law-enforcement request channel" not in html


def test_the_four_evidence_kinds_are_explained_where_they_appear() -> None:
    """A reader who cannot tell a guess from a sourced claim will read one as the other."""
    html = render_html(report_with_caveats())
    assert "On-chain facts" in html
    assert "Heuristic inferences" in html
    assert "it never names who runs it" in html
    assert "Engine observations" in html


def test_a_run_that_concluded_nothing_still_renders_a_whole_document() -> None:
    """Zero findings is a legitimate outcome, not an error page."""
    html = render_html(report_with_zero_findings())
    assert "The answers" in html
    assert "No endpoint reached" in html
    assert "Coverage and caveats" in html
    assert html.rstrip().endswith("</html>")


def test_hostile_label_text_cannot_escape_into_the_markup() -> None:
    """Label data is third-party input and lands in a document sent to others."""
    report = make_report(
        ranked=[
            RankedFinding(
                vasp_finding("0xevil", named=True, entity="<script>alert(1)</script>"), hop=1
            )
        ]
    )
    html = render_html(report)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_the_page_is_set_up_for_a4_print() -> None:
    """A report that reflows between the office that made it and the one that
    received it cannot have a page cited."""
    html = render_html(report_with_both_answers())
    assert "@page { size: A4;" in html


def test_the_colophon_carries_the_versions_a_rerun_would_need() -> None:
    """A conclusion is only reproducible against the engine that produced it.

    These printed twice — masthead and colophon — until the verdict block needed
    the top of page 1. They are provenance, not the answer, so they are stated
    once, at the end, under the same labels they always had.
    """
    html = render_html(report_with_both_answers())
    colophon = html[html.index('class="colophon"') :]
    for expected in ("Engine version", "0.1.0", "Ruleset version", "baseline-2026-08-07"):
        assert expected in colophon
    assert "Investigation id" in colophon and "Budgets" in colophon


def test_the_run_is_dated_up_front_and_the_printing_is_dated_at_the_end() -> None:
    """Read months later, the document still has to date the trace itself.

    The dates answer different questions and sit where each is asked: when the
    chain was read qualifies the verdict directly above it, while when this file
    was printed is something a reader checks against a case record.
    """
    html = render_html(report_with_both_answers())
    masthead = html[: html.index('id="summary"')]
    colophon = html[html.index('class="colophon"') :]

    assert "Run started" in masthead and "2026-08-16 11:48 UTC" in masthead
    assert "Report generated" in colophon and "2026-08-16 12:00 UTC" in colophon
    assert "Run last updated" in colophon


def test_claim_age_is_measured_against_the_moment_the_report_was_built() -> None:
    """Re-printing a stored report must not age its claims by the delay."""
    report = report_with_both_answers()
    assert report.header.generated_at == NOW
    forward = next(a for a in report.answers if a.direction is Direction.FORWARD)
    assert forward.nearest_named is not None
    assert "6 days old" in render_html(report)


def test_a_lead_reaches_the_page_with_its_weakness_on_the_card() -> None:
    """The caveat travels with the endpoint, not in a footnote.

    A reader who photocopies the answers page into a filing must not be able to
    leave the qualifier behind, so the weakness is printed inside the card and
    the heading itself says it is not a traced result.
    """
    html = unescape(render_html(report_with_only_a_lead()))

    assert "Best available lead — not a traced result" in html
    assert "selected by the heuristic mixer-exit-anonymity-set@1" in html, (
        "the rule the branch rests on never reached the page"
    )
    assert "Path unverified" in html


def test_the_lead_page_still_says_nothing_was_traced() -> None:
    """The honest empty answer is printed UNDER the lead, not replaced by it.

    The lead answers "what have you got"; it does not answer the objective, and
    a page that shows only the lead has quietly upgraded it.
    """
    html = unescape(render_html(report_with_only_a_lead()))

    assert "Traced endpoint" in html
    assert "No endpoint was reached in this direction by following value alone" in html


def test_a_lead_is_never_rendered_under_a_nearest_heading() -> None:
    """The exact misread this whole path exists to prevent."""
    html = unescape(render_html(report_with_only_a_lead()))

    assert "Nearest endpoint" not in html
    assert "Nearest NAMED endpoint" not in html

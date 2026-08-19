"""The block a reader sees first: who the money came from, who it went to.

Everything the document knows was already in it before this block existed — and
a reader still had to hold two headings, two cards and a divergence note in
their head to answer "which exchange do I write to". These tests are about the
one thing that block may never do, which is to be wrong in the reader's favour:
naming an operator the evidence does not name, or shortening an address into
something that cannot be quoted.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Iterator
from datetime import timedelta
from html import unescape

import pytest

from cipherchain.core.models import (
    Address,
    Direction,
    Evidence,
    EvidenceKind,
    Finding,
    FindingKind,
)
from cipherchain.investigation.answers import RankedFinding
from cipherchain.reporting.html import render_html
from cipherchain.reporting.model import (
    LEAD_BEHAVIOURAL,
    LEAD_UNTRACED,
    InvestigationReport,
    VaspProfile,
    summarise_answers,
)
from cipherchain.reporting.pdf import find_chromium, render_report_pdf
from tests.reporting.conftest import (
    ALL_SHAPES,
    CHAIN,
    NOW,
    OKX_ADDRESS,
    ONCHAIN,
    SANCTIONS_CLAIM_SUMMARY,
    TRON,
    UNNAMED_SERVICE_ADDRESS,
    claim,
    endpoint_claimed_but_unnamed,
    make_report,
    report_clean_run,
    report_like_the_shipped_tron_run,
    report_with_a_claim_that_names_nobody,
    report_with_both_answers,
    report_with_caveats,
    report_with_no_answers,
    report_with_one_answer,
    report_with_only_a_lead,
    report_with_reference_data_offered_for_an_unnamed_endpoint,
    vasp_finding,
)

#: Where the summary block ends and the rest of the document begins. Assertions
#: about the summary have to be made against the summary: "Binance appears in
#: the report" was already true before this block existed and stays true if it
#: renders empty.
_ANSWERS = 'id="answers"'

#: The one sentence in this document that tells a reader they have somebody to
#: serve. It is asserted on by exact text everywhere below, because a renderer
#: that reworded it while keeping the promise would pass a looser test.
_LEGAL_REQUEST = "A legal request can be addressed to this operator."


def summary_of(report: InvestigationReport) -> str:
    html = render_html(report)
    start = html.index('id="summary"')
    return unescape(html[start : html.index(_ANSWERS)])


def summary_text(report: InvestigationReport) -> str:
    """The summary as a reader reads it — markup stripped, entities resolved."""
    return re.sub(r"<[^>]+>", " ", summary_of(report))


def test_a_named_operator_in_both_directions_is_stated_in_full() -> None:
    """The deliverable: two names, two addresses, in the first block.

    Not "somewhere in the report" — the whole point of the block is that a
    reader who reads nothing else comes away with the two names and the two
    addresses to quote in a request.
    """
    report = report_clean_run()
    block = summary_of(report)

    assert "Funds came IN from" in block and "Funds went OUT to" in block
    assert "Binance" in block and "Kraken" in block
    assert "0xbinance" in block and "0xkraken" in block
    # distance, confidence and what the name rests on, per direction
    assert block.count("hop(s) from the subject") == 2
    assert block.count("confidence 90%") == 2
    assert block.count("The name rests on: third-party claim from etherscan-tags") == 2
    assert block.count("A legal request can be addressed to this operator.") == 2


def test_one_named_direction_says_so_and_says_the_other_is_unanswered() -> None:
    """A strong answer in one direction never implies anything about the other."""
    block = summary_of(report_with_one_answer())

    money_in = block[block.index("Money in") : block.index("Money out")]
    money_out = block[block.index("Money out") :]

    assert "Binance" in money_in and "0xbinance" in money_in
    assert "No operator can be named" in money_out
    assert "Binance" not in money_out, "a name leaked across into the unanswered direction"


def test_no_named_operator_anywhere_names_nobody_anywhere() -> None:
    """The report is allowed to answer nothing. It is not allowed to imply an answer."""
    for report in (report_with_no_answers(), report_with_caveats()):
        block = summary_of(report)
        assert block.count("No operator can be named") == 2
        assert "A legal request can be addressed to this operator." not in block
        assert "The name rests on:" not in block


def test_the_shipped_run_reads_the_way_it_actually_resolved() -> None:
    """OKX in, nobody out — the run this block is judged against.

    Both shapes in one document: a named operator with an address a regulator
    can act on, and an honest "no operator can be named" that does not dress the
    behavioural endpoint the trace did reach up as a VASP.
    """
    block = summary_of(report_like_the_shipped_tron_run())
    money_in = block[block.index("Money in") : block.index("Money out")]
    money_out = block[block.index("Money out") :]

    assert "OKX" in money_in
    assert OKX_ADDRESS in money_in
    assert "2 hop(s) from the subject" in money_in
    assert "confidence 90%" in money_in
    assert "third-party claim from okx-proof-of-reserves" in money_in
    assert "11 days old" in money_in, "the age of the claim the name rests on"

    assert "No operator can be named" in money_out
    assert UNNAMED_SERVICE_ADDRESS in money_out
    assert "Lead — an inference, not an attribution" in money_out
    assert "never who runs it" in money_out
    assert "OKX" not in money_out, "the backward answer leaked into the forward direction"


@pytest.mark.parametrize("shape", sorted(ALL_SHAPES), ids=lambda s: s.replace(" ", "_"))
def test_an_endpoint_no_claim_names_never_reaches_the_summary_with_a_name(shape: str) -> None:
    """Behaviour alone produces "operator unnamed", in every shape, always.

    Read off the model rather than the markup because this is the taxonomy rule
    itself and not a rendering detail: a summary endpoint carries an operator
    only where a third-party claim named one, so no arrangement of findings —
    or of reference data sitting at the address — can produce a named VASP from
    an inference.
    """
    report = ALL_SHAPES[shape]()
    verdicts = summarise_answers(report.answers, NOW)
    for section, verdict in zip(report.answers, verdicts, strict=True):
        if verdict.named is not None and verdict.named.operator is not None:
            assert section.nearest_named is not None
            assert section.nearest_named.named, f"{shape} named an endpoint with no claim"
        if verdict.lead is not None and verdict.lead.operator is not None:
            behind = section.best_effort or section.nearest
            assert behind is not None and behind.named, (
                f"{shape} named the lead {verdict.lead.operator} with no claim behind it"
            )


def test_reference_data_at_an_unnamed_address_stays_out_of_the_summary() -> None:
    """The metadata table knows an operator. The evidence does not.

    Profiles are keyed by address, so an address-indexed lookup will happily
    answer for an endpoint that rests on nothing but behaviour. In the summary
    that answer would be the headline — "Funds came IN from Binance" over an
    address no source has ever attributed to anybody.
    """
    block = summary_of(report_with_reference_data_offered_for_an_unnamed_endpoint())

    assert "No operator can be named" in block
    assert "Lead — an inference, not an attribution" in block
    for leaked in ("Binance", "Binance Holdings Ltd", "Cayman Islands", "le.binance.com"):
        assert leaked not in block, f"an unnamed endpoint was handed {leaked!r} in the summary"


def test_a_named_lead_past_a_mixer_is_not_stated_as_the_counterparty() -> None:
    """The name is real and the path to it is a guess, so both are said.

    This is the shape that would be easiest to over-claim: a sourced label sits
    at the end of the route, and printing it as the answer would assert a path
    this run selected rather than followed.
    """
    block = summary_of(report_with_only_a_lead())

    assert "No operator can be named" in block
    assert "Lead — the route to it was not traced" in block
    # the name is still shown, because a lead nobody can act on is not a lead
    assert "Binance" in block
    assert "0xpastmixer" in block
    assert "the route crosses a mixer" in block
    assert "A legal request can be addressed to this operator." not in block


# ── a claim of the right KIND is not a name ──────────────────────────────────


def _one_direction(finding: Finding) -> InvestigationReport:
    """One endpoint, one direction, so a count of one is a count of one."""
    return make_report(ranked=[RankedFinding(finding, hop=2)], directions=(Direction.BACKWARD,))


#: The three ways a direction can end, differing in exactly one thing: whether
#: an operator NAME could be read off the evidence. The middle one is the shape
#: that was missing — a legitimate third_party_claim about the ADDRESS ("mixer
#: 'Tornado Cash'") satisfies ``is_named``, so the document treated it as an
#: attribution and offered a respondent it could not name.
_ATTRIBUTION_SHAPES = {
    "a named operator": lambda: _one_direction(vasp_finding("0xbinance", named=True)),
    "a claim naming nobody": lambda: _one_direction(endpoint_claimed_but_unnamed()),
    "no claim at all": lambda: _one_direction(
        vasp_finding("0xguess", named=False, confidence=0.61)
    ),
}


@pytest.mark.parametrize("shape", sorted(_ATTRIBUTION_SHAPES), ids=lambda s: s.replace(" ", "_"))
def test_the_legal_request_line_is_printed_only_where_an_operator_is_named(shape: str) -> None:
    """The most damaging sentence this report can print, and when it may print.

    "A legal request can be addressed to this operator" sends an investigator to
    a respondent. On the page that gets photocopied into a filing on its own, it
    once printed over a blank name — an instruction to serve a request on
    nobody — because the card asked whether a third-party claim existed and
    never whether it named anybody.
    """
    block = summary_of(_ATTRIBUTION_SHAPES[shape]())
    expected = 1 if shape == "a named operator" else 0

    assert block.count(_LEGAL_REQUEST) == expected, (
        f"{shape} printed the actionability line {block.count(_LEGAL_REQUEST)} time(s)"
    )


def test_a_claim_that_names_nobody_says_so_and_still_shows_the_claim() -> None:
    """Refusing to over-claim may not become refusing to report.

    That an endpoint is a sanctioned mixer is one of the most important things
    this document can say, so the address, the source and the age of the claim
    all stay on the page. What goes is the pretence that any of it identifies a
    party — including the wording, since "the name rests on" is false where
    there is no name.
    """
    report = report_with_a_claim_that_names_nobody()
    block = summary_of(report)

    assert _LEGAL_REQUEST not in block
    assert "No operator can be named" in block
    assert "Named on a source that records no operator name" not in block, (
        "a placeholder where the operator goes reads as a naming that lost its name"
    )
    assert "The name rests on:" not in block

    assert "0xnameless" in block
    # The summary says WHAT was found and where the detail lives, rather than
    # reprinting the claim's provenance beside a name that does not exist.
    # (The source and date used to appear here only because `named` was wrongly
    # true, which put this endpoint through the naming branch.)
    assert "rather than naming an operator" in block
    assert "no company or service is identified" in block
    assert "set out in full in the evidence below" in block
    assert SANCTIONS_CLAIM_SUMMARY in unescape(render_html(report)), (
        "the claim itself was dropped from the evidence rather than merely un-promoted"
    )


def test_the_model_refuses_to_call_an_endpoint_with_no_name_an_answer() -> None:
    """Decided once, in the model, so no renderer has to remember the rule.

    ``named`` stays true — the claim is real evidence and the report keeps
    showing it — while everything that decides whether a reader has somebody to
    write to now turns on there being an extracted operator.
    """
    report = report_with_a_claim_that_names_nobody()
    section = report.answers[0]
    verdict = summarise_answers(report.answers, NOW)[0]

    assert section.nearest_named is not None
    # `named` is now strict — it means an operator came out of the claim — so
    # the endpoint is selected for this slot by carrying a claim at all, and
    # every actionability decision still turns on there being a name.
    assert section.nearest_named.named is False
    assert section.nearest_named.carries_a_claim, "the third-party claim is still recorded"
    assert section.nearest_named.entity is None
    assert section.nearest_named.names_an_operator is False
    assert section.actionable is False
    assert verdict.named is not None and verdict.named.operator is None
    assert verdict.answered is False

    caveat = report.caveat("claim_names_no_operator_backward")
    assert caveat is not None, "the direction ended with no respondent and said nothing about it"
    assert "does not supply a respondent" in caveat.detail


def test_the_answers_section_does_not_badge_a_nameless_claim_as_a_named_operator() -> None:
    """Page 1 and page 2 have to say the same thing about the same endpoint."""
    html = unescape(render_html(report_with_a_claim_that_names_nobody()))
    answers = html[html.index(_ANSWERS) : html.index('id="coverage"')]

    assert ">Named operator<" not in answers
    assert "Nearest NAMED endpoint" not in answers, "a heading claiming what the badge denies"
    assert _LEGAL_REQUEST not in answers
    assert "Operator not named" in answers
    assert SANCTIONS_CLAIM_SUMMARY in answers, "the claim is evidence and belongs on the page"


def test_the_divergence_note_does_not_promise_an_operator_the_further_endpoint_lacks() -> None:
    """Two rows, and the note explaining them made the same promise.

    "Why two answers" exists to tell a reader which of the two addresses can be
    served. Written for the ordinary case it asserts the further one "is
    attributed to an operator on a citable source, which is the one a legal
    request can be addressed to" — of an endpoint whose claim named nobody.
    """
    report = make_report(
        ranked=[
            RankedFinding(vasp_finding("0xguess", named=False, confidence=0.61), hop=1),
            RankedFinding(endpoint_claimed_but_unnamed("0xnameless"), hop=3),
        ],
        directions=(Direction.BACKWARD,),
    )
    section = report.answers[0]
    divergence = section.divergence or ""

    assert section.nearest is not None and section.nearest.address == "0xguess"
    assert section.nearest_named is not None and section.nearest_named.address == "0xnameless"
    assert "is attributed to an operator" not in divergence
    assert "neither one names an operator" in divergence
    assert "nobody on this route to address a legal request to" in divergence
    assert _LEGAL_REQUEST not in unescape(render_html(report))


def test_the_status_banner_does_not_count_a_nameless_claim_as_a_named_operator() -> None:
    """The banner is read by the skimmer who never reaches the summary block.

    It counted endpoints in the ``nearest_named`` slot, so this shape produced
    "Every objective was answered with a named operator" above a list of names
    that was empty because there were none.
    """
    html = render_html(report_with_a_claim_that_names_nobody())

    assert "Every objective was answered with a named operator" not in html
    assert "Not answered · coverage partial" in html
    assert "No objective reached a named operator" in html


def test_the_unnamed_endpoint_beside_a_named_one_is_marked_as_the_nearer_address() -> None:
    """Second is not suppressed, and it is not promoted either."""
    block = summary_of(report_with_both_answers())

    assert "Binance" in block
    assert "A nearer address on this route — operator unnamed" in block
    assert "0xguess" in block
    assert block.index("Binance") < block.index("0xguess"), "the name must come first"


@pytest.mark.parametrize("shape", sorted(ALL_SHAPES), ids=lambda s: s.replace(" ", "_"))
def test_the_summary_never_contradicts_the_status_banner(shape: str) -> None:
    """A summary naming an operator over a banner reading "Not answered" is a bug.

    Both are derived from ``nearest_named``, so this asserts that neither
    renderer has grown its own opinion about what counts as answered.
    """
    report = ALL_SHAPES[shape]()
    html = render_html(report)
    verdicts = summarise_answers(report.answers, NOW)
    answered = [v for v in verdicts if v.answered]

    if "Not answered · coverage partial" in html:
        assert not answered, f"{shape} banner says nothing was named, summary names one"
    if "Answered · coverage partial" in html and "Partly answered" not in html:
        assert answered and len(answered) == len(verdicts)
    if "Partly answered · coverage partial" in html:
        assert answered and len(answered) < len(verdicts)


@pytest.mark.parametrize("shape", sorted(ALL_SHAPES), ids=lambda s: s.replace(" ", "_"))
def test_every_address_in_the_summary_is_printed_whole(shape: str) -> None:
    """An address a reader has to retype reaches the exchange with a typo in it.

    Checked against the addresses the model holds rather than against a regex
    for an ellipsis, so a renderer that shortened by slicing would fail this
    too.
    """
    report = ALL_SHAPES[shape]()
    block = summary_of(report)
    for verdict in summarise_answers(report.answers, NOW):
        for endpoint in (verdict.named, verdict.lead):
            if endpoint is None:
                continue
            assert endpoint.address in block, f"{shape} lost {endpoint.address}"
    assert "…" not in block and "..." not in block


def test_the_full_address_is_selectable_as_one_string() -> None:
    """Copied out of a PDF in one gesture, not reassembled by hand."""
    html = render_html(report_like_the_shipped_tron_run())
    assert ".address" in html and "user-select: all" in html
    assert f'<p class="address">{OKX_ADDRESS}</p>' in html


def test_the_summary_is_the_first_thing_after_the_masthead() -> None:
    """Below the answers it would be a recap; a recap is not what was asked for."""
    html = render_html(report_like_the_shipped_tron_run())
    summary = html.index('id="summary"')

    assert html.index('class="masthead"') < summary
    for later in (_ANSWERS, 'id="coverage"', 'id="findings"', 'class="colophon"'):
        assert summary < html.index(later), f"{later} rendered before the summary"


@pytest.mark.parametrize("shape", sorted(ALL_SHAPES), ids=lambda s: s.replace(" ", "_"))
def test_nothing_required_was_traded_for_the_summary(shape: str) -> None:
    """Condensing the document may not cost it anything a reader must weigh.

    The three things a reader needs to check the claim rather than believe it:
    every caveat, the coverage figures, and the provenance to re-run it. All of
    them are still rendered, in full, in every shape.
    """
    report = ALL_SHAPES[shape]()
    html = unescape(render_html(report))

    assert "Coverage and caveats" in html
    assert "Addresses reached" in html
    for caveat in report.caveats:
        assert caveat.headline in html and caveat.detail in html
    for provenance in ("Engine version", "Ruleset version", "Investigation id", "Budgets"):
        assert provenance in html, f"{shape} dropped {provenance}"
    assert report.header.engine_version in html
    assert report.header.investigation_id in html


@pytest.mark.parametrize("shape", sorted(ALL_SHAPES), ids=lambda s: s.replace(" ", "_"))
def test_folded_evidence_is_still_printed_evidence(shape: str) -> None:
    """A collapsed ``details`` is omitted from print output.

    So every evidence block is emitted open. Emitting a closed one would produce
    a PDF whose evidence tables are simply absent while the HTML looked complete
    — the one failure this document may not have.
    """
    html = render_html(ALL_SHAPES[shape]())
    assert "<details" not in html.replace('<details class="evidence" open>', "")
    for group in (
        group
        for section in ALL_SHAPES[shape]().answers
        for endpoint in (section.nearest, section.nearest_named, section.best_effort)
        if endpoint is not None
        for group in endpoint.evidence_groups
    ):
        assert group.caption in unescape(html)
        for item in group.items:
            assert item.summary in unescape(html)


def test_a_lead_and_a_nearer_unnamed_address_are_told_apart() -> None:
    """Two different weaknesses; one wording for both would hide one of them."""
    traced = summarise_answers(report_with_caveats().answers, NOW)
    assert traced[0].lead_kind == LEAD_BEHAVIOURAL
    assert traced[0].lead is not None and traced[0].lead.operator is None

    selected = summarise_answers(report_with_only_a_lead().answers, NOW)
    assert selected[0].lead_kind == LEAD_UNTRACED
    assert selected[0].named is None, "a lead is never an answer, however well sourced"
    assert selected[0].lead is not None and selected[0].lead.operator == "Binance"


def test_a_stale_claim_is_flagged_where_the_name_is_stated() -> None:
    """A three-year-old attribution and this morning's must not read alike."""
    report = make_report(
        ranked=[RankedFinding(vasp_finding("0xbinance", named=True, claim_days_old=900), hop=2)],
    )
    verdict = summarise_answers(report.answers, NOW)[0]

    assert verdict.named is not None and verdict.named.stale
    assert 'class="meta-line flag"' in summary_of(report)
    assert "900 days old" in summary_text(report)


def test_the_age_printed_is_the_age_of_the_claim_the_NAME_came_from() -> None:
    """One finding, two claims, and only one of them names anybody.

    A sanctions listing and a VASP label sit on the same endpoint routinely, and
    the listing names nobody. Quoting whichever claim came first would date a
    three-year-old attribution by a listing added last week, so the reader would
    weigh the name by the freshness of a source that never mentioned it — and
    the stale flag, which is the only warning this block gives, would be off.
    """
    finding = Finding(
        kind=FindingKind.VASP_ENDPOINT,
        subject=Address(chain=CHAIN, value="0xtwoclaims"),
        summary="nearest previous VASP: Binance",
        confidence=0.9,
        direction=Direction.BACKWARD,
        evidence=(
            ONCHAIN,
            Evidence(
                kind=EvidenceKind.THIRD_PARTY_CLAIM,
                summary="address identified as mixer 'Tornado Cash'",
                source="ofac-sdn",
                source_date=NOW - timedelta(days=2),
                confidence=0.95,
            ),
            claim("Binance", days_old=900, source="etherscan-tags"),
        ),
    )
    report = make_report(ranked=[RankedFinding(finding, hop=2)], directions=(Direction.BACKWARD,))
    verdict = summarise_answers(report.answers, NOW)[0]

    assert verdict.named is not None and verdict.named.operator == "Binance"
    assert verdict.named.basis is not None
    assert "etherscan-tags" in verdict.named.basis, "dated against a claim that names nobody"
    assert "900 days old" in verdict.named.basis
    assert verdict.named.stale, "a three-year-old attribution presented as current"
    assert "ofac-sdn" not in summary_text(report)


def test_a_hostile_operator_name_cannot_escape_into_the_summary() -> None:
    """Label data is third-party input, and this block is the loudest thing on the page."""
    report = make_report(
        ranked=[
            RankedFinding(
                vasp_finding("0xevil", named=True, entity="<script>alert(1)</script>"), hop=1
            )
        ]
    )
    html = render_html(report)
    start = html.index('id="summary"')
    block = html[start : html.index(_ANSWERS)]

    assert "<script>alert(1)</script>" not in block
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in block


def test_a_report_with_no_objective_still_states_that_plainly() -> None:
    """An empty summary reads as "we looked and found nothing", which is a lie."""
    block = summary_of(make_report(directions=()))

    assert "No objective was recorded" in block
    assert "no direction to answer" in block


def test_the_summary_needs_nothing_from_the_network() -> None:
    """It is the part most likely to be photocopied and emailed on its own."""
    block = summary_of(report_like_the_shipped_tron_run())
    for forbidden in ("http://", "https://", "<script", " src=", "@import", "url("):
        assert forbidden not in block


# ── the printed document, on the browser this repo renders with ──────────────

_HAS_TEXT_EXTRACTOR = shutil.which("pdftotext") is not None
needs_printing = pytest.mark.skipif(
    find_chromium() is None or not _HAS_TEXT_EXTRACTOR,
    reason="needs headless chromium and pdftotext to read the printed document back",
)


@pytest.fixture(scope="module")
def printed_tron_report(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Printed once for the module: a browser render costs about a second."""
    destination = render_report_pdf(
        report_like_the_shipped_tron_run(), tmp_path_factory.mktemp("printed") / "report.pdf"
    )
    extracted = subprocess.run(
        ["pdftotext", "-layout", str(destination), "-"],
        capture_output=True,
        check=True,
        text=True,
    )
    yield extracted.stdout


@needs_printing
def test_the_printed_document_leads_with_the_operator_and_the_whole_address(
    printed_tron_report: str,
) -> None:
    """The PDF is the artefact that reaches the case file, so it is read back.

    Chromium lays the same HTML out, but a print stylesheet can hide what a
    screen shows, and an address broken across a line boundary is an address
    nobody can copy.
    """
    printed = printed_tron_report
    body = printed[printed.index("SUMMARY — MONEY IN, MONEY OUT") :]

    assert "OKX" in body
    assert OKX_ADDRESS in body, "the address was broken up or shortened in print"
    assert UNNAMED_SERVICE_ADDRESS in body
    assert body.index("OKX") < body.index("No operator can be named")
    # first page, not somewhere in the middle of the document
    assert printed.index("SUMMARY — MONEY IN, MONEY OUT") < printed.index("THE ANSWERS")


@needs_printing
def test_the_printed_document_still_carries_everything_that_was_condensed(
    printed_tron_report: str,
) -> None:
    """Folded, moved and de-duplicated — none of it dropped on the way to paper."""
    printed = printed_tron_report

    for evidence in (
        "ON-CHAIN FACTS",
        "THIRD-PARTY CLAIMS",
        "HEURISTIC INFERENCES",
        "value path over 3 transaction(s) links root to endpoint",
        "collects from 88 and pays out to 41 distinct addresses",
    ):
        assert evidence in printed, f"the print path lost {evidence!r}"
    assert "COVERAGE AND CAVEATS" in printed
    assert "APPENDIX — OTHER FINDINGS" in printed
    assert "HOW TO REPRODUCE THIS REPORT" in printed
    for provenance in ("ENGINE VERSION", "RULESET VERSION", "INVESTIGATION ID", "BUDGETS"):
        assert provenance in printed
    assert printed.index("COVERAGE AND CAVEATS") < printed.index("APPENDIX — OTHER FINDINGS")


@needs_printing
def test_the_directions_stay_apart_on_paper(printed_tron_report: str) -> None:
    """The forward direction named nobody, and the page must not lend it OKX."""
    printed = printed_tron_report
    summary = printed[printed.index("SUMMARY — MONEY IN, MONEY OUT") : printed.index("THE ANSWERS")]
    # Past the block's own heading, which carries both words itself.
    verdicts = summary[summary.index("Funds came IN from") :]
    money_out = verdicts[verdicts.index("MONEY OUT") :]

    assert "OKX" not in money_out
    assert "No operator can be named" in money_out
    assert "LEAD — AN INFERENCE, NOT AN ATTRIBUTION" in money_out


def test_the_direction_wording_matches_the_direction() -> None:
    """ "In" and "out" the wrong way round is a report that sends a request to
    the wrong exchange, and nothing else in the document would contradict it."""
    verdicts = summarise_answers(report_like_the_shipped_tron_run().answers, NOW)
    by_direction = {verdict.direction: verdict for verdict in verdicts}

    assert by_direction[Direction.BACKWARD].lead_in == "Funds came IN from"
    assert by_direction[Direction.FORWARD].lead_in == "Funds went OUT to"
    assert by_direction[Direction.BACKWARD].named is not None
    assert by_direction[Direction.BACKWARD].named.operator == "OKX"
    assert by_direction[Direction.BACKWARD].named.chain == TRON
    assert by_direction[Direction.FORWARD].named is None


def test_metadata_for_a_named_operator_is_still_reachable_from_the_document() -> None:
    """The summary states the name; the answers section still states where to file."""
    report = make_report(
        ranked=[RankedFinding(vasp_finding("0xbinance", named=True), hop=2)],
        profiles={
            "0xbinance": VaspProfile(
                entity="Binance",
                jurisdiction="Cayman Islands",
                le_request_channel="le.binance.com",
            )
        },
    )
    html = render_html(report)

    assert "Binance" in summary_of(report)
    assert "le.binance.com" in html and "Cayman Islands" in html

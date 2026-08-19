"""What the report is allowed to say, and what it may never leave out.

These lock down the decisions a renderer must not be able to undo: the caveats
section exists in every shape a run can end in, two different answers stay two
different answers with the difference spelled out, and a claim's age is never
implied by silence.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from cipherchain.core.models import Direction, Evidence, EvidenceKind, FindingKind
from cipherchain.investigation.answers import RankedFinding
from cipherchain.reporting.model import (
    EVIDENCE_ORDER,
    InvestigationReport,
    ReportHeader,
    TraversalCoverage,
    build_report,
    claim_entity,
    describe_claim_age,
    group_evidence,
)
from tests.reporting.conftest import (
    ALL_SHAPES,
    NOW,
    claim,
    header,
    make_report,
    mixer_finding,
    report_clean_run,
    report_with_both_answers,
    report_with_caveats,
    report_with_no_answers,
    report_with_one_answer,
    report_with_only_a_lead,
    report_with_reference_data_offered_for_an_unnamed_endpoint,
    vasp_finding,
)


@pytest.mark.parametrize("shape", sorted(ALL_SHAPES), ids=lambda s: s.replace(" ", "_"))
def test_every_report_shape_states_its_coverage(shape: str) -> None:
    """The caveats section is mandatory in all six shapes, empty runs included.

    A report that hides its own gaps is the most dangerous output this tool can
    produce, so "nothing to report" is itself reported.
    """
    report = ALL_SHAPES[shape]()
    assert report.caveats, f"{shape} produced a report with no coverage statement"


def test_a_report_cannot_be_constructed_without_caveats() -> None:
    """Emptiness is unrepresentable, not merely discouraged."""
    with pytest.raises(ValueError, match="coverage"):
        InvestigationReport(
            header=header(),
            answers=(),
            other_findings=(),
            coverage=TraversalCoverage(),
            caveats=(),
        )


def test_a_nearer_guess_and_a_further_named_endpoint_are_both_kept() -> None:
    """Neither answer may be dropped, and the report must say why there are two."""
    report = report_with_both_answers()
    backward = next(a for a in report.answers if a.direction is Direction.BACKWARD)

    assert backward.nearest is not None and backward.nearest.address == "0xguess"
    assert backward.nearest_named is not None and backward.nearest_named.address == "0xbinance"
    assert backward.same is False
    divergence = backward.divergence or ""
    assert "1 hop(s) out" in divergence and "3 hop(s) out" in divergence
    assert "legal request" in divergence


def test_a_single_answer_that_is_itself_named_is_not_printed_twice() -> None:
    """One fact, one row — the divergence note has nothing to explain."""
    report = report_with_one_answer()
    backward = next(a for a in report.answers if a.direction is Direction.BACKWARD)
    assert backward.same is True
    assert backward.divergence is None


def test_an_unnamed_endpoint_is_reported_as_nobody_to_ask() -> None:
    """The distinction that decides whether an investigator can act at all.

    A behavioural inference identifies custodial infrastructure; it never
    identifies an operator, so there is no respondent for a legal request.
    """
    report = report_with_caveats()
    backward = next(a for a in report.answers if a.direction is Direction.BACKWARD)
    assert backward.nearest is not None
    assert backward.nearest.named is False
    assert backward.nearest.entity is None
    assert backward.actionable is False

    caveat = report.caveat("unnamed_endpoint_only_backward")
    assert caveat is not None
    assert "sourced label" in caveat.detail


def test_a_direction_that_reached_nothing_is_named_as_a_gap() -> None:
    """Silence in one direction must not read as 'there is nothing there'."""
    report = report_with_no_answers()
    for route in ("backward", "forward"):
        caveat = report.caveat(f"no_endpoint_{route}")
        assert caveat is not None
        assert "not about the funds" in caveat.detail


def test_a_partial_run_leads_the_caveats() -> None:
    """'We stopped early' outranks every other gap and is stated first."""
    report = report_with_caveats()
    assert report.header.is_partial is True
    assert report.caveats[0].code == "partial_run"
    assert "rather than on an exhausted trail" in report.caveats[0].detail


def test_a_run_still_in_flight_admits_it_is_a_snapshot() -> None:
    """A report pulled mid-run must not read like a finished one."""
    report = make_report(status="running")
    caveat = report.caveat("run_in_flight")
    assert caveat is not None and "still in progress" in caveat.headline


def test_a_failed_run_carries_the_error_that_stopped_it() -> None:
    report = make_report(status="failed", error="ProviderUnavailable('etherscan')")
    caveat = report.caveat("failed_run")
    assert caveat is not None and "etherscan" in caveat.detail


def test_the_engines_own_statements_are_reproduced_word_for_word() -> None:
    """Partial supernode expansion reaches the reader in the engine's words.

    Re-describing a gap in the report's own words is how gaps get softened, so
    engine observations are quoted rather than summarised.
    """
    report = report_with_caveats()
    quoted = [c.detail for c in report.caveats if c.code == "engine_observation"]
    assert (
        "expansion capped at the 20 highest-value counterparties; "
        "40 were reached but never explored" in quoted
    )


def test_a_repeated_engine_statement_is_not_repeated_in_the_caveats() -> None:
    """The per-run coverage sentence hangs off every terminal finding.

    Printing it once per finding buried the statements that actually differ.
    """
    repeated = "the explored frontier ran dry within budgets"
    note = Evidence(kind=EvidenceKind.ENGINE_OBSERVATION, summary=repeated)
    findings = [
        replace(finding, evidence=(*finding.evidence, note))
        for finding in (mixer_finding("0xmixer_a"), mixer_finding("0xmixer_b"))
    ]
    report = make_report(findings=findings)
    assert [c.detail for c in report.caveats].count(repeated) == 1


def test_a_mixer_contact_is_reported_as_a_cut_trail() -> None:
    """The trail did not run dry there — it was cut, and that is different."""
    report = report_with_no_answers()
    caveat = report.caveat("mixer_contact")
    assert caveat is not None
    assert "de-anonymization" in caveat.detail
    assert caveat.subject == "0xtornado"


def test_a_clean_run_says_what_a_clean_record_does_not_prove() -> None:
    """The easiest section to drop is the one with nothing in it."""
    report = report_clean_run()
    assert report.coverage.complete is True
    caveat = report.caveat("no_gaps_recorded")
    assert caveat is not None
    assert "not proof that no other funds moved" in caveat.detail


def test_a_budget_extension_is_disclosed_without_erasing_the_clean_record() -> None:
    """Two true statements, and neither may silence the other.

    A run that extended its budget three times and then read every address it
    reached HAS a clean coverage record — the extensions are what bought it. The
    obvious implementation puts the disclosure among the gap caveats, where its
    presence suppresses "no coverage gaps were recorded"; the reader then loses
    the clean-record sentence in exactly the runs that worked hardest for it.
    """
    clean = report_clean_run()
    pursued = build_report(
        header=header(),
        findings=[],
        answers=[],
        coverage=replace(
            clean.coverage,
            budget_extensions=(
                "budget 'api_calls' extended from 100 to 200 to keep pursuing an unanswered "
                "objective (find_next_vasp still had no named endpoint)",
            ),
        ),
    )

    assert pursued.coverage.complete is True, "an extension is spend, not a gap"
    assert pursued.caveat("no_gaps_recorded") is not None
    extended = pursued.caveat("budget_extended")
    assert extended is not None
    assert "extended its own budget 1 time(s)" in extended.headline
    assert "budget 'api_calls' extended from 100 to 200" in extended.detail
    assert clean.caveat("budget_extended") is None, "a run that never extended says nothing"


def test_a_claim_with_no_date_is_flagged_harder_than_a_stale_one() -> None:
    """An undated claim used to look identical to one recorded this morning."""
    fresh, fresh_flag = describe_claim_age(claim(days_old=6), NOW)
    stale, stale_flag = describe_claim_age(claim(days_old=1200), NOW)
    undated, undated_flag = describe_claim_age(claim(days_old=None), NOW)

    assert "6 days old" in fresh and fresh_flag is False
    assert "1200 days old" in stale and stale_flag is True
    assert "unknown" in undated and undated_flag is True


def test_a_future_dated_claim_is_never_reported_as_fresh() -> None:
    """A data defect must not read as the freshest attribution in the document."""
    future = Evidence(
        kind=EvidenceKind.THIRD_PARTY_CLAIM,
        summary="Binance labeled 'vasp'",
        source="etherscan-tags",
        source_date=NOW + timedelta(days=30),
        confidence=0.9,
    )
    text, flag = describe_claim_age(future, NOW)
    assert "future" in text and flag is True


def test_the_operator_name_is_read_off_the_claim_and_only_the_claim() -> None:
    """Only a third-party claim ever names an operator — an inference never does."""
    assert claim_entity(vasp_finding("0xa", named=True, entity="Kraken")) == "Kraken"
    assert claim_entity(vasp_finding("0xb", named=False)) is None


def test_reference_data_cannot_supply_a_name_an_inference_never_had() -> None:
    """Metadata describes an operator a claim named; it may not produce one.

    Profiles are keyed by address, so a row exists for this address. The
    endpoint rests on a behavioural inference alone, so the report drops the row
    rather than print a legal entity and a filing channel for an address that
    nothing attributed to anybody.
    """
    report = report_with_reference_data_offered_for_an_unnamed_endpoint()
    backward = next(a for a in report.answers if a.direction is Direction.BACKWARD)

    assert backward.nearest is not None
    assert backward.nearest.named is False
    assert backward.nearest.entity is None
    assert backward.nearest.vasp is None
    assert backward.actionable is False


def test_evidence_is_grouped_in_reading_order_with_empty_kinds_dropped() -> None:
    """Verifiable first, guesses last; a kind with nothing in it prints nothing."""
    groups = group_evidence(vasp_finding("0xa", named=True))
    kinds = [g.kind for g in groups]
    assert kinds == [EvidenceKind.ONCHAIN_FACT, EvidenceKind.THIRD_PARTY_CLAIM]
    assert kinds == [k for k in EVIDENCE_ORDER if k in kinds]


def test_an_endpoint_shown_as_an_answer_is_not_listed_again_below() -> None:
    """Answers and findings come from separate queries: equal objects, not the same ones."""
    report = make_report(
        ranked=[RankedFinding(vasp_finding("0xbinance", named=True), hop=2)],
        findings=[vasp_finding("0xbinance", named=True), mixer_finding()],
    )
    assert [f.kind for f in report.other_findings] == [FindingKind.MIXER_INTERACTION]


def test_coverage_completeness_follows_the_counters_not_the_status() -> None:
    """A 'completed' run that left work on the frontier is not complete coverage."""
    assert TraversalCoverage(addresses_reached=5).complete is True
    assert TraversalCoverage(unexplored_frontier=1).complete is False
    assert TraversalCoverage(truncated_histories=1).complete is False
    assert TraversalCoverage(depth_horizon_stops=1).complete is False


def test_the_header_carries_what_a_conclusion_must_be_reproducible_against() -> None:
    """Engine and ruleset version, or the recipient cannot re-run the case."""
    head: ReportHeader = header()
    assert head.engine_version and head.ruleset_version
    assert head.is_settled is True
    assert header(status="running").is_settled is False


class TestALeadIsReportedAsALead:
    """REACHING_THE_VASP.md §4 at the last layer before a reader.

    The two failures are mirror images and both are silent: the direction that
    crossed a mixer printing NOTHING while the run holds a named exchange, and
    that same exchange printing in a slot whose heading asserts the funds were
    traced to it.
    """

    def test_a_lead_only_direction_is_not_reported_as_empty(self) -> None:
        report = report_with_only_a_lead()
        [section] = report.answers

        assert section.best_effort is not None
        assert section.best_effort.address == "0xpastmixer"
        assert section.has_answer, "a direction holding a lead is not an empty direction"

    def test_a_lead_never_occupies_a_traced_slot(self) -> None:
        [section] = report_with_only_a_lead().answers

        assert section.nearest is None
        assert section.nearest_named is None

    def test_a_named_lead_is_still_not_actionable(self) -> None:
        """The operator is named on a citable source and the path to it is a
        guess. `actionable` decides whether the report tells a reader they have
        somebody to serve, and this run cannot show the funds reached them."""
        [section] = report_with_only_a_lead().answers

        assert section.best_effort is not None and section.best_effort.named
        assert not section.actionable

    def test_the_caveat_says_the_route_crossed_a_mixer_and_names_the_basis(self) -> None:
        """The headline card carries the name; this is where the reader is told
        what it cost to get there — naming the versioned rule the branch rests
        on, so the caveat points at something checkable rather than hedging."""
        report = report_with_only_a_lead()
        [caveat] = [c for c in report.caveats if c.code == "lead_only_backward"]

        assert "without crossing a mixer" in caveat.headline
        assert "selected by the heuristic mixer-exit-anonymity-set@1" in caveat.detail
        assert caveat.subject == "0xpastmixer"

    def test_a_lead_is_not_repeated_in_the_other_findings_list(self) -> None:
        """It is surfaced in the headline; printing it again under "other
        findings" would read as two independent endpoints."""
        report = report_with_only_a_lead()

        assert not [
            f
            for f in report.other_findings
            if f.kind is FindingKind.VASP_ENDPOINT and f.subject.value == "0xpastmixer"
        ]

    def test_an_endpoint_cannot_be_offered_as_a_lead_without_a_weakness(self) -> None:
        """The structural half of the rule: no constructor path produces a named
        lead with no stated reason to doubt it, so "print the caveat" is not
        something a renderer has to remember."""
        [section] = report_with_only_a_lead().answers
        assert section.best_effort is not None

        with pytest.raises(ValueError):
            replace(section.best_effort, weakness="   ")


class TestTheReportDoesNotContradictItself:
    """Two caveats, one document, opposite claims.

    `mixer_contact` was written when every mixer was a full stop, and it says
    CipherChain "does not attempt de-anonymization, so value passing through cannot
    be followed and is not guessed at". Once a run can cross a pool, that
    sentence sits beside "N branch(es) were continued past a mixer by
    heuristic" — in the section a reader consults precisely to learn what the
    report cannot support. The stopped wording must survive for runs that
    really did stop.
    """

    @staticmethod
    def caveat(report, code: str):
        return next((c for c in report.caveats if c.code == code), None)

    def test_a_run_that_crossed_a_mixer_does_not_claim_it_never_guesses(self) -> None:
        report = make_report(
            findings=[mixer_finding()],
            coverage=TraversalCoverage(addresses_reached=9, mixer_crossings=2, mixer_stops=1),
        )
        contact = self.caveat(report, "mixer_contact")

        assert contact is not None
        assert "is not guessed at" not in contact.detail
        assert "SELECTED" in contact.detail

    def test_a_run_that_stopped_at_every_mixer_still_says_so_plainly(self) -> None:
        """The other half. Softening this for runs that did NOT cross would give
        away the strongest true statement the tool can make."""
        report = make_report(
            findings=[mixer_finding()],
            coverage=TraversalCoverage(addresses_reached=9, mixer_stops=2),
        )
        contact = self.caveat(report, "mixer_contact")

        assert contact is not None
        assert "is not guessed at" in contact.detail
        assert "The trail was cut" in contact.headline

    def test_crossings_and_stops_both_count_against_completeness(self) -> None:
        """A run that crossed a mixer has not covered the ground beyond it, and
        `complete` is what stops a summary claiming the trace read everything."""
        assert not TraversalCoverage(addresses_reached=5, mixer_crossings=1).complete
        assert not TraversalCoverage(addresses_reached=5, mixer_stops=1).complete
        assert TraversalCoverage(addresses_reached=5).complete

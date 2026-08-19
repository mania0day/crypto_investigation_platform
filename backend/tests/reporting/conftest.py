"""Report scenarios, built by hand so the hard cases are exact.

A report's job is hardest precisely where a live trace is least cooperative: no
answer at all, an answer nobody can be served with, a run that stopped early.
Waiting for the engine to produce those shapes would test whichever one it
happened to produce. These builders make each shape directly, which is what
lets every test below assert on the whole document rather than on a fragment.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from cipherchain.core.models import (
    Address,
    Direction,
    Evidence,
    EvidenceKind,
    Finding,
    FindingKind,
)
from cipherchain.investigation.answers import RankedFinding, select_answers
from cipherchain.reporting.model import (
    InvestigationReport,
    ReportHeader,
    TraversalCoverage,
    VaspProfile,
    build_report,
)

CHAIN = "testchain"
SUBJECT = Address(chain=CHAIN, value="0xsubject0000000000000000000000000000000001")
NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)

ONCHAIN = Evidence(
    kind=EvidenceKind.ONCHAIN_FACT,
    summary="value path over 3 transaction(s) links root to endpoint",
    refs=("0xtx_a", "0xtx_b", "0xtx_c"),
)


def claim(
    entity: str = "Binance", *, days_old: int | None = 6, source: str = "etherscan-tags"
) -> Evidence:
    """A third-party claim, worded exactly as the engine words one.

    Matching ``InvestigationEngine._vasp_finding`` matters: the report reads the
    operator's name back out of this string, and a test that invented its own
    phrasing would pass while the real format silently stopped resolving.
    """
    return Evidence(
        kind=EvidenceKind.THIRD_PARTY_CLAIM,
        summary=f"{entity} labeled 'vasp'",
        source=source,
        source_date=None if days_old is None else NOW - timedelta(days=days_old),
        confidence=0.9,
    )


INFERENCE = Evidence(
    kind=EvidenceKind.HEURISTIC_INFERENCE,
    summary="collects from 88 and pays out to 41 distinct addresses",
    heuristic="service-endpoint@1",
    confidence=0.61,
)


def engine_note(summary: str) -> Evidence:
    return Evidence(kind=EvidenceKind.ENGINE_OBSERVATION, summary=summary)


def vasp_finding(
    address: str,
    *,
    named: bool,
    direction: Direction = Direction.BACKWARD,
    confidence: float = 0.9,
    entity: str = "Binance",
    claim_days_old: int | None = 6,
    chain: str = CHAIN,
    source: str = "etherscan-tags",
) -> Finding:
    evidence = (
        (ONCHAIN, claim(entity, days_old=claim_days_old, source=source))
        if named
        else (ONCHAIN, INFERENCE)
    )
    label = entity if named else "custodial infrastructure, operator unnamed"
    return Finding(
        kind=FindingKind.VASP_ENDPOINT,
        subject=Address(chain=chain, value=address),
        summary=f"nearest {'previous' if direction is Direction.BACKWARD else 'next'} "
        f"VASP: {label}",
        confidence=confidence,
        direction=direction,
        evidence=evidence,
    )


#: A third-party claim about the ADDRESS, naming nobody who could be served.
#: Worded as the sanctions harvester words one, which is the point: it is a
#: perfectly legitimate third_party_claim — the kind the taxonomy is built for —
#: and ``claim_entity`` reads no operator out of it, because there is no
#: operator in it to read.
SANCTIONS_CLAIM_SUMMARY = "address identified as mixer 'Tornado Cash'"


def sanctions_claim(days_old: int = 900) -> Evidence:
    return Evidence(
        kind=EvidenceKind.THIRD_PARTY_CLAIM,
        summary=SANCTIONS_CLAIM_SUMMARY,
        source="ofac-sdn",
        source_date=NOW - timedelta(days=days_old),
        confidence=0.95,
    )


def endpoint_claimed_but_unnamed(
    address: str = "0xnameless",
    *,
    direction: Direction = Direction.BACKWARD,
    confidence: float = 0.9,
    chain: str = CHAIN,
    days_old: int = 900,
) -> Finding:
    """An endpoint whose only third-party claim names no operator.

    The gap this shape exists to hold open: ``is_named`` asks only whether some
    third_party_claim is attached, so this finding satisfies it and lands in the
    ``nearest_named`` slot — while there is no operator anywhere in it. Every
    "a legal request can be addressed to this operator" in the document used to
    be decided by the first of those two facts.
    """
    return Finding(
        kind=FindingKind.VASP_ENDPOINT,
        subject=Address(chain=chain, value=address),
        summary=(
            f"nearest {'previous' if direction is Direction.BACKWARD else 'next'} "
            "VASP: sanctioned service, operator unnamed"
        ),
        confidence=confidence,
        direction=direction,
        evidence=(ONCHAIN, sanctions_claim(days_old)),
    )


def mixer_finding(address: str = "0xtornado") -> Finding:
    return Finding(
        kind=FindingKind.MIXER_INTERACTION,
        subject=Address(chain=CHAIN, value=address),
        summary="funds reached a known mixer (Tornado Cash); the trail cannot be followed",
        confidence=0.95,
        direction=Direction.FORWARD,
        evidence=(
            Evidence(
                kind=EvidenceKind.THIRD_PARTY_CLAIM,
                summary="address identified as mixer 'Tornado Cash'",
                source="ofac-sdn",
                source_date=NOW - timedelta(days=900),
                confidence=0.95,
            ),
        ),
    )


def supernode_finding(address: str = "0xhub") -> Finding:
    return Finding(
        kind=FindingKind.TERMINAL,
        subject=Address(chain=CHAIN, value=address),
        summary="high-degree address (60 counterparties): followed the 20 largest by value",
        confidence=1.0,
        direction=Direction.FORWARD,
        evidence=(
            ONCHAIN,
            engine_note(
                "expansion capped at the 20 highest-value counterparties; "
                "40 were reached but never explored"
            ),
        ),
    )


def header(
    *,
    status: str = "completed",
    objectives: Sequence[str] = ("find_prev_vasp", "find_next_vasp"),
    error: str | None = None,
    subject: Address = SUBJECT,
) -> ReportHeader:
    return ReportHeader(
        investigation_id="7f0e0c4a-0000-4000-8000-00000000abcd",
        subject=subject,
        status=status,
        generated_at=NOW,
        engine_version="0.1.0",
        ruleset_version="baseline-2026-08-07",
        objectives=tuple(objectives),
        started_at=NOW - timedelta(minutes=12),
        updated_at=NOW - timedelta(minutes=1),
        budgets={"api_calls": 100, "max_depth": 6},
        spent={"api_calls": 41, "txs_normalized": 318},
        error=error,
    )


def make_report(
    *,
    ranked: Sequence[RankedFinding] = (),
    findings: Sequence[Finding] | None = None,
    directions: Sequence[Direction] = (Direction.BACKWARD, Direction.FORWARD),
    coverage: TraversalCoverage | None = None,
    status: str = "completed",
    profiles: dict[str, VaspProfile] | None = None,
    error: str | None = None,
    subject: Address = SUBJECT,
) -> InvestigationReport:
    """One report, assembled the way the API edge assembles one."""
    all_findings = list(findings) if findings is not None else [r.finding for r in ranked]
    return build_report(
        header=header(status=status, error=error, subject=subject),
        findings=all_findings,
        answers=select_answers(list(ranked), directions),
        coverage=coverage or TraversalCoverage(addresses_reached=24, transactions_examined=318),
        profiles=profiles,
    )


# ── the shapes every renderer has to survive ─────────────────────────────────

#: The run this document's summary is judged against: investigation
#: ba0783b9-1b26-44f2-a005-23f758a2d993 on Tron. Backward resolved to OKX two
#: hops out at 0.90, on a proof-of-reserves label whose signature was verified.
#: Forward reached no named endpoint at all — only an unnamed behavioural
#: service endpoint at ~61%.
#:
#: Its addresses are used at their real length on purpose. "The address is
#: printed in full" is not a claim a fixture address of eight characters can
#: falsify, and a truncated address in a report a regulator receives is useless.
TRON = "tron"
TRON_SUBJECT = Address(chain=TRON, value="TQ5NMqJjW8fyq3sSAtY3fVvzr4ZgUvJn9r")
OKX_ADDRESS = "TM1zzNDZD2DPASbKcgdVoTYhfmYgtfwx9R"
UNNAMED_SERVICE_ADDRESS = "TLdp3cdHk54miDW8MmTmTvpx5yUNQYQhKr"


def report_like_the_shipped_tron_run() -> InvestigationReport:
    """Named backward, nothing named forward — both shapes in one document."""
    return make_report(
        ranked=[
            RankedFinding(
                vasp_finding(
                    OKX_ADDRESS,
                    named=True,
                    entity="OKX",
                    chain=TRON,
                    source="okx-proof-of-reserves",
                    claim_days_old=11,
                ),
                hop=2,
            ),
            RankedFinding(
                vasp_finding(
                    UNNAMED_SERVICE_ADDRESS,
                    named=False,
                    direction=Direction.FORWARD,
                    confidence=0.61,
                    chain=TRON,
                ),
                hop=1,
            ),
        ],
        status="partial",
        subject=TRON_SUBJECT,
        coverage=TraversalCoverage(
            addresses_reached=1573,
            truncated_histories=97,
            unexplored_frontier=1435,
            transactions_examined=14982,
            max_depth=4,
        ),
    )


def report_with_both_answers() -> InvestigationReport:
    """Backward and forward both answered, and backward answered twice over."""
    return make_report(
        ranked=[
            RankedFinding(vasp_finding("0xguess", named=False, confidence=0.61), hop=1),
            RankedFinding(vasp_finding("0xbinance", named=True), hop=3),
            RankedFinding(
                vasp_finding("0xkraken", named=True, direction=Direction.FORWARD, entity="Kraken"),
                hop=2,
            ),
        ]
    )


def report_with_one_answer() -> InvestigationReport:
    """Backward answered; forward reached nothing at all."""
    return make_report(
        ranked=[RankedFinding(vasp_finding("0xbinance", named=True), hop=2)],
    )


def report_with_no_answers() -> InvestigationReport:
    """Neither direction reached an endpoint, but findings were still recorded."""
    return make_report(findings=[mixer_finding(), supernode_finding()])


def report_with_zero_findings() -> InvestigationReport:
    """A run that concluded nothing whatsoever — the emptiest legal report."""
    return make_report(findings=[])


def report_with_both_answers_but_partial() -> InvestigationReport:
    """Every objective answered with a name, and the run still stopped on budget.

    The shape the shipped case actually produced, and the one that misled a
    reader: "partial" is a statement about coverage, not about whether anybody
    was named.
    """
    return make_report(
        ranked=[
            RankedFinding(vasp_finding("0xguess", named=False, confidence=0.61), hop=1),
            RankedFinding(vasp_finding("0xbinance", named=True), hop=3),
            RankedFinding(
                vasp_finding("0xkraken", named=True, direction=Direction.FORWARD, entity="Kraken"),
                hop=2,
            ),
        ],
        status="partial",
        coverage=TraversalCoverage(
            addresses_reached=1573,
            truncated_histories=97,
            depth_horizon_stops=0,
            unexplored_frontier=1435,
            transactions_examined=14982,
            max_depth=4,
        ),
    )


def report_with_caveats() -> InvestigationReport:
    """A partial run with every coverage gap the traversal can record."""
    return make_report(
        ranked=[RankedFinding(vasp_finding("0xguess", named=False, confidence=0.61), hop=1)],
        findings=[
            vasp_finding("0xguess", named=False, confidence=0.61),
            mixer_finding(),
            supernode_finding(),
        ],
        status="partial",
        coverage=TraversalCoverage(
            addresses_reached=112,
            truncated_histories=4,
            depth_horizon_stops=9,
            unexplored_frontier=37,
            transactions_examined=980,
            max_depth=6,
        ),
    )


def report_clean_run() -> InvestigationReport:
    """Nothing wrong anywhere — the case where a caveats section is easiest to drop."""
    return make_report(
        ranked=[
            RankedFinding(vasp_finding("0xbinance", named=True), hop=2),
            RankedFinding(
                vasp_finding("0xkraken", named=True, direction=Direction.FORWARD, entity="Kraken"),
                hop=1,
            ),
        ],
        coverage=TraversalCoverage(addresses_reached=18, transactions_examined=204),
    )


def report_with_reference_data_offered_for_an_unnamed_endpoint() -> InvestigationReport:
    """An inference-only endpoint, with a metadata row sitting at its address.

    The shape that decides whether the evidence taxonomy holds at the last step.
    Profiles are keyed by address, so an address-indexed lookup hands this
    endpoint an operator, a jurisdiction and a place to file — for an address
    that no third-party claim ever attributed to anybody.
    """
    return make_report(
        ranked=[RankedFinding(vasp_finding("0xguess", named=False, confidence=0.61), hop=1)],
        profiles={
            "0xguess": VaspProfile(
                entity="Binance",
                jurisdiction="Cayman Islands",
                legal_entity="Binance Holdings Ltd",
                le_request_channel="le.binance.com",
            )
        },
    )


def report_with_a_claim_that_names_nobody() -> InvestigationReport:
    """One endpoint, sourced, and still nobody to serve — the mixer/sanctions shape.

    Partial on purpose. The status banner speaks only on a partial run, and this
    is the shape where the banner and the summary block are most easily made to
    contradict each other: one counting "an endpoint with a claim" as answered
    while the other can find no name to print.
    """
    return make_report(
        ranked=[RankedFinding(endpoint_claimed_but_unnamed(), hop=2)],
        directions=(Direction.BACKWARD,),
        status="partial",
        coverage=TraversalCoverage(
            addresses_reached=140,
            unexplored_frontier=22,
            transactions_examined=1204,
            max_depth=4,
        ),
    )


#: What ``nodes.speculative_basis`` holds after a rung-5 crossing — the id of
#: the heuristic that proposed the branch, which is what
#: ``_enqueue_mixer_candidates`` writes and what ``collect_report`` reads back.
#:
#: It was previously the candidate's prose weakness. That string is real, but
#: this field never carries it, so every rendered-report test below was
#: exercising a shape production cannot produce — and the shape it does produce
#: printed a bare identifier where the document promises plain language.
ANONYMITY_BASIS = "mixer-exit-anonymity-set@1"


def report_with_only_a_lead() -> InvestigationReport:
    """Every endpoint sits past a mixer crossing — the run has a name, not an answer.

    The shape that exists because both ways of handling it are wrong in a legal
    document: print the endpoint under "nearest VASP" and the report asserts a
    path that was never followed; drop it and the reader is handed an empty box
    while the run is holding a named exchange. It goes in ALL_SHAPES so every
    renderer has to survive it, which is what stops the lead being rendered by
    the traced-endpoint path the first time someone edits a template.
    """
    return make_report(
        ranked=[
            RankedFinding(
                vasp_finding("0xpastmixer", named=True),
                hop=3,
                speculative=True,
                speculative_basis=ANONYMITY_BASIS,
            )
        ],
        directions=(Direction.BACKWARD,),
    )


ALL_SHAPES = {
    "both answers but partial": report_with_both_answers_but_partial,
    "both answers": report_with_both_answers,
    "one answer": report_with_one_answer,
    "no answers": report_with_no_answers,
    "zero findings": report_with_zero_findings,
    "caveats": report_with_caveats,
    "clean run": report_clean_run,
    "lead only": report_with_only_a_lead,
    "claim names nobody": report_with_a_claim_that_names_nobody,
    "shipped tron run": report_like_the_shipped_tron_run,
}

"""Answer selection: a direction can yield two answers, and neither may vanish.

The shape that forced this: a behavioural inference at hop 1 and a sourced label
at hop 2, in the same direction. Reporting only one of them either hides a named
exchange behind a 61% guess, or hides the nearer endpoint behind a further one —
and which of those happened used to depend on traversal order, because the
frontend simply took the first VASP finding it saw.
"""

from __future__ import annotations

from cipherchain.core.models import Address, Direction, Evidence, EvidenceKind, Finding, FindingKind
from cipherchain.investigation.answers import RankedFinding, select_answers

CHAIN = "testchain"

ONCHAIN = Evidence(kind=EvidenceKind.ONCHAIN_FACT, summary="value path", refs=("tx_a",))
CLAIM = Evidence(
    kind=EvidenceKind.THIRD_PARTY_CLAIM,
    summary="Binance (operational address) labeled 'vasp'",
    source="etherscan-tags@2026-08-10",
    confidence=0.9,
)
INFERENCE = Evidence(
    kind=EvidenceKind.HEURISTIC_INFERENCE,
    summary="collects from 88 and pays out to 41 distinct addresses",
    heuristic="service-endpoint@1",
    confidence=0.61,
)


def vasp(address: str, *, named: bool, confidence: float, direction=Direction.BACKWARD) -> Finding:
    return Finding(
        kind=FindingKind.VASP_ENDPOINT,
        subject=Address(chain=CHAIN, value=address),
        summary=f"nearest previous VASP: {address}",
        confidence=confidence,
        direction=direction,
        evidence=(ONCHAIN, CLAIM) if named else (ONCHAIN, INFERENCE),
    )


def test_a_nearer_guess_never_hides_a_further_named_endpoint() -> None:
    ranked = [
        RankedFinding(vasp("guess_hop1", named=False, confidence=0.61), hop=1),
        RankedFinding(vasp("binance_hop2", named=True, confidence=0.9), hop=2),
    ]
    answer = select_answers(ranked, [Direction.BACKWARD])[0]

    assert answer.nearest is not None and answer.nearest.finding.subject.value == "guess_hop1"
    assert answer.nearest_named is not None
    assert answer.nearest_named.finding.subject.value == "binance_hop2"
    # Two genuinely different answers — the consumer must show both.
    assert answer.same is False


def test_a_further_named_endpoint_never_hides_the_nearer_one() -> None:
    """The mirror image: reporting only the named one would drop the nearest."""
    ranked = [
        RankedFinding(vasp("binance_hop3", named=True, confidence=0.9), hop=3),
        RankedFinding(vasp("guess_hop1", named=False, confidence=0.61), hop=1),
    ]
    answer = select_answers(ranked, [Direction.BACKWARD])[0]
    assert answer.nearest is not None and answer.nearest.hop == 1
    assert answer.nearest_named is not None and answer.nearest_named.hop == 3


def test_selection_ignores_insertion_order() -> None:
    """Order used to decide the headline. It must now decide nothing.

    Same two findings, both orderings, identical answer.
    """
    near = RankedFinding(vasp("guess_hop1", named=False, confidence=0.61), hop=1)
    far = RankedFinding(vasp("binance_hop2", named=True, confidence=0.9), hop=2)
    forwards = select_answers([near, far], [Direction.BACKWARD])[0]
    backwards = select_answers([far, near], [Direction.BACKWARD])[0]
    assert forwards.nearest.finding.subject.value == backwards.nearest.finding.subject.value
    assert (
        forwards.nearest_named.finding.subject.value
        == backwards.nearest_named.finding.subject.value
    )


def test_one_answer_when_the_nearest_endpoint_is_itself_named() -> None:
    """No duplicate: the same finding must not print under two headings."""
    ranked = [
        RankedFinding(vasp("binance_hop1", named=True, confidence=0.9), hop=1),
        RankedFinding(vasp("guess_hop2", named=False, confidence=0.61), hop=2),
    ]
    answer = select_answers(ranked, [Direction.BACKWARD])[0]
    assert answer.same is True
    assert answer.nearest.finding.subject.value == "binance_hop1"


def test_no_named_endpoint_is_reported_as_absent_not_substituted() -> None:
    ranked = [RankedFinding(vasp("guess_hop1", named=False, confidence=0.61), hop=1)]
    answer = select_answers(ranked, [Direction.BACKWARD])[0]
    assert answer.nearest is not None
    assert answer.nearest_named is None, "an unnamed endpoint must never pass as a named one"


def test_directions_do_not_borrow_each_others_answers() -> None:
    ranked = [
        RankedFinding(vasp("in_hop1", named=True, confidence=0.9, direction=Direction.BACKWARD), 1),
        RankedFinding(vasp("out_hop2", named=True, confidence=0.9, direction=Direction.FORWARD), 2),
    ]
    backward, forward = select_answers(ranked, [Direction.BACKWARD, Direction.FORWARD])
    assert backward.nearest.finding.subject.value == "in_hop1"
    assert forward.nearest.finding.subject.value == "out_hop2"


def test_a_tie_between_two_guesses_goes_to_the_stronger_claim() -> None:
    """With nothing named to protect, the tie-break is plain confidence."""
    ranked = [
        RankedFinding(vasp("weak", named=False, confidence=0.55), hop=1),
        RankedFinding(vasp("strong", named=False, confidence=0.72), hop=1),
    ]
    answer = select_answers(ranked, [Direction.BACKWARD])[0]
    assert answer.nearest.finding.subject.value == "strong"


def test_a_tie_between_a_guess_and_a_label_stays_two_rows() -> None:
    """The collapse is for one fact, not for one distance.

    A guess and a sourced label at the same hop are two different addresses.
    Letting the label take both slots would print a single row and silently
    delete the guess from the headline — the same hiding the rest of this module
    exists to prevent, just at zero hop separation. Both orderings, because a tie
    is exactly where insertion order would creep back in.
    """
    label = RankedFinding(vasp("binance_hop1", named=True, confidence=0.9), hop=1)
    guess = RankedFinding(vasp("guess_hop1", named=False, confidence=0.62), hop=1)

    for ranked in ([label, guess], [guess, label]):
        answer = select_answers(ranked, [Direction.BACKWARD])[0]
        assert answer.same is False, "two addresses, two rows — even at equal distance"
        assert answer.nearest is not None
        assert answer.nearest.finding.subject.value == "guess_hop1"
        assert answer.nearest_named is not None
        assert answer.nearest_named.finding.subject.value == "binance_hop1"


def test_no_findings_yields_no_answer_rather_than_an_empty_one() -> None:
    answer = select_answers([], [Direction.BACKWARD])[0]
    assert answer.nearest is None and answer.nearest_named is None
    assert answer.same is False

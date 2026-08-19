"""The third answer slot: a lead where there is no answer, never instead of one.

The ruling this locks down is "VASP is main and important, say weak decision
like because of mixer and stuff but i need VASP" (REACHING_THE_VASP.md §4). The
two failures it sits between are both silent:

- a direction that crossed a mixer reporting NOTHING, because speculative
  endpoints are barred from the two real slots — the reader is handed an empty
  box while the run holds a name;
- a guess taking the headline off an endpoint that was actually traced, or
  arriving without the sentence that says it is a guess.

Every test here is one of those two.
"""

from __future__ import annotations

import pytest

from cipherchain.core.models import Address, Direction, Evidence, EvidenceKind, Finding, FindingKind
from cipherchain.investigation.answers import (
    BestEffortFinding,
    DirectionAnswer,
    RankedFinding,
    select_answers,
)

CHAIN = "testchain"

ONCHAIN = Evidence(kind=EvidenceKind.ONCHAIN_FACT, summary="value path", refs=("tx_a",))
CLAIM = Evidence(
    kind=EvidenceKind.THIRD_PARTY_CLAIM,
    summary="Kraken (deposit address) labeled 'vasp'",
    source="etherscan-tags@2026-08-10",
    confidence=0.9,
)
INFERENCE = Evidence(
    kind=EvidenceKind.HEURISTIC_INFERENCE,
    summary="collects from 88 and pays out to 41 distinct addresses",
    heuristic="service-endpoint@1",
    confidence=0.61,
)

# What `nodes.speculative_basis` ACTUALLY holds after a rung-5 crossing: the
# heuristic id, written by `_enqueue_mixer_candidates` from
# `candidate.heuristic` and inherited unchanged by every descendant. This used
# to be the candidate's prose weakness ("one of 412 withdrawals in the anonymity
# set within 7 days"), which is a real string but one this field never carries —
# so the suite proved the weakness read well in a shape production never
# produces, while the shape it does produce printed a bare identifier as the
# reason a branch is a guess.
ANONYMITY_BASIS = "mixer-exit-anonymity-set@1"

# A prose basis is still legal — a caller can rank findings by hand outside a
# traversal — and is worded differently, so it is covered too.
PROSE_BASIS = (
    "one of 412 withdrawals in the anonymity set within 7 days; "
    "this is a lead, not an attribution"
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


def lead(
    address: str,
    *,
    hop: int,
    named: bool = True,
    confidence: float = 0.17,
    basis: str | None = ANONYMITY_BASIS,
    direction: Direction = Direction.BACKWARD,
) -> RankedFinding:
    """A VASP endpoint sitting past a mixer crossing — speculative, with a basis."""
    return RankedFinding(
        finding=vasp(address, named=named, confidence=confidence, direction=direction),
        hop=hop,
        speculative=True,
        speculative_basis=basis,
    )


def test_a_traced_answer_always_beats_a_lead_however_close_the_lead_is() -> None:
    """The regression this exists to prevent: a guess taking a real answer's place.

    The lead is nearer, more confident and named, and still must not appear —
    ``best_effort`` fills silence, and there is no silence here.
    """
    ranked = [
        lead("kraken_hop1", hop=1, named=True, confidence=0.5),
        RankedFinding(vasp("binance_hop4", named=True, confidence=0.9), hop=4),
    ]
    answer = select_answers(ranked, [Direction.BACKWARD])[0]

    assert answer.nearest is not None
    assert answer.nearest.finding.subject.value == "binance_hop4"
    assert answer.best_effort is None, "a lead must never displace a traced endpoint"


def test_a_speculative_endpoint_never_fills_the_nearest_or_named_slots() -> None:
    """A sourced label on a mixer branch is a true label reached by a guessed path.

    Letting it into ``nearest_named`` would print it under a heading that
    asserts the path was followed — REACHING_THE_VASP.md §3's "confident path to
    an exchange account with no connection to the case".
    """
    answer = select_answers([lead("kraken_hop3", hop=3)], [Direction.BACKWARD])[0]

    assert answer.nearest is None
    assert answer.nearest_named is None, "a selected path must not read as a traced one"
    assert answer.best_effort is not None
    assert answer.best_effort.finding.subject.value == "kraken_hop3"


def test_a_lead_names_the_heuristic_that_selected_its_branch() -> None:
    """The basis is an identifier, and must be worded as one.

    ``nodes.speculative_basis`` holds ``mixer-exit-anonymity-set@1``. Dropped
    into the sentence after a colon it read "reached only by following a
    speculative branch: mixer-exit-anonymity-set@1" — an identifier standing in
    the grammatical slot of a reason, in the one field ``BestEffortFinding``
    exists to make unskippable. Named as a rule it is useful: it tells a reader
    which published heuristic to go and check.
    """
    answer = select_answers([lead("kraken_hop3", hop=3)], [Direction.BACKWARD])[0]

    assert answer.best_effort is not None
    weakness = answer.best_effort.weakness
    assert "selected by the heuristic mixer-exit-anonymity-set@1" in weakness
    assert "not observed" in weakness
    assert weakness.lower().count("not an attribution") == 1


def test_a_prose_basis_is_still_read_as_the_reason_it_is() -> None:
    """A hand-built lead may carry a sentence, and then the sentence is quoted.

    Both shapes have to work: the wording branches, the guarantee does not.
    """
    answer = select_answers(
        [lead("kraken_hop3", hop=3, basis=PROSE_BASIS)], [Direction.BACKWARD]
    )[0]

    assert answer.best_effort is not None
    assert "412 withdrawals in the anonymity set" in answer.best_effort.weakness
    # Said once. The prose basis already ends "a lead, not an attribution", and
    # a warning printed twice in one line reads as boilerplate.
    assert answer.best_effort.weakness.lower().count("not an attribution") == 1


def test_a_lead_with_no_recorded_basis_still_states_a_weakness() -> None:
    """A missing basis must degrade the sentence, never empty it.

    A CHECK constraint guarantees a basis on any node marked speculative, but a
    caller can rank a finding by hand, and this is the path where a blank
    weakness would otherwise reach the constructor.
    """
    answer = select_answers([lead("kraken_hop3", hop=3, basis=None)], [Direction.BACKWARD])[0]

    assert answer.best_effort is not None
    assert answer.best_effort.weakness.strip()
    assert "speculative" in answer.best_effort.weakness


def test_an_unnamed_lead_says_that_no_source_names_the_operator() -> None:
    """Two separate weaknesses: the path, and the endpoint.

    A marked path must not be read as "at least we know what it is". An
    inference describes behaviour and never names an operator, and where the
    path is a guess too the reader is entitled to both halves.
    """
    answer = select_answers(
        [lead("service_hop2", hop=2, named=False, confidence=0.61)], [Direction.BACKWARD]
    )[0]

    assert answer.best_effort is not None
    assert "no source names the operator" in answer.best_effort.weakness


def test_a_best_effort_finding_cannot_be_built_without_a_weakness() -> None:
    """Structural, not conventional: no constructor path yields a bare lead."""
    finding = vasp("kraken", named=True, confidence=0.4)
    for blank in ("", "   ", "\n"):
        with pytest.raises(ValueError, match="weakness"):
            BestEffortFinding(finding=finding, hop=2, speculative=True, weakness=blank)


def test_an_answer_cannot_hold_a_lead_beside_a_traced_endpoint() -> None:
    """The invariant is enforced by the type, not only by the selection code.

    Consumers render ``best_effort`` in the headline position, so a stray one
    constructed elsewhere would take the headline off a traced answer.
    """
    traced = RankedFinding(vasp("binance", named=True, confidence=0.9), hop=2)
    guess = BestEffortFinding(
        finding=vasp("kraken", named=True, confidence=0.4),
        hop=1,
        speculative=True,
        speculative_basis=ANONYMITY_BASIS,
        weakness="reached only by following a speculative branch",
    )
    with pytest.raises(ValueError, match="never competes"):
        DirectionAnswer(
            direction=Direction.BACKWARD,
            nearest=traced,
            nearest_named=None,
            best_effort=guess,
        )


def test_the_lead_prefers_the_named_endpoint_among_equals() -> None:
    """The tie-break flips relative to ``nearest``, and that is deliberate.

    ``_closest`` prefers the unnamed candidate because a named slot sits beside
    it. Nothing sits beside this one, so the reader gets the lead that can
    actually be filed on rather than "custodial infrastructure, operator
    unnamed". Both orderings, because a tie is where insertion order creeps in.
    """
    named = lead("kraken_hop2", hop=2, named=True, confidence=0.4)
    unnamed = lead("service_hop2", hop=2, named=False, confidence=0.61)

    for ranked in ([named, unnamed], [unnamed, named]):
        answer = select_answers(ranked, [Direction.BACKWARD])[0]
        assert answer.best_effort is not None
        assert answer.best_effort.finding.subject.value == "kraken_hop2"


def test_the_lead_is_nearest_first_and_named_only_after_that() -> None:
    """Distance still dominates: a named lead four hops out is not "closer"."""
    ranked = [
        lead("kraken_hop4", hop=4, named=True, confidence=0.5),
        lead("service_hop1", hop=1, named=False, confidence=0.2),
    ]
    answer = select_answers(ranked, [Direction.BACKWARD])[0]

    assert answer.best_effort is not None
    assert answer.best_effort.finding.subject.value == "service_hop1"


def test_directions_report_their_leads_independently() -> None:
    """A strong answer one way must not imply anything about the other way.

    The product answers two questions and they fail separately. A run that
    names the funding exchange and only guesses the cash-out must show both,
    each labelled for what it is.
    """
    ranked = [
        RankedFinding(
            vasp("binance_in", named=True, confidence=0.9, direction=Direction.BACKWARD), hop=2
        ),
        lead("kraken_out", hop=3, direction=Direction.FORWARD),
    ]
    backward, forward = select_answers(ranked, [Direction.BACKWARD, Direction.FORWARD])

    assert backward.nearest is not None and backward.best_effort is None
    assert forward.nearest is None and forward.nearest_named is None
    assert forward.best_effort is not None
    assert forward.best_effort.finding.subject.value == "kraken_out"


def test_lead_selection_ignores_insertion_order() -> None:
    """Traversal order decided the headline once. It must decide nothing here."""
    near = lead("kraken_hop1", hop=1)
    far = lead("kraken_hop3", hop=3)
    forwards = select_answers([near, far], [Direction.BACKWARD])[0]
    backwards = select_answers([far, near], [Direction.BACKWARD])[0]

    assert forwards.best_effort is not None and backwards.best_effort is not None
    assert forwards.best_effort.finding.subject.value == backwards.best_effort.finding.subject.value


def test_a_direction_with_nothing_at_all_invents_no_lead() -> None:
    """Silence stays silence. "There is always a VASP" is false as a guarantee.

    Coins get mined, funds sit uncashed, settlement happens off-ledger
    (REACHING_THE_VASP.md §2). A slot that filled itself anyway would be
    fabrication, so an empty direction reports three empty slots.
    """
    answer = select_answers([], [Direction.BACKWARD])[0]

    assert answer.nearest is None
    assert answer.nearest_named is None
    assert answer.best_effort is None


def test_existing_callers_keep_working_without_the_speculation_fields() -> None:
    """Backward compatibility, pinned: the engine wiring adopts this a caller at a time.

    A ``RankedFinding`` built the old way reports a traced answer, which is
    exactly what every caller meant before mixer crossing existed.
    """
    ranked = [RankedFinding(vasp("binance", named=True, confidence=0.9), hop=2)]
    answer = select_answers(ranked, [Direction.BACKWARD])[0]

    assert answer.nearest is not None and answer.same is True
    assert answer.best_effort is None

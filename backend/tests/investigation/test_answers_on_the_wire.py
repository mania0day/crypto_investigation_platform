"""The answer slots as a consumer receives them.

The domain can refuse to build a lead without a weakness and still have it
reach the browser as ``null`` if the wire model is looser than the type behind
it. These tests sit on the mapping itself, because that is where the caveat
would be lost — and a lead printed without its caveat is a guess formatted
exactly like a traced answer.

``GraphNodeOut`` is here for the same reason at one remove: the renderer cannot
obey "never draw a guess as a traced path" if the payload never told it which
nodes are guesses.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cipherchain.api.schemas import AnswerOut, BestEffortOut, GraphNodeOut
from cipherchain.core.models import Address, Direction, Evidence, EvidenceKind, Finding, FindingKind
from cipherchain.investigation.answers import BestEffortFinding, RankedFinding, select_answers
from cipherchain.storage.repositories import GraphNode

CHAIN = "testchain"

ONCHAIN = Evidence(kind=EvidenceKind.ONCHAIN_FACT, summary="value path", refs=("tx_a",))
CLAIM = Evidence(
    kind=EvidenceKind.THIRD_PARTY_CLAIM,
    summary="Kraken 6 (deposit address) labeled 'vasp'",
    source="etherscan-tags@2026-08-10",
    confidence=0.9,
)
BASIS = "one of 6 withdrawals in the anonymity set within 7 days"


def vasp_finding(address: str, *, direction: Direction = Direction.FORWARD) -> Finding:
    return Finding(
        kind=FindingKind.VASP_ENDPOINT,
        subject=Address(chain=CHAIN, value=address),
        summary=f"nearest next VASP: {address}",
        confidence=0.17,
        direction=direction,
        evidence=(ONCHAIN, CLAIM),
    )


def graph_node(**overrides: object) -> GraphNode:
    fields: dict[str, object] = {
        "id": 7,
        "chain": CHAIN,
        "address": "0xcandidate",
        "direction": "forward",
        "hop_distance": 3,
        "value_share": None,
        "state": "expanded",
        "history_truncated": False,
        "terminal_reason": None,
        "discovered_reason": "mixer_candidate",
    }
    fields.update(overrides)
    return GraphNode(**fields)  # type: ignore[arg-type]


def test_a_lead_reaches_the_wire_with_its_weakness_attached() -> None:
    """The name and the caveat are one statement and must travel together."""
    ranked = RankedFinding(
        finding=vasp_finding("0xkraken"),
        hop=3,
        speculative=True,
        speculative_basis=BASIS,
    )
    payload = AnswerOut.of(select_answers([ranked], [Direction.FORWARD])[0]).model_dump()

    assert payload["nearest"] is None and payload["nearest_named"] is None
    assert payload["best_effort"] is not None
    assert payload["best_effort"]["address"] == "0xkraken"
    assert payload["best_effort"]["claim"] == CLAIM.summary
    assert BASIS in payload["best_effort"]["weakness"]
    assert payload["best_effort"]["speculative"] is True


def test_an_answer_entry_reports_whether_it_was_reached_by_speculation() -> None:
    """Present on EVERY entry, not only on the lead.

    A renderer decides how to draw a row from the row itself. If only the lead
    carried the flag, any other slot that ever admits a speculative endpoint
    would silently render as traced.
    """
    ranked = RankedFinding(finding=vasp_finding("0xbinance"), hop=2)
    payload = AnswerOut.of(select_answers([ranked], [Direction.FORWARD])[0]).model_dump()

    assert payload["nearest"]["speculative"] is False
    assert payload["nearest"]["speculative_basis"] is None
    assert payload["best_effort"] is None


def test_the_wire_model_refuses_a_lead_with_an_empty_weakness() -> None:
    """Belt and braces with the domain type: the caveat is non-nullable here too.

    A model that accepted "" would let a future caller construct the entry
    directly and ship a headline with nothing marking it.
    """
    with pytest.raises(ValidationError):
        BestEffortOut(
            address="0xkraken",
            hop=3,
            confidence=0.17,
            named=True,
            claim=CLAIM.summary,
            summary="nearest next VASP",
            speculative=True,
            speculative_basis=BASIS,
            weakness="",
        )


def test_a_lead_maps_across_without_losing_its_weakness() -> None:
    """``of_lead`` is the only constructor path from the domain, and it is total."""
    lead = BestEffortFinding(
        finding=vasp_finding("0xkraken"),
        hop=3,
        speculative=True,
        speculative_basis=BASIS,
        weakness="reached only by following a speculative branch: " + BASIS,
    )
    entry = BestEffortOut.of_lead(lead)

    assert entry.weakness == lead.weakness
    assert entry.speculative is True
    assert entry.speculative_basis == BASIS
    assert entry.hop == 3


def test_a_graph_node_says_whether_it_is_a_guess_and_why() -> None:
    """Without these two fields the picture cannot draw the difference at all."""
    out = GraphNodeOut.of(graph_node(speculative=True, speculative_basis=BASIS))

    assert out.speculative is True
    assert out.speculative_basis == BASIS


def test_a_traced_graph_node_is_reported_as_traced() -> None:
    """The default must be "observed" — a guess is the exceptional claim."""
    out = GraphNodeOut.of(graph_node(discovered_reason="counterparty"))

    assert out.speculative is False
    assert out.speculative_basis is None

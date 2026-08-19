"""A sourced claim is not the same fact as a named operator.

``is_named`` gates the engine's ``answered`` decision (``_finish_partial``) and
every actionability line in the report. It used to ask only whether a
``third_party_claim`` existed on the finding — and plenty of legitimate claims
name nobody. A sanctions listing is a sourced, citable claim ABOUT an address;
it does not say which company runs it.

Under the old test such a finding closed an objective as answered and the report
told an investigator a legal request could be served — on a respondent it could
not name. These tests hold the two facts apart.
"""

from __future__ import annotations

from cipherchain.core.models import Address, Direction, Evidence, EvidenceKind, Finding, FindingKind
from cipherchain.investigation.answers import claim_entity, is_named, naming_claim

CHAIN = "testchain"

ONCHAIN = Evidence(kind=EvidenceKind.ONCHAIN_FACT, summary="value path", refs=("tx_a",))
NAMES_AN_OPERATOR = Evidence(
    kind=EvidenceKind.THIRD_PARTY_CLAIM,
    summary="OKX labeled 'vasp'",
    source="okx-por@2026-08-10",
    confidence=0.9,
)
# Shaped exactly like the sanctions claims the SDN harvest emits: sourced,
# citable, and naming no operator at all.
NAMES_NOBODY = Evidence(
    kind=EvidenceKind.THIRD_PARTY_CLAIM,
    summary="address identified as mixer 'Tornado Cash'",
    source="ofac-sdn@2026-08-19",
    confidence=0.9,
)
INFERENCE = Evidence(
    kind=EvidenceKind.HEURISTIC_INFERENCE,
    summary="collects from 88 and pays out to 41 distinct addresses",
    heuristic="service-endpoint@1",
    confidence=0.61,
)


def finding(*evidence: Evidence) -> Finding:
    return Finding(
        kind=FindingKind.VASP_ENDPOINT,
        subject=Address(chain=CHAIN, value="subject"),
        summary="endpoint",
        confidence=0.9,
        direction=Direction.BACKWARD,
        evidence=(ONCHAIN, *evidence),
    )


def test_a_claim_that_names_an_operator_is_named() -> None:
    assert is_named(finding(NAMES_AN_OPERATOR)) is True
    assert claim_entity(finding(NAMES_AN_OPERATOR)) == "OKX"


def test_a_sourced_claim_that_names_nobody_does_not_answer_the_question() -> None:
    """The regression. This finding carries a real third_party_claim."""
    subject = finding(NAMES_NOBODY)
    assert any(e.kind is EvidenceKind.THIRD_PARTY_CLAIM for e in subject.evidence)
    assert is_named(subject) is False
    assert claim_entity(subject) is None


def test_the_nameless_claim_is_still_kept_as_evidence() -> None:
    """Refusing to call it an attribution must not discard it.

    That an endpoint is a known mixer matters to the reader even though it names
    no company to write to.
    """
    subject = finding(NAMES_NOBODY)
    assert naming_claim(subject) is NAMES_NOBODY
    assert NAMES_NOBODY in subject.evidence


def test_behaviour_alone_is_never_named() -> None:
    assert is_named(finding(INFERENCE)) is False
    assert claim_entity(finding(INFERENCE)) is None


def test_the_name_is_read_from_the_claim_that_carries_one() -> None:
    """A finding can hold both; the naming claim is the one that names."""
    both = finding(NAMES_NOBODY, NAMES_AN_OPERATOR)
    assert is_named(both) is True
    assert claim_entity(both) == "OKX"
    assert naming_claim(both) is NAMES_AN_OPERATOR

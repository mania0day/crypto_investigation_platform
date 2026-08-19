"""Service-endpoint inference: identifying a VASP's ROLE without a label.

The chain can show that an address behaves like custodial infrastructure.
It can never show WHO operates it. These tests hold that line.
"""

from datetime import UTC, datetime

from cipherchain.analysis.heuristics.service import (
    SERVICE_HEURISTIC,
    assess_service_endpoint,
    detect_service_endpoint,
)
from cipherchain.core.models import Address, EvidenceKind, FindingKind
from cipherchain.storage.repositories import StoredMovement

NOW = datetime(2026, 8, 7, tzinfo=UTC)
SUBJECT = Address("ethereum", "0xhub")


def mv(i: int, *, incoming: bool, peer: int) -> StoredMovement:
    return StoredMovement(
        id=i,
        transaction_id=i,
        chain="ethereum",
        tx_hash=f"0x{i:04x}",
        from_address_id=peer if incoming else 1,
        to_address_id=1 if incoming else peer,
        kind="native",
        asset_id=1,
        amount=1000,
        timestamp=NOW,
    )


def busy(senders: int, recipients: int):
    incoming = [mv(i, incoming=True, peer=1000 + i) for i in range(senders)]
    outgoing = [mv(5000 + i, incoming=False, peer=2000 + i) for i in range(recipients)]
    return incoming, outgoing


def test_bidirectional_hub_is_identified_as_a_service() -> None:
    incoming, outgoing = busy(40, 40)
    found = detect_service_endpoint(SUBJECT, incoming, outgoing)
    assert len(found) == 1
    assert found[0].kind is FindingKind.VASP_ENDPOINT
    assert "operator unnamed" in found[0].summary


def test_finding_never_invents_an_operator_name() -> None:
    """The whole point: role yes, identity no."""
    incoming, outgoing = busy(60, 60)
    text = " ".join(
        found.summary + " " + " ".join(e.summary for e in found.evidence)
        for found in detect_service_endpoint(SUBJECT, incoming, outgoing)
    ).lower()
    for brand in ("binance", "coinbase", "kraken", "okx", "bitfinex"):
        assert brand not in text
    assert "identity is off-chain" in text


def test_evidence_is_an_inference_not_a_claim() -> None:
    incoming, outgoing = busy(40, 40)
    finding = detect_service_endpoint(SUBJECT, incoming, outgoing)[0]
    kinds = {e.kind for e in finding.evidence}
    assert EvidenceKind.HEURISTIC_INFERENCE in kinds
    # never a third-party claim: nobody told us this, we inferred it
    assert EvidenceKind.THIRD_PARTY_CLAIM not in kinds
    inference = next(e for e in finding.evidence if e.kind is EvidenceKind.HEURISTIC_INFERENCE)
    assert inference.heuristic == SERVICE_HEURISTIC


def test_confidence_stays_modest() -> None:
    """A behavioural inference about an unnamed operator must not read as
    strongly as a sourced attribution."""
    incoming, outgoing = busy(400, 400)
    assert detect_service_endpoint(SUBJECT, incoming, outgoing)[0].confidence <= 0.75


def test_airdrop_distributor_is_not_a_service() -> None:
    """Pays out to thousands, receives from almost nobody — a distributor,
    not custodial infrastructure. Requiring BOTH directions excludes it."""
    incoming, outgoing = busy(2, 500)
    assert detect_service_endpoint(SUBJECT, incoming, outgoing) == []


def test_collection_only_address_is_not_a_service() -> None:
    incoming, outgoing = busy(500, 2)
    assert detect_service_endpoint(SUBJECT, incoming, outgoing) == []


def test_ordinary_wallet_is_not_a_service() -> None:
    incoming, outgoing = busy(5, 5)
    assert detect_service_endpoint(SUBJECT, incoming, outgoing) == []
    assert assess_service_endpoint(incoming, outgoing)[0] is False


def test_burn_address_is_never_a_service() -> None:
    """The all-zero address accumulates traffic from everywhere and belongs
    to nobody; ending a trace there would be a black hole, not an answer."""
    from cipherchain.analysis.heuristics.service import is_sentinel_address

    incoming, outgoing = busy(200, 200)
    burn = Address("ethereum", "0x" + "0" * 40)
    assert is_sentinel_address(burn.value)
    assert detect_service_endpoint(burn, incoming, outgoing) == []
    # a normal address with the same traffic still qualifies
    assert detect_service_endpoint(SUBJECT, incoming, outgoing)

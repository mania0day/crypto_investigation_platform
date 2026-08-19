"""Sweep heuristic: what it must catch, and what it must NOT."""

from datetime import UTC, datetime, timedelta

from cipherchain.analysis.heuristics.sweep import SWEEP_HEURISTIC, detect_sweeps, find_sweep_matches
from cipherchain.core.models import Address, EvidenceKind, FindingKind
from cipherchain.storage.repositories import StoredMovement

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
SUBJECT = Address("ethereum", "0xdeposit")


def movement(
    identifier: int,
    amount: int,
    when: datetime,
    *,
    incoming: bool,
    asset_id: int = 1,
) -> StoredMovement:
    return StoredMovement(
        id=identifier,
        transaction_id=identifier,
        chain="ethereum",
        tx_hash=f"0x{identifier:04x}",
        from_address_id=None if incoming else 99,
        to_address_id=99 if incoming else None,
        kind="native",
        asset_id=asset_id,
        amount=amount,
        timestamp=when,
    )


def test_detects_prompt_near_total_forward() -> None:
    incoming = [movement(1, 1_000, NOW, incoming=True)]
    outgoing = [movement(2, 995, NOW + timedelta(minutes=5), incoming=False)]

    findings = detect_sweeps(SUBJECT, incoming, outgoing)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.kind is FindingKind.SWEEP_PATTERN
    assert "pass-through" in finding.summary

    inference = next(e for e in finding.evidence if e.kind is EvidenceKind.HEURISTIC_INFERENCE)
    assert inference.heuristic == SWEEP_HEURISTIC  # versioned
    assert inference.confidence == finding.confidence
    fact = next(e for e in finding.evidence if e.kind is EvidenceKind.ONCHAIN_FACT)
    assert set(fact.refs) == {"0x0001", "0x0002"}  # both transactions cited


def test_partial_forward_is_not_a_sweep() -> None:
    incoming = [movement(1, 1_000, NOW, incoming=True)]
    outgoing = [movement(2, 400, NOW + timedelta(minutes=5), incoming=False)]
    assert detect_sweeps(SUBJECT, incoming, outgoing) == []


def test_outflow_before_inflow_is_never_a_sweep() -> None:
    """Time-respecting: value cannot leave before it arrived."""
    incoming = [movement(1, 1_000, NOW, incoming=True)]
    outgoing = [movement(2, 990, NOW - timedelta(hours=1), incoming=False)]
    assert detect_sweeps(SUBJECT, incoming, outgoing) == []


def test_different_asset_is_not_a_sweep() -> None:
    """A swap is a different pattern; conflating them misreports value."""
    incoming = [movement(1, 1_000, NOW, incoming=True, asset_id=1)]
    outgoing = [movement(2, 990, NOW + timedelta(minutes=5), incoming=False, asset_id=2)]
    assert detect_sweeps(SUBJECT, incoming, outgoing) == []


def test_forward_after_the_window_is_not_a_sweep() -> None:
    incoming = [movement(1, 1_000, NOW, incoming=True)]
    outgoing = [movement(2, 990, NOW + timedelta(days=3), incoming=False)]
    assert detect_sweeps(SUBJECT, incoming, outgoing) == []
    # ... but a wider window catches it
    assert len(detect_sweeps(SUBJECT, incoming, outgoing, max_delay=timedelta(days=5))) == 1


def test_one_forward_cannot_sweep_two_deposits() -> None:
    incoming = [
        movement(1, 1_000, NOW, incoming=True),
        movement(2, 1_000, NOW + timedelta(minutes=1), incoming=True),
    ]
    outgoing = [movement(3, 999, NOW + timedelta(minutes=5), incoming=False)]
    assert len(find_sweep_matches(incoming, outgoing)) == 1


def test_confidence_rewards_speed_and_completeness() -> None:
    incoming = [movement(1, 1_000, NOW, incoming=True)]
    fast = detect_sweeps(SUBJECT, incoming, [movement(2, 999, NOW, incoming=False)])[0]
    slow = detect_sweeps(
        SUBJECT, incoming, [movement(3, 960, NOW + timedelta(hours=20), incoming=False)]
    )[0]
    assert fast.confidence > slow.confidence
    assert fast.confidence < 1.0  # a heuristic is never certainty


def test_repeated_sweeps_aggregate_into_one_finding() -> None:
    """A relay wallet can sweep hundreds of times. One finding per occurrence
    would bury every other signal in the report, so the behaviour is stated
    once with the count as evidence of scale."""
    incoming = [
        movement(1, 1_000, NOW, incoming=True),
        movement(3, 900, NOW + timedelta(hours=2), incoming=True),
    ]
    outgoing = [
        movement(2, 990, NOW + timedelta(minutes=10), incoming=False),
        movement(4, 895, NOW + timedelta(hours=2, minutes=10), incoming=False),
    ]
    found = detect_sweeps(SUBJECT, incoming, outgoing)
    assert len(found) == 1
    assert "2 receive-and-forward cycles" in found[0].summary
    # both occurrences are still cited as evidence
    fact = next(e for e in found[0].evidence if e.kind is EvidenceKind.ONCHAIN_FACT)
    assert "2 matched receive/forward pair(s)" in fact.summary
    assert {"0x0001", "0x0002", "0x0003", "0x0004"} <= set(fact.refs)


def test_receive_and_forward_in_one_transaction_is_not_a_sweep() -> None:
    """Value in and out within ONE atomic transaction is a swap or router
    hop, not a wallet holding funds and passing them on. Counting it
    produced self-referential evidence ('received in X, forwarded in X')."""
    same_tx_in = StoredMovement(
        id=1,
        transaction_id=42,
        chain="ethereum",
        tx_hash="0xsame",
        from_address_id=None,
        to_address_id=99,
        kind="native",
        asset_id=1,
        amount=1_000,
        timestamp=NOW,
    )
    same_tx_out = StoredMovement(
        id=2,
        transaction_id=42,
        chain="ethereum",
        tx_hash="0xsame",
        from_address_id=99,
        to_address_id=None,
        kind="native",
        asset_id=1,
        amount=1_000,
        timestamp=NOW,
    )
    assert detect_sweeps(SUBJECT, [same_tx_in], [same_tx_out]) == []

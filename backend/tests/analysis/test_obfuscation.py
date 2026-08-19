"""Structural obfuscation detectors.

Half of these tests assert what must NOT fire. For a forensics tool, a
false positive is an accusation, so the negative cases carry more weight
than the positive ones.
"""

from datetime import UTC, datetime, timedelta

from cipherchain.analysis.heuristics.obfuscation import (
    DISTRIBUTION_HEURISTIC,
    EQUAL_SPLIT_HEURISTIC,
    FAN_IN_HEURISTIC,
    FAN_OUT_HEURISTIC,
    PEEL_HEURISTIC,
    RAPID_HOP_HEURISTIC,
    detect_distribution,
    detect_fan_in,
    detect_peel_chain,
    detect_rapid_hop,
)
from cipherchain.core.models import Address, EvidenceKind
from cipherchain.storage.repositories import StoredMovement

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
SUBJECT = Address("ethereum", "0xsubject")


def mv(
    i: int,
    amount: int,
    when: datetime,
    *,
    incoming: bool,
    asset_id: int = 1,
    peer: int | None = None,
) -> StoredMovement:
    return StoredMovement(
        id=i,
        transaction_id=i,
        chain="ethereum",
        tx_hash=f"0x{i:04x}",
        from_address_id=(peer if peer is not None else 900 + i) if incoming else 1,
        to_address_id=1 if incoming else (peer if peer is not None else 900 + i),
        kind="native",
        asset_id=asset_id,
        amount=amount,
        timestamp=when,
    )


def heuristics(findings) -> set[str]:
    return {
        e.heuristic
        for f in findings
        for e in f.evidence
        if e.kind is EvidenceKind.HEURISTIC_INFERENCE
    }


class TestPeelChain:
    def test_detects_large_remainder_plus_small_slice(self) -> None:
        incoming = [mv(1, 1000, NOW, incoming=True)]
        outgoing = [
            mv(2, 850, NOW + timedelta(minutes=5), incoming=False),  # remainder 85%
            mv(3, 140, NOW + timedelta(minutes=6), incoming=False),  # slice 14%
        ]
        found = detect_peel_chain(SUBJECT, incoming, outgoing)
        assert len(found) == 1
        assert "peel" in found[0].summary
        assert heuristics(found) == {PEEL_HEURISTIC}

    def test_even_split_is_not_a_peel(self) -> None:
        # 50/50 is a split, not a peel — the bulk must keep moving
        incoming = [mv(1, 1000, NOW, incoming=True)]
        outgoing = [
            mv(2, 500, NOW + timedelta(minutes=5), incoming=False),
            mv(3, 500, NOW + timedelta(minutes=6), incoming=False),
        ]
        assert detect_peel_chain(SUBJECT, incoming, outgoing) == []

    def test_single_outflow_is_not_a_peel(self) -> None:
        incoming = [mv(1, 1000, NOW, incoming=True)]
        outgoing = [mv(2, 900, NOW + timedelta(minutes=5), incoming=False)]
        assert detect_peel_chain(SUBJECT, incoming, outgoing) == []

    def test_outflow_before_inflow_is_not_a_peel(self) -> None:
        incoming = [mv(1, 1000, NOW, incoming=True)]
        outgoing = [
            mv(2, 850, NOW - timedelta(hours=2), incoming=False),
            mv(3, 140, NOW - timedelta(hours=1), incoming=False),
        ]
        assert detect_peel_chain(SUBJECT, incoming, outgoing) == []

    def test_different_asset_is_not_a_peel(self) -> None:
        incoming = [mv(1, 1000, NOW, incoming=True, asset_id=1)]
        outgoing = [
            mv(2, 850, NOW + timedelta(minutes=5), incoming=False, asset_id=2),
            mv(3, 140, NOW + timedelta(minutes=6), incoming=False, asset_id=2),
        ]
        assert detect_peel_chain(SUBJECT, incoming, outgoing) == []


class TestDistribution:
    """One finding per address for the outflow axis — the shape picks the
    headline. Run as two detectors this doubled every batch payout."""

    def test_identical_values_to_many_recipients_is_batch_distribution(self) -> None:
        outgoing = [mv(i, 100, NOW, incoming=False, peer=500 + i) for i in range(10)]
        found = detect_distribution(SUBJECT, [], outgoing)
        assert len(found) == 1  # ONE finding, not a splitter + equal-split pair
        assert "batch distribution" in found[0].summary
        assert "10 outputs of identical value across 10 distinct addresses" in found[0].summary
        assert heuristics(found) == {DISTRIBUTION_HEURISTIC}

    def test_varied_values_to_many_recipients_is_a_splitter(self) -> None:
        outgoing = [mv(i, 100 + i, NOW, incoming=False, peer=500 + i) for i in range(10)]
        found = detect_distribution(SUBJECT, [], outgoing)
        assert len(found) == 1
        assert "splitter: value distributed to 10 distinct addresses" in found[0].summary
        assert heuristics(found) == {FAN_OUT_HEURISTIC}

    def test_minority_equal_subset_is_noted_not_a_second_finding(self) -> None:
        varied = [mv(i, 100 + i, NOW, incoming=False, peer=500 + i) for i in range(20)]
        equal = [mv(50 + i, 7777, NOW, incoming=False, peer=600 + i) for i in range(4)]
        found = detect_distribution(SUBJECT, [], varied + equal)
        assert len(found) == 1
        assert "splitter" in found[0].summary
        inference = next(e for e in found[0].evidence if e.kind is EvidenceKind.HEURISTIC_INFERENCE)
        assert "includes 4 outputs of identical value" in inference.summary

    def test_identical_outputs_to_few_recipients_is_equal_split(self) -> None:
        outgoing = [mv(i, 1_000_000, NOW, incoming=False, peer=700 + i) for i in range(5)]
        found = detect_distribution(SUBJECT, [], outgoing)
        assert len(found) == 1
        assert "5 outputs of identical value" in found[0].summary
        assert heuristics(found) == {EQUAL_SPLIT_HEURISTIC}

    def test_ordinary_payment_activity_does_not_fire(self) -> None:
        outgoing = [mv(i, 100 + i, NOW, incoming=False, peer=500 + i) for i in range(3)]
        assert detect_distribution(SUBJECT, [], outgoing) == []

    def test_repeat_payments_to_one_peer_do_not_fire_fan_out(self) -> None:
        # 20 varied payments but only ONE counterparty: not a splitter
        outgoing = [mv(i, 100 + i, NOW, incoming=False, peer=777) for i in range(20)]
        assert detect_distribution(SUBJECT, [], outgoing) == []

    def test_fan_in_fires_on_many_senders(self) -> None:
        incoming = [mv(i, 100, NOW, incoming=True, peer=600 + i) for i in range(9)]
        found = detect_fan_in(SUBJECT, incoming, [])
        assert len(found) == 1
        assert heuristics(found) == {FAN_IN_HEURISTIC}

    def test_finding_names_the_benign_lookalike(self) -> None:
        """The evidence must state what legitimate behaviour looks the same."""
        outgoing = [mv(i, 100 + i, NOW, incoming=False, peer=500 + i) for i in range(10)]
        inference = next(
            e
            for e in detect_distribution(SUBJECT, [], outgoing)[0].evidence
            if e.kind is EvidenceKind.HEURISTIC_INFERENCE
        )
        assert "batch payouts" in inference.summary

    def test_never_names_a_specific_mixing_protocol(self) -> None:
        """The chain cannot show WHICH protocol was used, only the shape."""
        outgoing = [mv(i, 999, NOW, incoming=False, peer=700 + i) for i in range(5)]
        text = " ".join(
            f.summary + " " + " ".join(e.summary for e in f.evidence)
            for f in detect_distribution(SUBJECT, [], outgoing)
        ).lower()
        for protocol in ("wasabi", "samourai", "whirlpool", "joinmarket", "tornado"):
            assert protocol not in text


class TestRapidHop:
    def test_repeated_quick_relays_fire(self) -> None:
        incoming, outgoing = [], []
        for i in range(4):
            t = NOW + timedelta(hours=i)
            incoming.append(mv(100 + i, 500, t, incoming=True))
            outgoing.append(mv(200 + i, 490, t + timedelta(minutes=3), incoming=False))
        found = detect_rapid_hop(SUBJECT, incoming, outgoing)
        assert len(found) == 1
        assert heuristics(found) == {RAPID_HOP_HEURISTIC}

    def test_slow_movement_does_not_fire(self) -> None:
        # value rests ~12h before moving, and no outflow ever lands within
        # the 30-minute window of any inflow
        incoming, outgoing = [], []
        for i in range(4):
            t = NOW + timedelta(days=i)
            incoming.append(mv(100 + i, 500, t, incoming=True))
            outgoing.append(mv(200 + i, 490, t + timedelta(hours=12), incoming=False))
        assert detect_rapid_hop(SUBJECT, incoming, outgoing) == []

    def test_single_relay_does_not_fire(self) -> None:
        incoming = [mv(1, 500, NOW, incoming=True)]
        outgoing = [mv(2, 490, NOW + timedelta(minutes=2), incoming=False)]
        assert detect_rapid_hop(SUBJECT, incoming, outgoing) == []


def test_every_detector_stays_below_certainty() -> None:
    """No structural inference may ever be presented as certain."""
    outgoing = [mv(i, 1_000, NOW, incoming=False, peer=800 + i) for i in range(12)]
    incoming = [mv(500 + i, 1_000, NOW, incoming=True, peer=900 + i) for i in range(12)]
    for detector in (detect_distribution, detect_fan_in):
        for f in detector(SUBJECT, incoming, outgoing):
            assert f.confidence < 1.0
            for e in f.evidence:
                if e.confidence is not None:
                    assert e.confidence < 1.0


def test_detectors_are_deterministic() -> None:
    """Same input, byte-identical findings — required for replay."""
    outgoing = [mv(i, 1_000, NOW, incoming=False, peer=800 + i) for i in range(10)]
    assert detect_distribution(SUBJECT, [], outgoing) == detect_distribution(SUBJECT, [], outgoing)

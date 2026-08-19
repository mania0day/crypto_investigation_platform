"""Canonical model invariants: what must not be constructible, isn't."""

from datetime import UTC, datetime

import pytest

from cipherchain.core.models import (
    Address,
    Asset,
    AssetKind,
    Evidence,
    EvidenceKind,
    Finding,
    FindingKind,
    Movement,
    MovementKind,
    Provenance,
    TxRef,
)

NOW = datetime(2026, 8, 7, 12, 0, 0, tzinfo=UTC)
SHA = "a" * 64
PROV = Provenance(provider="test", retrieved_at=NOW, payload_sha256=SHA)


def eth_tx() -> TxRef:
    return TxRef(chain="ethereum", tx_hash="0xabc", timestamp=NOW, block_number=1)


def eth_asset() -> Asset:
    return Asset(chain="ethereum", kind=AssetKind.NATIVE, symbol="ETH", decimals=18)


class TestAsset:
    def test_token_requires_contract(self) -> None:
        with pytest.raises(ValueError, match="contract"):
            Asset(chain="ethereum", kind=AssetKind.TOKEN, symbol="USDT", decimals=6)

    def test_native_forbids_contract(self) -> None:
        with pytest.raises(ValueError, match="contract"):
            Asset(
                chain="ethereum",
                kind=AssetKind.NATIVE,
                symbol="ETH",
                decimals=18,
                contract="0x1",
            )

    def test_asset_is_hashable(self) -> None:
        assert len({eth_asset(), eth_asset()}) == 1


class TestMovement:
    def test_account_movement_valid(self) -> None:
        m = Movement(
            tx=eth_tx(),
            asset=eth_asset(),
            amount=10**18,
            kind=MovementKind.NATIVE,
            from_address=Address("ethereum", "0xfrom"),
            to_address=Address("ethereum", "0xto"),
            index=0,
            provenance=PROV,
        )
        assert m.amount == 10**18

    def test_account_movement_requires_both_endpoints(self) -> None:
        with pytest.raises(ValueError, match="both endpoints"):
            Movement(
                tx=eth_tx(),
                asset=eth_asset(),
                amount=1,
                kind=MovementKind.NATIVE,
                from_address=Address("ethereum", "0xfrom"),
                to_address=None,
                index=0,
                provenance=PROV,
            )

    def test_utxo_input_shape(self) -> None:
        btc_tx = TxRef(chain="bitcoin", tx_hash="f00d", timestamp=NOW)
        btc = Asset(chain="bitcoin", kind=AssetKind.NATIVE, symbol="BTC", decimals=8)
        m = Movement(
            tx=btc_tx,
            asset=btc,
            amount=5000,
            kind=MovementKind.UTXO_INPUT,
            from_address=Address("bitcoin", "bc1qxyz"),
            to_address=None,
            index=0,
            provenance=PROV,
        )
        assert m.to_address is None
        with pytest.raises(ValueError, match="utxo_input"):
            Movement(
                tx=btc_tx,
                asset=btc,
                amount=5000,
                kind=MovementKind.UTXO_INPUT,
                from_address=None,
                to_address=Address("bitcoin", "bc1qxyz"),
                index=0,
                provenance=PROV,
            )

    def test_negative_amount_rejected(self) -> None:
        with pytest.raises(ValueError, match="amount"):
            Movement(
                tx=eth_tx(),
                asset=eth_asset(),
                amount=-1,
                kind=MovementKind.NATIVE,
                from_address=Address("ethereum", "0xa"),
                to_address=Address("ethereum", "0xb"),
                index=0,
                provenance=PROV,
            )

    def test_cross_chain_endpoint_rejected(self) -> None:
        with pytest.raises(ValueError, match="chain"):
            Movement(
                tx=eth_tx(),
                asset=eth_asset(),
                amount=1,
                kind=MovementKind.NATIVE,
                from_address=Address("bitcoin", "bc1qxyz"),
                to_address=Address("ethereum", "0xb"),
                index=0,
                provenance=PROV,
            )


class TestEvidenceTaxonomy:
    def test_fact_requires_refs_and_forbids_confidence(self) -> None:
        Evidence(kind=EvidenceKind.ONCHAIN_FACT, summary="tx exists", refs=("0xabc",))
        with pytest.raises(ValueError, match="refs"):
            Evidence(kind=EvidenceKind.ONCHAIN_FACT, summary="tx exists")
        with pytest.raises(ValueError, match="confidence"):
            Evidence(
                kind=EvidenceKind.ONCHAIN_FACT,
                summary="tx exists",
                refs=("0xabc",),
                confidence=0.9,
            )

    def test_inference_requires_versioned_heuristic_and_confidence(self) -> None:
        Evidence(
            kind=EvidenceKind.HEURISTIC_INFERENCE,
            summary="sweep pattern",
            heuristic="sweep@1",
            confidence=0.7,
        )
        with pytest.raises(ValueError, match="name@version"):
            Evidence(
                kind=EvidenceKind.HEURISTIC_INFERENCE,
                summary="sweep pattern",
                heuristic="sweep",
                confidence=0.7,
            )
        with pytest.raises(ValueError, match="confidence"):
            Evidence(
                kind=EvidenceKind.HEURISTIC_INFERENCE,
                summary="sweep pattern",
                heuristic="sweep@1",
            )

    def test_claim_requires_source_and_confidence(self) -> None:
        Evidence(
            kind=EvidenceKind.THIRD_PARTY_CLAIM,
            summary="labeled as exchange",
            source="ofac-sdn@2026-08-01",
            confidence=0.95,
        )
        with pytest.raises(ValueError, match="source"):
            Evidence(
                kind=EvidenceKind.THIRD_PARTY_CLAIM,
                summary="labeled as exchange",
                confidence=0.95,
            )

    def test_confidence_bounds(self) -> None:
        with pytest.raises(ValueError, match="confidence"):
            Evidence(
                kind=EvidenceKind.THIRD_PARTY_CLAIM,
                summary="x",
                source="s",
                confidence=1.5,
            )


class TestFinding:
    def test_finding_requires_evidence(self) -> None:
        with pytest.raises(ValueError, match="evidence"):
            Finding(
                kind=FindingKind.VASP_ENDPOINT,
                subject=Address("ethereum", "0xa"),
                summary="exchange endpoint",
                confidence=0.9,
                evidence=(),
            )

    def test_finding_valid(self) -> None:
        f = Finding(
            kind=FindingKind.VASP_ENDPOINT,
            subject=Address("ethereum", "0xa"),
            summary="exchange endpoint",
            confidence=0.9,
            evidence=(
                Evidence(
                    kind=EvidenceKind.THIRD_PARTY_CLAIM,
                    summary="labeled",
                    source="seed@2026-08-07",
                    confidence=0.9,
                ),
            ),
        )
        assert f.kind is FindingKind.VASP_ENDPOINT

"""Contract conformance for the frozen Chain SDK base."""

from datetime import UTC, datetime

import pytest

from cipherchain.chains.base import (
    BridgeDirection,
    BridgeHint,
    ChainAdapter,
    ChainRegistry,
    ChainTransaction,
    HistoryPage,
    NormalizedTransaction,
    TimeWindow,
)
from cipherchain.core.errors import CapabilityNotSupported, UnknownChain
from cipherchain.core.models import Address, Capability, Provenance, TxRef

NOW = datetime(2026, 8, 7, tzinfo=UTC)
PROV = Provenance(provider="stub", retrieved_at=NOW, payload_sha256="a" * 64)


class StubAdapter(ChainAdapter):
    chain = "stubchain"

    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.TX_LOOKUP, Capability.ADDRESS_HISTORY})

    async def address_history(
        self,
        address: Address,
        *,
        window: TimeWindow | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> HistoryPage:
        return HistoryPage(items=(), next_cursor=None)

    async def transaction(self, tx_hash: str) -> ChainTransaction:
        return ChainTransaction(chain=self.chain, tx_hash=tx_hash, raw={}, provenance=PROV)

    async def normalize(self, tx: ChainTransaction) -> NormalizedTransaction:
        return NormalizedTransaction(
            tx=TxRef(chain=tx.chain, tx_hash=tx.tx_hash, timestamp=NOW), movements=()
        )


def test_capability_discovery_and_require() -> None:
    adapter = StubAdapter()
    assert adapter.supports(Capability.TX_LOOKUP)
    assert not adapter.supports(Capability.INTERNAL_TRACES)
    with pytest.raises(CapabilityNotSupported):
        adapter.require(Capability.INTERNAL_TRACES)


def test_registry_round_trip_and_unknown_chain() -> None:
    registry = ChainRegistry()
    adapter = StubAdapter()
    registry.register(adapter)
    assert registry.get("stubchain") is adapter
    assert registry.chains() == ("stubchain",)
    with pytest.raises(UnknownChain):
        registry.get("atlantis")
    with pytest.raises(ValueError, match="duplicate"):
        registry.register(StubAdapter())


def test_time_window_validation() -> None:
    with pytest.raises(ValueError, match="start"):
        TimeWindow(start=NOW, end=datetime(2020, 1, 1, tzinfo=UTC))


def test_normalized_transaction_rejects_foreign_movements() -> None:
    from cipherchain.core.models import Asset, AssetKind, Movement, MovementKind

    tx = TxRef(chain="stubchain", tx_hash="0xaaa", timestamp=NOW)
    foreign = Movement(
        tx=TxRef(chain="stubchain", tx_hash="0xbbb", timestamp=NOW),
        asset=Asset(chain="stubchain", kind=AssetKind.NATIVE, symbol="STB", decimals=8),
        amount=1,
        kind=MovementKind.NATIVE,
        from_address=Address("stubchain", "s1"),
        to_address=Address("stubchain", "s2"),
        index=0,
        provenance=PROV,
    )
    with pytest.raises(ValueError, match="different transaction"):
        NormalizedTransaction(tx=tx, movements=(foreign,))


def test_bridge_hint_requires_evidence() -> None:
    tx = TxRef(chain="stubchain", tx_hash="0xaaa", timestamp=NOW)
    with pytest.raises(ValueError, match="evidence"):
        BridgeHint(
            bridge_id="wormhole",
            direction=BridgeDirection.DEPOSIT,
            counterpart_chain=None,
            tx=tx,
            refs=(),
        )

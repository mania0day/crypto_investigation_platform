"""A synthetic chain with a known-answer graph.

The engine is chain-agnostic, so a fake adapter is a *legitimate* test
subject, not a shortcut: it implements the same frozen contract and lets us
assert exact traversal outcomes against a ground truth.

Default graph (money flows left to right):

    exchange_in → funder → ROOT → cashout → exchange_out

so ``find_prev_vasp`` must reach ``exchange_in`` and ``find_next_vasp``
must reach ``exchange_out``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from cipherchain.chains.base import (
    ChainAdapter,
    ChainRegistry,
    ChainTransaction,
    HistoryPage,
    NormalizedTransaction,
    TimeWindow,
)
from cipherchain.core.models import (
    Address,
    Asset,
    AssetKind,
    Capability,
    Movement,
    MovementKind,
    Provenance,
    TxRef,
)
from cipherchain.investigation.attribution import AttributionResult

CHAIN = "testchain"
ASSET = Asset(chain=CHAIN, kind=AssetKind.NATIVE, symbol="TST", decimals=8)
GENESIS = datetime(2026, 1, 1, tzinfo=UTC)

ROOT = "root"
FUNDER = "funder"
EXCHANGE_IN = "exchange_in"
CASHOUT = "cashout"
EXCHANGE_OUT = "exchange_out"


@dataclass
class Hop:
    """One transfer in the synthetic ledger."""

    src: str
    dst: str
    amount: int
    day: int
    tx: str
    # Contract address when the hop moves a TOKEN; None means the chain's
    # native asset. Lets a ledger reproduce the asset-forgery attack, where a
    # contract emits transfers between addresses that never signed anything.
    token: str | None = None


DEFAULT_LEDGER: tuple[Hop, ...] = (
    Hop(EXCHANGE_IN, FUNDER, 1_000, 1, "tx_ex_in"),
    Hop(FUNDER, ROOT, 900, 2, "tx_fund"),
    Hop(ROOT, CASHOUT, 800, 3, "tx_out"),
    Hop(CASHOUT, EXCHANGE_OUT, 700, 4, "tx_ex_out"),
)


@dataclass
class FakeAdapter(ChainAdapter):
    """Serves the synthetic ledger through the frozen contract."""

    chain: str = CHAIN
    ledger: tuple[Hop, ...] = DEFAULT_LEDGER
    history_calls: list[str] = field(default_factory=list)

    def capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.ADDRESS_HISTORY, Capability.TX_LOOKUP})

    def recognizes(self, address: str) -> bool:
        # The synthetic ledger uses bare words ("root", "exchange_in"). Without
        # this the fake never claims an address and the API's chain resolution
        # cannot be exercised at all.
        return bool(re.fullmatch(r"[a-z][a-z0-9_]*", address.strip()))

    def _hops_for(self, address_value: str) -> Iterable[Hop]:
        return [h for h in self.ledger if address_value in (h.src, h.dst)]

    def _chain_tx(self, hop: Hop) -> ChainTransaction:
        return ChainTransaction(
            chain=self.chain,
            tx_hash=hop.tx,
            raw=hop,
            provenance=Provenance(
                provider="fake",
                retrieved_at=GENESIS,
                payload_sha256=f"{hop.tx:_<64}"[:64].replace(" ", "0"),
            ),
        )

    async def address_history(
        self,
        address: Address,
        *,
        window: TimeWindow | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> HistoryPage:
        self.history_calls.append(address.value)
        hops = sorted(self._hops_for(address.value), key=lambda h: -h.day)
        return HistoryPage(items=tuple(self._chain_tx(h) for h in hops[:limit]), next_cursor=None)

    async def transaction(self, tx_hash: str) -> ChainTransaction:
        hop = next(h for h in self.ledger if h.tx == tx_hash)
        return self._chain_tx(hop)

    async def normalize(self, tx: ChainTransaction) -> NormalizedTransaction:
        hop: Hop = tx.raw  # type: ignore[assignment]
        tx_ref = TxRef(
            chain=self.chain,
            tx_hash=hop.tx,
            timestamp=GENESIS + timedelta(days=hop.day),
            block_number=hop.day,
        )
        # Asset follows the adapter's chain, not a fixed constant, so the
        # fake stays valid when instantiated for any chain id.
        if hop.token is not None:
            asset = Asset(
                chain=self.chain,
                kind=AssetKind.TOKEN,
                symbol="TKN",
                decimals=6,
                contract=hop.token,
            )
            kind = MovementKind.TOKEN
        else:
            asset = (
                ASSET
                if self.chain == CHAIN
                else Asset(chain=self.chain, kind=AssetKind.NATIVE, symbol="TST", decimals=8)
            )
            kind = MovementKind.NATIVE
        return NormalizedTransaction(
            tx=tx_ref,
            movements=(
                Movement(
                    tx=tx_ref,
                    asset=asset,
                    amount=hop.amount,
                    kind=kind,
                    from_address=Address(self.chain, hop.src),
                    to_address=Address(self.chain, hop.dst),
                    index=0,
                    provenance=tx.provenance,
                ),
            ),
        )


class MapAttributor:
    """Label lookup over a dict — the Attributor port, no network."""

    def __init__(self, labels: dict[str, tuple[str, str]]) -> None:
        self._labels = labels
        self.calls: list[str] = []

    async def attribute(self, address: Address) -> tuple[AttributionResult, ...]:
        self.calls.append(address.value)
        entry = self._labels.get(address.value)
        if entry is None:
            return ()
        entity, category = entry
        return (
            AttributionResult(
                entity=entity,
                category=category,
                source="test-labels@2026-08-07",
                confidence=0.9,
                source_date=GENESIS,
            ),
        )


@pytest.fixture
def registry() -> tuple[ChainRegistry, FakeAdapter]:
    adapter = FakeAdapter()
    reg = ChainRegistry()
    reg.register(adapter)
    return reg, adapter


class FakeClock:
    """Clock that advances a fixed step per reading.

    Simulates wall time accumulating *during* a run, which is what the
    `seconds` budget actually guards — without any real waiting.
    """

    def __init__(self, step: float = 0.0) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        current = self.now
        self.now += self.step
        return current

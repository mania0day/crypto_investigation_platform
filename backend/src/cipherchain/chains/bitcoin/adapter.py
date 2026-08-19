"""Bitcoin chain adapter.

Data source: any esplora-shaped Class A provider routed by the pool
(mempool.space today; self-hosted instances are a base-URL change).
Transaction payloads embed prevout data on every input, so normalization
needs no extra lookups.

Normalization emits the UTXO halves of the canonical model:
- one ``utxo_input`` movement per spending input (from = prevout address),
- one ``utxo_output`` movement per paying output (to = output address).
Indexes follow the transaction's own input/output order, which is
immutable — re-normalization is byte-for-byte idempotent.

Declared absences (capability discovery): receipts, logs, token transfers,
and internal traces do not exist on Bitcoin.

Skeleton simplifications, stated:
- Unconfirmed transactions are excluded — they have no temporal anchor and
  can be replaced; evidence builds on confirmed data only.
- Inputs/outputs without a decodable address (op_return, exotic scripts)
  produce no movement half; the tx itself remains fully retrievable via
  its provenance digest.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, cast

from cipherchain.chains.base import (
    ChainAdapter,
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
    TxRef,
)
from cipherchain.providers.base import ProviderRequest
from cipherchain.providers.pool import ProviderPool

BTC_ASSET = Asset(chain="bitcoin", kind=AssetKind.NATIVE, symbol="BTC", decimals=8)

# Address formats. Base58 alphabet deliberately excludes 0, O, I and l.
_P2PKH_P2SH = re.compile(r"^[13][a-km-zA-HJ-NP-Z1-9]{25,34}$")
_BECH32 = re.compile(r"^(bc1|BC1)[a-zA-Z0-9]{11,71}$")

# esplora-style endpoints page confirmed history in blocks of 25.
_PAGE_HINT = 25


class BitcoinAdapter(ChainAdapter):
    chain = "bitcoin"

    def __init__(self, pool: ProviderPool) -> None:
        self._pool = pool

    def capabilities(self) -> frozenset[Capability]:
        return frozenset(
            {
                Capability.ADDRESS_HISTORY,
                Capability.TX_LOOKUP,
                Capability.UTXO_LOOKUP,
                Capability.BLOCK_LOOKUP,
            }
        )

    def recognizes(self, address: str) -> bool:
        return bool(_P2PKH_P2SH.match(address) or _BECH32.match(address))

    def canonical_address(self, address: str) -> str:
        # Bech32 is case-insensitive and canonically lowercase; Base58 is
        # case-SIGNIFICANT and must never be folded.
        value = address.strip()
        return value.lower() if _BECH32.match(value) else value

    async def address_history(
        self,
        address: Address,
        *,
        window: TimeWindow | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> HistoryPage:
        self.require(Capability.ADDRESS_HISTORY)
        params: dict[str, Any] = {"address": address.value}
        if cursor is not None:
            params["after_txid"] = cursor
        response = await self._pool.fetch(
            ProviderRequest(self.chain, Capability.ADDRESS_HISTORY, params)
        )
        payload = cast(list[dict[str, Any]], response.payload)
        confirmed = [tx for tx in payload if tx.get("status", {}).get("confirmed")]
        kept = (
            confirmed if window is None else [tx for tx in confirmed if self._in_window(tx, window)]
        )
        items = tuple(
            ChainTransaction(
                chain=self.chain,
                tx_hash=tx["txid"],
                raw=tx,
                provenance=response.provenance(),
            )
            for tx in kept[:limit]
        )
        # "More history exists" is a property of the PROVIDER's page, not of what
        # survived our filters. Reading it off the filtered list ended pagination
        # the moment a time window emptied a page — so a windowed trace reported
        # "exhausted" for transactions it had never fetched. The cursor must also
        # advance past the whole provider page, or the next request re-reads what
        # the window just discarded.
        if len(kept) > limit:
            next_cursor = str(items[-1].tx_hash) if items else None
        elif len(confirmed) >= _PAGE_HINT:
            next_cursor = str(confirmed[-1]["txid"])
        else:
            next_cursor = None
        return HistoryPage(items=items, next_cursor=next_cursor)

    async def transaction(self, tx_hash: str) -> ChainTransaction:
        response = await self._pool.fetch(
            ProviderRequest(self.chain, Capability.TX_LOOKUP, {"tx_hash": tx_hash})
        )
        payload = cast(dict[str, Any], response.payload)
        return ChainTransaction(
            chain=self.chain,
            tx_hash=payload["txid"],
            raw=payload,
            provenance=response.provenance(),
        )

    async def normalize(self, tx: ChainTransaction) -> NormalizedTransaction:
        raw = cast(dict[str, Any], tx.raw)
        status = raw.get("status", {})
        if not status.get("confirmed"):
            raise ValueError(f"refusing to normalize unconfirmed tx {tx.tx_hash}")
        tx_ref = TxRef(
            chain=self.chain,
            tx_hash=raw["txid"],
            timestamp=datetime.fromtimestamp(status["block_time"], tz=UTC),
            block_number=status.get("block_height"),
        )
        movements: list[Movement] = []
        # Index and identity come from the vin/vout POSITION, not a running
        # counter over emitted halves — otherwise skipping a coinbase or an
        # undecodable script shifts every later half's identity, so a provider
        # that decodes a different address subset would collide/drop rows on
        # re-normalization (REVIEW_FINDINGS.md #1). Positions are stable because
        # the full tx is always fetched.
        for position, vin in enumerate(raw.get("vin", [])):
            if vin.get("is_coinbase"):
                continue  # newly minted coins have no source address
            prevout = vin.get("prevout") or {}
            source = prevout.get("scriptpubkey_address")
            value = prevout.get("value")
            if source is None or value is None:
                continue  # non-address script: no half, tx stays replayable via digest
            movements.append(
                Movement(
                    tx=tx_ref,
                    asset=BTC_ASSET,
                    amount=int(value),
                    kind=MovementKind.UTXO_INPUT,
                    from_address=Address(self.chain, source),
                    to_address=None,
                    index=position,
                    provenance=tx.provenance,
                    dedup_key=f"in:{position}",
                )
            )
        for position, vout in enumerate(raw.get("vout", [])):
            target = vout.get("scriptpubkey_address")
            value = vout.get("value")
            if target is None or value is None:
                continue
            movements.append(
                Movement(
                    tx=tx_ref,
                    asset=BTC_ASSET,
                    amount=int(value),
                    kind=MovementKind.UTXO_OUTPUT,
                    from_address=None,
                    to_address=Address(self.chain, target),
                    index=position,
                    provenance=tx.provenance,
                    dedup_key=f"out:{position}",
                )
            )
        return NormalizedTransaction(tx=tx_ref, movements=tuple(movements))

    @staticmethod
    def _in_window(tx: dict[str, Any], window: TimeWindow) -> bool:
        block_time = tx.get("status", {}).get("block_time")
        if block_time is None:
            return False
        when = datetime.fromtimestamp(block_time, tz=UTC)
        if window.start is not None and when < window.start:
            return False
        return not (window.end is not None and when > window.end)

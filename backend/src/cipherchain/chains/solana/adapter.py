"""Solana chain adapter — the third execution paradigm.

Solana transactions do not state "A sent X to B". They list the accounts a
transaction touched plus their balances **before and after**, and the value
movement is the difference. Normalization therefore *derives* movements from
balance deltas rather than reading them off the payload.

That shape is a natural fit for the canonical model's UTXO halves: accounts
whose balance fell are inputs, accounts whose balance rose are outputs, and
the joining transaction supplies the counterparties. This is why the engine
needed no change to support a third paradigm — the frontier already resolves
counterparties through a shared transaction.

Correctness rules encoded here:
- **Fees are not value movement.** The fee payer's balance always falls by
  the fee; that amount is added back before computing its delta, or every
  transaction would look like a payment to nobody.
- **Failed transactions move nothing** (``meta.err`` non-null).
- **Token deltas are per (account, mint)**, so several SPL tokens in one
  transaction stay separate assets.
- Index and dedup key come from the account's position in the transaction,
  which is intrinsic and stable across acquisitions.

Stated simplification: history is a signature list plus one fetch per
signature (Solana has no batched history endpoint), so a Solana expansion
costs more provider calls than an EVM one. The pool cache absorbs repeats.
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

SOL_ASSET = Asset(chain="solana", kind=AssetKind.NATIVE, symbol="SOL", decimals=9)

# Base58 (no 0, O, I, l). A pubkey is 32 bytes, which encodes to 43-44
# characters for any value that isn't dominated by leading zero bytes — i.e.
# every real wallet. Deliberately NOT widened to 32-42: that range collides
# with Bitcoin's legacy format (25-34 chars), which would make every legacy
# Bitcoin address ambiguous and force the investigator to disambiguate on
# the common path. Short all-ones system addresses (e.g. the System Program)
# therefore need an explicit chain — they are program IDs, not wallets a
# trace follows.
_BASE58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{43,44}$")


class SolanaAdapter(ChainAdapter):
    chain = "solana"

    def __init__(self, pool: ProviderPool, *, page_limit: int = 25) -> None:
        self._pool = pool
        self._page_limit = page_limit

    def capabilities(self) -> frozenset[Capability]:
        return frozenset(
            {
                Capability.ADDRESS_HISTORY,
                Capability.TX_LOOKUP,
                Capability.BALANCE,
                Capability.BLOCK_LOOKUP,
            }
        )

    def recognizes(self, address: str) -> bool:
        return bool(_BASE58.match(address.strip()))

    def canonical_address(self, address: str) -> str:
        return address.strip()  # Base58 is case-significant — never fold it

    async def address_history(
        self,
        address: Address,
        *,
        window: TimeWindow | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> HistoryPage:
        self.require(Capability.ADDRESS_HISTORY)
        params: dict[str, Any] = {
            "address": address.value,
            "limit": min(limit, self._page_limit),
        }
        if cursor:
            params["before"] = cursor
        listing = await self._pool.fetch(
            ProviderRequest(self.chain, Capability.ADDRESS_HISTORY, params)
        )
        entries = cast(list[dict[str, Any]], listing.payload)
        # Only confirmed, successful signatures: an errored transaction moved
        # no value, and an unconfirmed one has no temporal anchor.
        signatures = [
            e["signature"]
            for e in entries
            if e.get("err") is None and e.get("blockTime") is not None
        ]
        if window is not None:
            signatures = [
                e["signature"]
                for e in entries
                if e.get("err") is None
                and e.get("blockTime") is not None
                and self._in_window(int(e["blockTime"]), window)
            ]

        items: list[ChainTransaction] = []
        for signature in signatures:
            items.append(await self.transaction(signature))
        next_cursor = entries[-1]["signature"] if len(entries) >= params["limit"] else None
        return HistoryPage(items=tuple(items), next_cursor=next_cursor)

    async def transaction(self, tx_hash: str) -> ChainTransaction:
        response = await self._pool.fetch(
            ProviderRequest(self.chain, Capability.TX_LOOKUP, {"tx_hash": tx_hash})
        )
        payload = cast(dict[str, Any], response.payload)
        return ChainTransaction(
            chain=self.chain,
            tx_hash=tx_hash,
            raw=payload,
            provenance=response.provenance(),
        )

    async def normalize(self, tx: ChainTransaction) -> NormalizedTransaction:
        raw = cast(dict[str, Any], tx.raw)
        meta = raw.get("meta") or {}
        block_time = raw.get("blockTime")
        if block_time is None:
            raise ValueError(f"refusing to normalize unconfirmed tx {tx.tx_hash}")
        tx_ref = TxRef(
            chain=self.chain,
            tx_hash=tx.tx_hash,
            timestamp=datetime.fromtimestamp(int(block_time), tz=UTC),
            block_number=raw.get("slot"),
        )
        if meta.get("err") is not None:
            return NormalizedTransaction(tx=tx_ref, movements=())  # failed: no value moved

        movements: list[Movement] = []
        movements.extend(self._native_movements(tx, tx_ref, raw, meta))
        movements.extend(self._token_movements(tx, tx_ref, meta))
        return NormalizedTransaction(tx=tx_ref, movements=tuple(movements))

    # ── native SOL, from lamport deltas ──────────────────────────────────

    def _native_movements(
        self,
        tx: ChainTransaction,
        tx_ref: TxRef,
        raw: dict[str, Any],
        meta: dict[str, Any],
    ) -> list[Movement]:
        pre = meta.get("preBalances") or []
        post = meta.get("postBalances") or []
        keys = self._account_keys(raw)
        if not (len(pre) == len(post) == len(keys)):
            return []  # malformed: never guess at value
        fee = int(meta.get("fee") or 0)
        movements: list[Movement] = []
        for index, (before, after, account) in enumerate(zip(pre, post, keys, strict=True)):
            delta = int(after) - int(before)
            if index == 0:
                delta += fee  # the fee is a cost, not a transfer
            if delta == 0:
                continue
            movements.append(
                self._half(tx, tx_ref, SOL_ASSET, delta, account, index, f"sol:{index}")
            )
        return movements

    # ── SPL tokens, from per-(account, mint) deltas ──────────────────────

    def _token_movements(
        self, tx: ChainTransaction, tx_ref: TxRef, meta: dict[str, Any]
    ) -> list[Movement]:
        def index_by(entries: list[dict[str, Any]]) -> dict[tuple[int, str], dict[str, Any]]:
            return {(int(e["accountIndex"]), str(e["mint"])): e for e in entries}

        pre = index_by(meta.get("preTokenBalances") or [])
        post = index_by(meta.get("postTokenBalances") or [])
        movements: list[Movement] = []
        for key in sorted(set(pre) | set(post)):
            account_index, mint = key
            before = self._token_amount(pre.get(key))
            after = self._token_amount(post.get(key))
            delta = after - before
            if delta == 0:
                continue
            entry = post.get(key) or pre.get(key) or {}
            owner = entry.get("owner")
            if not owner:
                continue  # without an owner there is no address to attribute
            decimals = int((entry.get("uiTokenAmount") or {}).get("decimals", 0))
            asset = Asset(
                chain=self.chain,
                kind=AssetKind.TOKEN,
                symbol="SPL",  # display metadata is enrichment, never guessed
                decimals=decimals,
                contract=mint,
            )
            movements.append(
                self._half(
                    tx, tx_ref, asset, delta, owner, account_index, f"spl:{account_index}:{mint}"
                )
            )
        return movements

    # ── shared ───────────────────────────────────────────────────────────

    def _half(
        self,
        tx: ChainTransaction,
        tx_ref: TxRef,
        asset: Asset,
        delta: int,
        account: str,
        index: int,
        dedup_key: str,
    ) -> Movement:
        """A balance decrease is an input half; an increase is an output half."""
        outgoing = delta < 0
        return Movement(
            tx=tx_ref,
            asset=asset,
            amount=abs(delta),
            kind=MovementKind.UTXO_INPUT if outgoing else MovementKind.UTXO_OUTPUT,
            from_address=Address(self.chain, account) if outgoing else None,
            to_address=None if outgoing else Address(self.chain, account),
            index=index,
            provenance=tx.provenance,
            dedup_key=dedup_key,
        )

    @staticmethod
    def _account_keys(raw: dict[str, Any]) -> list[str]:
        """Account list, tolerating both payload shapes.

        ``getTransaction`` nests the keys under ``transaction.message``,
        while ``getBlock`` with ``transactionDetails=accounts`` puts them
        directly on ``transaction``. Entries are objects under jsonParsed
        encoding and bare strings otherwise.
        """
        transaction = raw.get("transaction") or {}
        keys = (transaction.get("message") or {}).get("accountKeys")
        if keys is None:
            keys = transaction.get("accountKeys") or []
        return [str(k.get("pubkey")) if isinstance(k, dict) else str(k) for k in keys]

    @staticmethod
    def _token_amount(entry: dict[str, Any] | None) -> int:
        if not entry:
            return 0
        return int((entry.get("uiTokenAmount") or {}).get("amount", 0))

    @staticmethod
    def _in_window(unix_time: int, window: TimeWindow) -> bool:
        when = datetime.fromtimestamp(unix_time, tz=UTC)
        if window.start is not None and when < window.start:
            return False
        return not (window.end is not None and when > window.end)

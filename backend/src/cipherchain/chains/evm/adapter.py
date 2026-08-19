"""EVM-family chain adapter.

One class serves every EVM chain: an :class:`EvmChainConfig` instance per
chain makes "more chains" configuration, not code (vision §6).

Two acquisition dialects, both normalized to the same canonical facts:

- **etherscan rows** (``address_history``): ``txlist`` + ``tokentx`` pages
  merged by tx hash. Rows carry timestamps, so no extra lookups are needed.
- **rpc objects** (``transaction``): tx + receipt + block header via Class B
  RPC. Token movements are decoded from ERC-20 ``Transfer`` logs in the
  receipt.

Correctness rules encoded here:
- addresses are lowercased at the boundary (canonical form),
- amounts are ints in smallest units, never floats,
- failed transactions (``isError``/receipt status 0) move no value,
- a token asset decoded from a bare log carries ``decimals=0`` and a
  placeholder symbol — amounts stay exact in smallest units; display
  metadata is later enrichment (asset registry), never guessed here,
- gas price is resolved ONCE per transaction and stamped on every movement
  that transaction contains (see :func:`_rpc_gas_price` for which field it
  reads and why). A gas price is a property of the transaction; a token
  transfer is a log inside one and carries no price of its own, so reading
  it per-movement would leave every token and internal movement null and
  strand the unique-gas-price mixer rung on native transfers alone.

Skeleton simplification, stated: history merges one ``txlist`` page with
one ``tokentx`` page per call; ``limit`` is the per-source page size, so a
page may contain up to 2x ``limit`` distinct transactions. Deep-pagination
merge refinement is deferred until the engine needs it.

Losing a feed (2026-08-16)
--------------------------
``address_history`` fans out into three provider calls, and each one can
come back ``AllProvidersFailed`` — a quota gone, a circuit open, the public
explorer under the fetch tier refusing to serve this host for the next hour.
One shared ``await`` chain meant the loss of any single feed raised, killed
the page, and with it the whole branch of the trace: one dead feed cost the
address.

So the two SECONDARY feeds are attempted independently and their failure
costs only their own rows — plus a :class:`FeedGap` on the page saying so.
``ADDRESS_HISTORY`` is different and stays fatal: with no native history
there is no page to build, and returning an empty one would tell the engine
"this address has nothing", which is the one answer this system must never
invent.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from cipherchain.chains.base import (
    BridgeDirection,
    BridgeHint,
    ChainAdapter,
    ChainTransaction,
    FeedGap,
    HistoryPage,
    NormalizedTransaction,
    TimeWindow,
)
from cipherchain.chains.bridges import BridgeRegistry
from cipherchain.chains.feeds import optional_feed
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
from cipherchain.providers.base import ProviderRequest, ProviderResponse
from cipherchain.providers.pool import ProviderPool

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
_EVM_ADDRESS = re.compile(r"^0x[a-fA-F0-9]{40}$")


def _receipt_succeeded(receipt: dict[str, Any]) -> bool:
    """Whether a receipt indicates success across encodings.

    Post-Byzantium receipts carry ``status`` (``0x1``/``0x0``, possibly
    zero-padded). Pre-Byzantium receipts (blocks < 4,370,000) have no
    ``status`` but do carry a state ``root`` — their inclusion means success.
    Treating a missing status as failure would silently drop every value
    movement of a successful historical transaction (REVIEW_FINDINGS.md #6).
    """
    status = receipt.get("status")
    if status is not None:
        try:
            return int(str(status), 16) == 1
        except ValueError:
            return False
    return receipt.get("root") is not None


def _erc20_amount(data: object) -> int | None:
    """Decode a standard ERC-20 Transfer amount: exactly one 32-byte word.

    Returns None for any non-canonical data (wrong length, non-hex) so the
    caller skips the log rather than storing a wrong value or overflowing the
    amount column — a contract can emit the Transfer topic with arbitrary
    data, so decoding the whole field was a value-corruption / DoS vector
    (REVIEW_FINDINGS.md #5). A 32-byte word is always within uint256.
    """
    text = str(data or "0x")
    body = text[2:] if text.startswith("0x") else text
    if len(body) != 64:  # a canonical Transfer carries exactly one uint256 word
        return None
    try:
        return int(body, 16)
    except ValueError:
        return None


def _hex_or_none(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value), 16)
    except ValueError:
        return None


def _gas_price(value: object) -> int | None:
    """Parse a gas price from either dialect's encoding, or None if unusable.

    RPC hands hex quantities (``"0x4a817c800"``), Etherscan decimal strings
    (``"20000000000"``); one parser reads both, so the SAME transaction seen
    through either vantage yields the same integer. That matters more than it
    looks: the rung that consumes this field matches on exact equality, so two
    encodings parsed by two rules would compare unequal and the rung would
    quietly never fire.

    Anything unparseable — empty string, null, a non-hex body — comes back None
    rather than a guess, and so does a negative value: no chain has a negative
    gas price, so it is a parse bug, and storing it would both fail the
    ``gas_price >= 0`` CHECK and (if two re-parses agreed on the same wrong
    number) read as a match between unrelated transactions. Unknown is a safe
    answer here; wrong is not.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        price = int(text, 16) if text.lower().startswith(("0x", "-0x")) else int(text, 10)
    except ValueError:
        return None
    return price if price >= 0 else None


def _rpc_gas_price(tx_obj: Mapping[str, Any], receipt: Mapping[str, Any]) -> int | None:
    """The price actually PAID per unit of gas, for an RPC-acquired tx.

    Order: the receipt's ``effectiveGasPrice`` first, the transaction's
    ``gasPrice`` second, and the EIP-1559 bid fields (``maxFeePerGas``,
    ``maxPriorityFeePerGas``) NEVER — which is the whole point of this
    function, so it is written down here.

    A post-London type-2 transaction does not name its price; it names a cap it
    is willing to pay and a tip it offers. What it actually paid is base fee +
    tip, which the receipt reports as ``effectiveGasPrice``. Etherscan's
    ``txlist``/``tokentx`` rows report that same paid price in their
    ``gasPrice`` column, so keying on the paid price is what lets a deposit
    normalized through RPC compare equal to a withdrawal normalized through
    Etherscan. Keying on ``maxFeePerGas`` instead would break that comparison
    silently — the two vantages would disagree about one transaction, the
    exact-equality rung would stop matching, and nothing would raise: the
    investigation would simply stop one link short with no sign anything was
    wrong. It is also the weaker signal on its own terms, since a cap is
    usually a wallet default while the paid price is not.

    Legacy transactions have no cap fields and pre-Byzantium receipts predate
    ``effectiveGasPrice`` entirely; the ``gasPrice`` fallback is for them.
    """
    effective = _gas_price(receipt.get("effectiveGasPrice"))
    if effective is not None:
        return effective
    return _gas_price(tx_obj.get("gasPrice"))


def _etherscan_gas_price(rows: Iterable[Mapping[str, Any] | None]) -> int | None:
    """The containing transaction's gas price, from whichever feed reported it.

    The three Etherscan feeds do not report it alike: ``txlist`` and
    ``tokentx`` rows both carry the transaction's ``gasPrice``,
    ``txlistinternal`` rows carry none at all. A transaction can also reach us
    through only one of the three — an internal-only transaction has no
    ``txlist`` row — so the price is taken from the first row that has one
    instead of from a privileged feed.

    All rows here belong to one transaction and therefore report one price;
    where a feed omits it, "the price this transfer paid" is still the
    transaction's price. Reading it per-row would leave contract-delivered
    value (the mixer withdrawal case) null, which is precisely the case the
    unique-gas-price rung exists to serve.
    """
    for row in rows:
        if row is None:
            continue
        price = _gas_price(row.get("gasPrice"))
        if price is not None:
            return price
    return None


def _token_key(sender: str, target: str, contract: str) -> str:
    """Vantage-stable identity for a token movement: same logical transfer,
    same key, regardless of which acquisition produced it."""
    return f"token:{sender}:{target}:{contract}"


def _internal_key(row: Mapping[str, Any], sender: str, target: str) -> str:
    """Vantage-stable identity for an internal transfer.

    ``traceId`` is the trace's intrinsic position inside its transaction, so
    two vantages of the same trace key identically. Where a provider omits it,
    fall back to the transfer's content — weaker only when one transaction
    contains several identical internal transfers between the same pair.
    """
    trace_id = str(row.get("traceId") or "").strip()
    if trace_id:
        return f"internal:{trace_id}"
    return f"internal:{sender}:{target}:{row.get('value', '0')}"


@dataclass(frozen=True, slots=True)
class EvmChainConfig:
    chain: str
    etherscan_chain_id: int
    native_symbol: str
    native_decimals: int = 18


ETHEREUM_CONFIG = EvmChainConfig(chain="ethereum", etherscan_chain_id=1, native_symbol="ETH")
# Adding an EVM chain is a config entry, not code — one Etherscan V2 key
# serves the whole family via ?chainid=.
POLYGON_CONFIG = EvmChainConfig(chain="polygon", etherscan_chain_id=137, native_symbol="POL")


class EvmAdapter(ChainAdapter):
    def __init__(
        self,
        config: EvmChainConfig,
        pool: ProviderPool,
        *,
        bridges: BridgeRegistry | None = None,
    ) -> None:
        self.chain = config.chain
        self._config = config
        self._pool = pool
        # Chain-local bridge contracts. Empty unless the operator supplies a
        # verified pack — CipherChain never guesses that an address is a bridge.
        self._bridges = (bridges or BridgeRegistry()).for_chain(config.chain)
        self._native = Asset(
            chain=config.chain,
            kind=AssetKind.NATIVE,
            symbol=config.native_symbol,
            decimals=config.native_decimals,
        )

    def capabilities(self) -> frozenset[Capability]:
        return frozenset(
            {
                Capability.ADDRESS_HISTORY,
                Capability.TOKEN_TRANSFERS,
                Capability.INTERNAL_TRACES,
                Capability.TX_LOOKUP,
                Capability.TX_RECEIPT,
                Capability.BLOCK_LOOKUP,
            }
        )

    def recognizes(self, address: str) -> bool:
        # Every EVM chain shares this format, so registering a second one
        # makes a bare 0x address ambiguous by design — the registry reports
        # both rather than picking a ledger for the investigator.
        return bool(_EVM_ADDRESS.match(address))

    def canonical_address(self, address: str) -> str:
        return address.strip().lower()

    async def address_history(
        self,
        address: Address,
        *,
        window: TimeWindow | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> HistoryPage:
        self.require(Capability.ADDRESS_HISTORY)
        page = 1 if cursor is None else int(cursor)
        common: dict[str, Any] = {
            "address": address.value.lower(),
            "page": page,
            "offset": limit,
            "sort": "desc",
        }
        # NOT degradable. The native feed is what makes a page a page; if it is
        # gone there is nothing to merge the other two into, and an empty page
        # would reach the engine indistinguishable from "this address has never
        # transacted" — the one output that is worse than an error, because
        # nothing downstream can tell it from the truth.
        native = await self._pool.fetch(
            ProviderRequest(self.chain, Capability.ADDRESS_HISTORY, common)
        )
        gaps: list[FeedGap] = []
        token = await self._optional_feed(Capability.TOKEN_TRANSFERS, common, gaps)
        # Contract-delivered native value (mixer withdrawals, exchange withdrawal
        # contracts, smart-contract wallets, bridge exits) appears in NEITHER of
        # the two feeds above: `txlist` carries only top-level calls and `tokentx`
        # only token transfers. Without this third feed an address funded entirely
        # by a contract has no visible inflow at all, and the engine reports the
        # trail as ending here (REVIEW_FINDINGS.md, contract-delivered value).
        internal = await self._optional_feed(Capability.INTERNAL_TRACES, common, gaps)
        native_rows = cast(list[dict[str, Any]], native.payload)
        token_rows = cast(list[dict[str, Any]], token.payload) if token is not None else []
        internal_rows = cast(list[dict[str, Any]], internal.payload) if internal is not None else []

        def blank_group() -> dict[str, Any]:
            return {"txlist_row": None, "token_rows": [], "internal_rows": []}

        groups: dict[str, dict[str, Any]] = {}
        for row in native_rows:
            tx_hash = str(row["hash"]).lower()
            groups.setdefault(tx_hash, blank_group())
            groups[tx_hash]["txlist_row"] = row
        for row in token_rows:
            tx_hash = str(row["hash"]).lower()
            groups.setdefault(tx_hash, blank_group())
            groups[tx_hash]["token_rows"].append(row)
        for row in internal_rows:
            tx_hash = str(row["hash"]).lower()
            groups.setdefault(tx_hash, blank_group())
            groups[tx_hash]["internal_rows"].append(row)

        def group_base(group: dict[str, Any]) -> dict[str, Any]:
            # A tx may be present in any one feed alone — an internal-only tx has
            # no txlist row and no token rows.
            row = group["txlist_row"]
            if row is not None:
                return cast(dict[str, Any], row)
            rows = group["token_rows"] or group["internal_rows"]
            return cast(dict[str, Any], rows[0])

        def group_time(group: dict[str, Any]) -> int:
            return int(group_base(group)["timeStamp"])

        ordered = sorted(groups.items(), key=lambda kv: (-group_time(kv[1]), kv[0]))
        if window is not None:
            ordered = [
                (tx_hash, group)
                for tx_hash, group in ordered
                if self._in_window(group_time(group), window)
            ]
        # A lost feed contributes no rows, so its provenance is never the one a
        # group ends up citing — every branch below is guarded by rows that only
        # a feed that ANSWERED could have produced. Carried as None rather than
        # omitted so the raw dialect keeps one shape whatever survived.
        prov_token = token.provenance() if token is not None else None
        prov_internal = internal.provenance() if internal is not None else None

        def group_provenance(group: dict[str, Any]) -> Provenance:
            if group["txlist_row"] is None:
                if group["token_rows"] and prov_token is not None:
                    return prov_token
                if group["internal_rows"] and prov_internal is not None:
                    return prov_internal
            return native.provenance()

        items = tuple(
            ChainTransaction(
                chain=self.chain,
                tx_hash=tx_hash,
                raw={
                    "source": "etherscan",
                    "txlist_row": group["txlist_row"],
                    "token_rows": group["token_rows"],
                    "internal_rows": group["internal_rows"],
                    "prov_txlist": native.provenance(),
                    "prov_token": prov_token,
                    "prov_internal": prov_internal,
                },
                provenance=group_provenance(group),
            )
            for tx_hash, group in ordered
        )
        next_cursor = (
            str(page + 1)
            if len(native_rows) >= limit or len(token_rows) >= limit or len(internal_rows) >= limit
            else None
        )
        return HistoryPage(items=items, next_cursor=next_cursor, gaps=tuple(gaps))

    async def _optional_feed(
        self,
        capability: Capability,
        params: dict[str, Any],
        gaps: list[FeedGap],
    ) -> ProviderResponse | None:
        """A SECONDARY feed: its loss costs its own rows and nothing else.

        The rules that make that safe — catch only ``AllProvidersFailed``, and
        record the gap before returning the ``None`` — live in
        ``chains.feeds`` so every multi-feed adapter keeps them identically.
        Which feed is secondary is the part that is chain knowledge, and it
        stays here: see ``address_history`` for why ``ADDRESS_HISTORY`` is not.
        """
        return await optional_feed(self._pool, self.chain, capability, params, gaps)

    async def transaction(self, tx_hash: str) -> ChainTransaction:
        tx_resp = await self._pool.fetch(
            ProviderRequest(self.chain, Capability.TX_LOOKUP, {"tx_hash": tx_hash})
        )
        receipt_resp = await self._pool.fetch(
            ProviderRequest(self.chain, Capability.TX_RECEIPT, {"tx_hash": tx_hash})
        )
        tx_obj = cast(dict[str, Any], tx_resp.payload)
        receipt = cast(dict[str, Any], receipt_resp.payload)
        block_resp = await self._pool.fetch(
            ProviderRequest(
                self.chain, Capability.BLOCK_LOOKUP, {"number": int(tx_obj["blockNumber"], 16)}
            )
        )
        block = cast(dict[str, Any], block_resp.payload)
        return ChainTransaction(
            chain=self.chain,
            tx_hash=str(tx_obj["hash"]).lower(),
            raw={
                "source": "rpc",
                "tx": tx_obj,
                "receipt": receipt,
                "timestamp": int(block["timestamp"], 16),
                "prov_tx": tx_resp.provenance(),
                "prov_receipt": receipt_resp.provenance(),
            },
            provenance=tx_resp.provenance(),
        )

    async def normalize(self, tx: ChainTransaction) -> NormalizedTransaction:
        raw = cast(dict[str, Any], tx.raw)
        if raw.get("source") == "etherscan":
            normalized = self._normalize_etherscan(tx, raw)
        elif raw.get("source") == "rpc":
            normalized = self._normalize_rpc(tx, raw)
        else:
            raise ValueError(f"unknown raw dialect for tx {tx.tx_hash}")
        hints = self._bridge_hints(normalized)
        if not hints:
            return normalized
        return NormalizedTransaction(
            tx=normalized.tx, movements=normalized.movements, bridge_hints=hints
        )

    def _bridge_hints(self, normalized: NormalizedTransaction) -> tuple[BridgeHint, ...]:
        """Flag value entering or leaving a known bridge contract.

        Chain-local only: this says "value moved into the Polygon PoS bridge
        heading to Polygon", not "and it came out over there" — pairing the
        two sides needs both chains and is analysis/bridges' job.
        """
        if not len(self._bridges):
            return ()
        hints: dict[tuple[str, BridgeDirection], BridgeHint] = {}
        for movement in normalized.movements:
            for endpoint, direction in (
                (movement.to_address, BridgeDirection.DEPOSIT),
                (movement.from_address, BridgeDirection.WITHDRAWAL),
            ):
                if endpoint is None:
                    continue
                entry = self._bridges.lookup(self.chain, endpoint.value)
                if entry is None:
                    continue
                key = (entry.bridge_id, direction)
                if key in hints:
                    continue
                hints[key] = BridgeHint(
                    bridge_id=entry.bridge_id,
                    direction=direction,
                    counterpart_chain=entry.counterpart_chain,
                    tx=normalized.tx,
                    refs=(normalized.tx.tx_hash, entry.address),
                )
        return tuple(hints[k] for k in sorted(hints))

    # ── etherscan-row dialect ────────────────────────────────────────────

    def _normalize_etherscan(
        self, tx: ChainTransaction, raw: dict[str, Any]
    ) -> NormalizedTransaction:
        row = cast(dict[str, Any] | None, raw.get("txlist_row"))
        token_rows = cast(list[dict[str, Any]], raw.get("token_rows") or [])
        internal_rows = cast(list[dict[str, Any]], raw.get("internal_rows") or [])
        base = row if row is not None else (token_rows or internal_rows)[0]
        tx_ref = TxRef(
            chain=self.chain,
            tx_hash=tx.tx_hash,
            timestamp=datetime.fromtimestamp(int(base["timeStamp"]), tz=UTC),
            block_number=int(base["blockNumber"]),
        )
        # One price for the whole transaction, stamped on every movement below —
        # the token rows repeat it, the internal rows never carry it, and all of
        # them were paid for by the same transaction.
        gas_price = _etherscan_gas_price((row, *token_rows, *internal_rows))
        movements: list[Movement] = []
        index = 0
        if row is not None and row.get("isError") != "1" and int(row.get("value", "0")) > 0:
            target = str(row.get("to") or row.get("contractAddress") or "").lower()
            if target:
                movements.append(
                    Movement(
                        tx=tx_ref,
                        asset=self._native,
                        amount=int(row["value"]),
                        kind=MovementKind.NATIVE,
                        from_address=Address(self.chain, str(row["from"]).lower()),
                        to_address=Address(self.chain, target),
                        index=index,
                        provenance=cast(Provenance, raw["prov_txlist"]),
                        dedup_key="native",
                        gas_price=gas_price,
                    )
                )
                index += 1
        for token_row in token_rows:
            sender = str(token_row.get("from") or "").lower()
            target = str(token_row.get("to") or "").lower()
            if not sender or not target:
                continue  # canonical token movements need both endpoints
            contract = str(token_row["contractAddress"]).lower()
            movements.append(
                Movement(
                    tx=tx_ref,
                    asset=Asset(
                        chain=self.chain,
                        kind=AssetKind.TOKEN,
                        symbol=str(token_row.get("tokenSymbol") or "TOKEN"),
                        decimals=int(token_row.get("tokenDecimal") or 0),
                        contract=contract,
                    ),
                    amount=int(token_row["value"]),
                    kind=MovementKind.TOKEN,
                    from_address=Address(self.chain, sender),
                    to_address=Address(self.chain, target),
                    index=index,
                    provenance=cast(Provenance, raw["prov_token"]),
                    # Same content key as the RPC dialect: one logical transfer
                    # keys identically from either vantage, so re-normalizing the
                    # same tx dedups instead of dropping/duplicating rows.
                    dedup_key=_token_key(sender, target, contract),
                    gas_price=gas_price,
                )
            )
            index += 1
        for internal_row in internal_rows:
            if internal_row.get("isError") == "1":
                continue  # a reverted internal call moved nothing
            amount = int(internal_row.get("value") or 0)
            if amount <= 0:
                continue
            sender = str(internal_row.get("from") or "").lower()
            # `create` traces name the new contract in contractAddress, not `to`;
            # self-destruct traces can carry neither, and are skipped.
            target = str(
                internal_row.get("to") or internal_row.get("contractAddress") or ""
            ).lower()
            if not sender or not target:
                continue  # canonical internal movements need both endpoints
            movements.append(
                Movement(
                    tx=tx_ref,
                    asset=self._native,
                    amount=amount,
                    kind=MovementKind.INTERNAL,
                    from_address=Address(self.chain, sender),
                    to_address=Address(self.chain, target),
                    index=index,
                    provenance=cast(Provenance, raw["prov_internal"]),
                    dedup_key=_internal_key(internal_row, sender, target),
                    # An internal trace has no gas price of its own — it is a
                    # call inside a transaction someone else paid for. Inherit
                    # it, or a mixer withdrawal (which arrives exactly this way)
                    # reaches the rung with nothing to match on.
                    gas_price=gas_price,
                )
            )
            index += 1
        return NormalizedTransaction(tx=tx_ref, movements=tuple(movements))

    # ── rpc dialect ──────────────────────────────────────────────────────

    def _normalize_rpc(self, tx: ChainTransaction, raw: dict[str, Any]) -> NormalizedTransaction:
        tx_obj = cast(dict[str, Any], raw["tx"])
        receipt = cast(dict[str, Any], raw["receipt"])
        tx_ref = TxRef(
            chain=self.chain,
            tx_hash=tx.tx_hash,
            timestamp=datetime.fromtimestamp(int(raw["timestamp"]), tz=UTC),
            block_number=int(tx_obj["blockNumber"], 16),
        )
        succeeded = _receipt_succeeded(receipt)
        # Resolved once, from the transaction and its receipt, then stamped on
        # every movement including the log-derived token transfers below: a log
        # names no price, and leaving them null would hide the price of every
        # token movement this dialect produces.
        gas_price = _rpc_gas_price(tx_obj, receipt)
        movements: list[Movement] = []
        index = 0
        if succeeded and int(tx_obj.get("value", "0x0"), 16) > 0:
            target = str(tx_obj.get("to") or receipt.get("contractAddress") or "").lower()
            if target:
                movements.append(
                    Movement(
                        tx=tx_ref,
                        asset=self._native,
                        amount=int(tx_obj["value"], 16),
                        kind=MovementKind.NATIVE,
                        from_address=Address(self.chain, str(tx_obj["from"]).lower()),
                        to_address=Address(self.chain, target),
                        index=index,
                        provenance=cast(Provenance, raw["prov_tx"]),
                        dedup_key="native",
                        gas_price=gas_price,
                    )
                )
                index += 1
        if succeeded:
            for log in receipt.get("logs", []):
                topics = log.get("topics") or []
                if len(topics) != 3 or str(topics[0]).lower() != TRANSFER_TOPIC:
                    continue
                # ERC-20 amount is the FIRST 32-byte word of data only. Decoding
                # the whole field would let a non-standard log inject a value
                # beyond uint256 (wrong amount, or NUMERIC overflow that aborts
                # the whole batch — a DoS). REVIEW_FINDINGS.md #5.
                amount = _erc20_amount(log.get("data"))
                if amount is None:
                    continue
                # log_index gives the token movement a vantage-stable identity
                # (the same transfer keys identically from any acquisition).
                log_index = _hex_or_none(log.get("logIndex"))
                movements.append(
                    Movement(
                        tx=tx_ref,
                        asset=Asset(
                            chain=self.chain,
                            kind=AssetKind.TOKEN,
                            symbol="TOKEN",  # display metadata is enrichment, never guessed
                            decimals=0,
                            contract=str(log["address"]).lower(),
                        ),
                        amount=amount,
                        kind=MovementKind.TOKEN,
                        from_address=Address(self.chain, "0x" + str(topics[1])[-40:].lower()),
                        to_address=Address(self.chain, "0x" + str(topics[2])[-40:].lower()),
                        index=log_index if log_index is not None else index,
                        provenance=cast(Provenance, raw["prov_receipt"]),
                        dedup_key=_token_key(
                            "0x" + str(topics[1])[-40:].lower(),
                            "0x" + str(topics[2])[-40:].lower(),
                            str(log["address"]).lower(),
                        ),
                        gas_price=gas_price,
                    )
                )
                index += 1
        return NormalizedTransaction(tx=tx_ref, movements=tuple(movements))

    @staticmethod
    def _in_window(unix_time: int, window: TimeWindow) -> bool:
        when = datetime.fromtimestamp(unix_time, tz=UTC)
        if window.start is not None and when < window.start:
            return False
        return not (window.end is not None and when > window.end)

"""Tron chain adapter.

Tron is an account-model chain, but its API surface differs from the EVM
family enough to need its own adapter rather than a config entry:

- Two separate history endpoints — native transactions and TRC-20 transfers
  — with different payload shapes, merged here into one page.
- Addresses appear in **two encodings**. The TRC-20 feed returns canonical
  Base58 (``T…``); raw transaction bodies return hex (``41…``). Both are
  converted to Base58 here so nothing above the adapter ever sees the
  difference.
- Success lives in ``ret[].contractRet == "SUCCESS"``.

TRC-20 matters most in practice: USDT-TRC20 is the dominant stablecoin rail,
so the token feed is the primary source and native TRX the secondary.

Provider note: TronGrid serves this keylessly at a modest rate. A free API
key raises the limit and is picked up automatically if ``TRONGRID_API_KEY``
is set.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Any, cast

from cipherchain.chains.base import (
    ChainAdapter,
    ChainTransaction,
    FeedGap,
    HistoryPage,
    NormalizedTransaction,
    TimeWindow,
)
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
from cipherchain.providers.base import SHORT_READ_KEY, ProviderRequest
from cipherchain.providers.pool import ProviderPool

TRX_ASSET = Asset(chain="tron", kind=AssetKind.NATIVE, symbol="TRX", decimals=6)

# Base58Check, always 34 chars, always 'T'. Distinct from Bitcoin's [13]
# prefix and Solana's 43-44 length, so no detection collision.
_TRON_ADDRESS = re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$")
_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58encode(raw: bytes) -> str:
    number = int.from_bytes(raw, "big")
    out = ""
    while number:
        number, remainder = divmod(number, 58)
        out = _B58_ALPHABET[remainder] + out
    padding = len(raw) - len(raw.lstrip(b"\x00"))
    return "1" * padding + out


def hex_to_base58(value: str) -> str:
    """Tron hex address (``41`` + 20 bytes) → canonical Base58Check ``T…``.

    Raw transaction bodies use hex; every other surface uses Base58. Storing
    both forms would split one wallet into two addresses in the graph, so
    everything is normalized here.
    """
    text = value[2:] if value.startswith("0x") else value
    try:
        raw = bytes.fromhex(text)
    except ValueError:
        return value  # already Base58, or unparseable — leave untouched
    if len(raw) != 21 or raw[0] != 0x41:
        return value
    checksum = hashlib.sha256(hashlib.sha256(raw).digest()).digest()[:4]
    return _b58encode(raw + checksum)


def _address(value: Any) -> str | None:
    if not value:
        return None
    text = str(value)
    return hex_to_base58(text) if not text.startswith("T") else text


# One opaque cursor over two independently-paged feeds. "|" cannot occur in a
# TronGrid fingerprint (base58), so it is safe as the separator, and an empty
# half means "that feed is exhausted, do not ask it again".
_CURSOR_SEPARATOR = "|"


def _split_cursor(cursor: str | None) -> tuple[str | None, str | None]:
    if not cursor:
        return None, None
    native, _, token = cursor.partition(_CURSOR_SEPARATOR)
    return native or None, token or None


def _feed_fingerprint(body: dict[str, Any]) -> str:
    """The fingerprint to ask for next, or "" when this feed is done.

    Presence of ``meta.links.next`` is the signal, not presence of a
    fingerprint: TronGrid returns a fingerprint on the final page too, so
    keying on it alone would page forever over the same last rows.
    """
    meta = body.get("meta")
    if not isinstance(meta, dict):
        return ""
    links = meta.get("links")
    if not isinstance(links, dict) or not links.get("next"):
        return ""
    return str(meta.get("fingerprint") or "")


def _join_cursor(
    native_body: dict[str, Any],
    token_body: dict[str, Any],
    *,
    token_fallback: str | None = None,
) -> str | None:
    """The cursor for the next page, from both feeds' own positions.

    ``token_fallback`` is the mark to carry forward when the token feed was not
    read at all (no provider could serve it). Its body is then empty, which
    ``_feed_fingerprint`` correctly reads as "no next link" — but that is a
    statement about a page nobody fetched, and treating it as "this feed is
    finished" would silently retire the token feed for the rest of the address.
    """
    native = _feed_fingerprint(native_body)
    token = _feed_fingerprint(token_body) or (token_fallback or "")
    if not native and not token:
        return None
    return f"{native}{_CURSOR_SEPARATOR}{token}"


class TronAdapter(ChainAdapter):
    chain = "tron"

    def __init__(self, pool: ProviderPool) -> None:
        self._pool = pool

    def capabilities(self) -> frozenset[Capability]:
        return frozenset(
            {
                Capability.ADDRESS_HISTORY,
                Capability.TOKEN_TRANSFERS,
                Capability.TX_LOOKUP,
            }
        )

    def recognizes(self, address: str) -> bool:
        return bool(_TRON_ADDRESS.match(address.strip()))

    def canonical_address(self, address: str) -> str:
        return hex_to_base58(address.strip())

    async def address_history(
        self,
        address: Address,
        *,
        window: TimeWindow | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> HistoryPage:
        self.require(Capability.ADDRESS_HISTORY)
        page_size = min(limit, 200)
        # The two feeds page INDEPENDENTLY, so one cursor has to carry both
        # fingerprints; advancing only one would silently re-read the other
        # from the top and drop everything between.
        native_mark, token_mark = _split_cursor(cursor)
        native = await self._pool.fetch(
            ProviderRequest(
                self.chain,
                Capability.ADDRESS_HISTORY,
                {"address": address.value, "limit": page_size, "fingerprint": native_mark},
            )
        )
        # TRC-20 is a SECONDARY feed here, and losing it must not cost the
        # address. That matters more on Tron than anywhere else: the chain is
        # mostly USDT, so an address whose token feed died has essentially no
        # visible money left — and Tron is where the label packs carry the most
        # VASPs, so a page killed by one dead feed is a VASP not reached.
        # ADDRESS_HISTORY above stays a hard await for the reason the EVM
        # adapter gives: with no native feed there is nothing to merge into, and
        # an empty page is indistinguishable downstream from an address that
        # never transacted.
        gaps: list[FeedGap] = []
        token = await optional_feed(
            self._pool,
            self.chain,
            Capability.TOKEN_TRANSFERS,
            {"address": address.value, "limit": page_size, "fingerprint": token_mark},
            gaps,
        )
        native_body = cast(dict[str, Any], native.payload)
        token_body = cast(dict[str, Any], token.payload) if token is not None else {}
        native_rows = cast(list[dict[str, Any]], native_body.get("data") or [])
        token_rows = cast(list[dict[str, Any]], token_body.get("data") or [])

        groups: dict[str, dict[str, Any]] = {}
        for row in native_rows:
            tx_id = str(row.get("txID") or "")
            if tx_id:
                groups.setdefault(tx_id, {"native": None, "tokens": []})["native"] = row
        for row in token_rows:
            tx_id = str(row.get("transaction_id") or "")
            if tx_id:
                groups.setdefault(tx_id, {"native": None, "tokens": []})["tokens"].append(row)

        def when(group: dict[str, Any]) -> int:
            base = group["native"] or group["tokens"][0]
            return int(base.get("block_timestamp") or 0)

        ordered = sorted(groups.items(), key=lambda kv: (-when(kv[1]), kv[0]))
        if window is not None:
            ordered = [(h, g) for h, g in ordered if self._in_window(when(g) // 1000, window)]
        items = tuple(
            ChainTransaction(
                chain=self.chain,
                tx_hash=tx_id,
                raw={
                    "native": group["native"],
                    "tokens": group["tokens"],
                    "prov_native": native.provenance(),
                    # None when the feed never answered. The shape stays
                    # constant so `normalize` reads one dialect, and it is only
                    # ever dereferenced inside a loop over rows that a feed
                    # which DID answer produced.
                    "prov_token": token.provenance() if token is not None else None,
                },
                provenance=(
                    native.provenance() if group["native"] or token is None else token.provenance()
                ),
            )
            for tx_id, group in ordered
        )
        return HistoryPage(
            items=items,
            # A feed that was never read is not a feed that is finished. Carrying
            # the mark we failed at forward makes the next page ask for exactly
            # the position we missed; letting `_join_cursor` see an empty body
            # instead would record the token feed as exhausted and skip it for
            # the rest of the address.
            next_cursor=_join_cursor(
                native_body, token_body, token_fallback=token_mark if token is None else None
            ),
            gaps=tuple(gaps),
            # EITHER feed being cut truncates the page, because a page is the
            # merge of the two and the caller reads one address, not two feeds.
            # This is the only signal a provider that cannot mint a cursor has:
            # the keyless explorer tier reads a fixed number of numbered pages
            # and then stops, and `_join_cursor` correctly reports no cursor for
            # it — which downstream is indistinguishable from "this address has
            # no more history". Without this line an address with more than
            # `site_pages_per_call` pages of history was counted as read in FULL
            # whenever that tier answered, while the SAME address served by
            # TronGrid was marked truncated: the document's honesty depended on
            # which provider happened to be up.
            truncated=bool(native_body.get(SHORT_READ_KEY)) or bool(token_body.get(SHORT_READ_KEY)),
        )

    async def transaction(self, tx_hash: str) -> ChainTransaction:
        response = await self._pool.fetch(
            ProviderRequest(self.chain, Capability.TX_LOOKUP, {"tx_hash": tx_hash})
        )
        payload = cast(dict[str, Any], response.payload)
        return ChainTransaction(
            chain=self.chain,
            tx_hash=tx_hash,
            raw={"native": payload, "tokens": [], "prov_native": response.provenance()},
            provenance=response.provenance(),
        )

    async def normalize(self, tx: ChainTransaction) -> NormalizedTransaction:
        raw = cast(dict[str, Any], tx.raw)
        native = cast(dict[str, Any] | None, raw.get("native"))
        tokens = cast(list[dict[str, Any]], raw.get("tokens") or [])
        base = native if native is not None else (tokens[0] if tokens else None)
        if base is None:
            raise ValueError(f"no payload to normalize for tx {tx.tx_hash}")

        millis = int(base.get("block_timestamp") or 0)
        if millis <= 0:
            raise ValueError(f"refusing to normalize unconfirmed tx {tx.tx_hash}")
        tx_ref = TxRef(
            chain=self.chain,
            tx_hash=tx.tx_hash,
            timestamp=datetime.fromtimestamp(millis / 1000, tz=UTC),
            block_number=base.get("blockNumber"),
        )

        movements: list[Movement] = []
        if native is not None and self._succeeded(native):
            movements.extend(self._native_movements(tx_ref, native, raw))
        for index, row in enumerate(tokens):
            movement = self._token_movement(tx_ref, row, raw, index)
            if movement is not None:
                movements.append(movement)
        return NormalizedTransaction(tx=tx_ref, movements=tuple(movements))

    # ── native TRX ───────────────────────────────────────────────────────

    def _native_movements(
        self, tx_ref: TxRef, native: dict[str, Any], raw: dict[str, Any]
    ) -> list[Movement]:
        movements: list[Movement] = []
        contracts = ((native.get("raw_data") or {}).get("contract")) or []
        for index, contract in enumerate(contracts):
            # Only a plain transfer moves native TRX; a contract call moves
            # value through TRC-20 events, which the token feed reports.
            if contract.get("type") != "TransferContract":
                continue
            value = (contract.get("parameter") or {}).get("value") or {}
            amount = int(value.get("amount") or 0)
            sender = _address(value.get("owner_address"))
            recipient = _address(value.get("to_address"))
            if amount <= 0 or not sender or not recipient:
                continue
            movements.append(
                Movement(
                    tx=tx_ref,
                    asset=TRX_ASSET,
                    amount=amount,
                    kind=MovementKind.NATIVE,
                    from_address=Address(self.chain, sender),
                    to_address=Address(self.chain, recipient),
                    index=index,
                    provenance=cast(Provenance, raw["prov_native"]),
                    dedup_key=f"trx:{index}",
                )
            )
        return movements

    # ── TRC-20 ───────────────────────────────────────────────────────────

    def _token_movement(
        self, tx_ref: TxRef, row: dict[str, Any], raw: dict[str, Any], index: int
    ) -> Movement | None:
        if str(row.get("type") or "Transfer") != "Transfer":
            return None
        sender = _address(row.get("from"))
        recipient = _address(row.get("to"))
        amount = int(row.get("value") or 0)
        info = row.get("token_info") or {}
        contract = _address(info.get("address"))
        if not (sender and recipient and contract) or amount <= 0:
            return None
        return Movement(
            tx=tx_ref,
            asset=Asset(
                chain=self.chain,
                kind=AssetKind.TOKEN,
                symbol=str(info.get("symbol") or "TRC20"),
                decimals=int(info.get("decimals") or 0),
                contract=contract,
            ),
            amount=amount,
            kind=MovementKind.TOKEN,
            from_address=Address(self.chain, sender),
            to_address=Address(self.chain, recipient),
            index=index,
            provenance=cast(Provenance, raw.get("prov_token") or raw["prov_native"]),
            # Content key: the same transfer keys identically from either feed.
            dedup_key=f"trc20:{sender}:{recipient}:{contract}:{amount}",
        )

    @staticmethod
    def _succeeded(native: dict[str, Any]) -> bool:
        rets = native.get("ret") or []
        return any(r.get("contractRet") == "SUCCESS" for r in rets) if rets else False

    @staticmethod
    def _in_window(unix_time: int, window: TimeWindow) -> bool:
        when = datetime.fromtimestamp(unix_time, tz=UTC)
        if window.start is not None and when < window.start:
            return False
        return not (window.end is not None and when > window.end)

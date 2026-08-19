"""Regression tests for the value-correctness bugs the review surfaced
(REVIEW_FINDINGS.md #1, #5, #6). Each would silently corrupt value.
"""

from datetime import UTC, datetime

import pytest

from cipherchain.chains.base import ChainTransaction
from cipherchain.chains.evm import ETHEREUM_CONFIG, EvmAdapter
from cipherchain.chains.evm.adapter import _erc20_amount, _receipt_succeeded, _token_key
from cipherchain.core.models import MovementKind, Provenance

NOW = datetime(2026, 8, 7, tzinfo=UTC)
PROV = Provenance(provider="rpc", retrieved_at=NOW, payload_sha256="a" * 64)
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
UINT256_MAX = 2**256 - 1


def topic_addr(addr: str) -> str:
    return "0x" + "0" * 24 + addr[2:]


def rpc_tx(*, value_hex: str, logs: list[dict], status: str = "0x1", root: str | None = None):
    receipt: dict = {"logs": logs}
    if status is not None:
        receipt["status"] = status
    if root is not None:
        receipt["root"] = root
    return ChainTransaction(
        chain="ethereum",
        tx_hash="0xtx",
        raw={
            "source": "rpc",
            "tx": {
                "hash": "0xtx",
                "from": "0x" + "1" * 40,
                "to": "0x" + "2" * 40,
                "value": value_hex,
                "blockNumber": "0x10",
            },
            "receipt": receipt,
            "timestamp": int(NOW.timestamp()),
            "prov_tx": PROV,
            "prov_receipt": PROV,
        },
        provenance=PROV,
    )


class TestErc20AmountDecode:
    def test_canonical_single_word_decodes(self) -> None:
        assert _erc20_amount("0x" + f"{500:064x}") == 500
        # a spoofed multi-word payload is rejected outright, never decoded as
        # 2^256+junk — the whole-field int() overflow vector is closed
        assert _erc20_amount("0x" + f"{1:064x}" + f"{999:064x}") is None

    def test_non_canonical_data_rejected(self) -> None:
        assert _erc20_amount("0x" + "ff" * 33) is None  # 33 bytes, not one word
        assert _erc20_amount("0xnothex" + "0" * 58) is None
        assert _erc20_amount("0x" + "ff" * 40) is None  # oversized (DoS vector)

    def test_zero_value_transfer_is_valid(self) -> None:
        assert _erc20_amount("0x" + "0" * 64) == 0  # 32-byte zero word

    def test_empty_data_is_skipped(self) -> None:
        assert _erc20_amount("0x") is None  # malformed: no amount word
        assert _erc20_amount(None) is None

    async def test_oversized_log_is_skipped_not_stored(self) -> None:
        adapter = EvmAdapter(ETHEREUM_CONFIG, object())  # type: ignore[arg-type]
        tx = rpc_tx(
            value_hex="0x0",
            logs=[
                {
                    "address": "0x" + "c" * 40,
                    "topics": [TRANSFER, topic_addr("0x" + "a" * 40), topic_addr("0x" + "b" * 40)],
                    "data": "0x" + "ff" * 40,  # 40 bytes, exceeds uint256
                    "logIndex": "0x0",
                }
            ],
        )
        normalized = await adapter.normalize(tx)
        assert normalized.movements == ()  # malformed log dropped, no overflow

    async def test_valid_transfer_decoded(self) -> None:
        adapter = EvmAdapter(ETHEREUM_CONFIG, object())  # type: ignore[arg-type]
        tx = rpc_tx(
            value_hex="0x0",
            logs=[
                {
                    "address": "0x" + "c" * 40,
                    "topics": [TRANSFER, topic_addr("0x" + "a" * 40), topic_addr("0x" + "b" * 40)],
                    "data": "0x" + f"{500:064x}",
                    "logIndex": "0x2",
                }
            ],
        )
        normalized = await adapter.normalize(tx)
        assert len(normalized.movements) == 1
        assert normalized.movements[0].amount == 500
        assert normalized.movements[0].dedup_key == _token_key(
            "0x" + "a" * 40, "0x" + "b" * 40, "0x" + "c" * 40
        )


class TestReceiptStatus:
    def test_standard_success_and_failure(self) -> None:
        assert _receipt_succeeded({"status": "0x1"}) is True
        assert _receipt_succeeded({"status": "0x0"}) is False

    def test_zero_padded_status(self) -> None:
        assert _receipt_succeeded({"status": "0x01"}) is True  # non-canonical encoding

    def test_pre_byzantium_receipt_with_root_is_success(self) -> None:
        # blocks < 4,370,000: no status field, carries a state root
        assert _receipt_succeeded({"root": "0x" + "a" * 64}) is True

    async def test_pre_byzantium_tx_moves_value(self) -> None:
        adapter = EvmAdapter(ETHEREUM_CONFIG, object())  # type: ignore[arg-type]
        tx = rpc_tx(value_hex=hex(10**21), logs=[], status=None, root="0x" + "e" * 64)
        normalized = await adapter.normalize(tx)
        assert len(normalized.movements) == 1  # not silently dropped as "failed"
        assert normalized.movements[0].amount == 10**21
        assert normalized.movements[0].kind is MovementKind.NATIVE


async def test_amount_must_be_int_not_float() -> None:
    from cipherchain.core.models import Address, Asset, AssetKind, Movement, TxRef

    with pytest.raises(ValueError, match="int"):
        Movement(
            tx=TxRef(chain="ethereum", tx_hash="0x1", timestamp=NOW),
            asset=Asset(chain="ethereum", kind=AssetKind.NATIVE, symbol="ETH", decimals=18),
            amount=0.5,  # type: ignore[arg-type]
            kind=MovementKind.NATIVE,
            from_address=Address("ethereum", "0xa"),
            to_address=Address("ethereum", "0xb"),
            index=0,
            provenance=PROV,
        )

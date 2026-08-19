"""Tron adapter against recorded real TronGrid payloads.

The subtle part is address encoding: TronGrid returns Base58 (``T…``) on the
TRC-20 feed but hex (``41…``) inside raw transaction bodies. Storing both
forms would split one wallet into two nodes in the graph, so every address
is normalized to Base58 here — and that conversion is checked against a
known contract address.
"""

from datetime import UTC, datetime
from typing import Any

import pytest

from cipherchain.chains.base import ChainTransaction
from cipherchain.chains.tron import TRX_ASSET, TronAdapter, hex_to_base58
from cipherchain.core.models import Capability, MovementKind, Provenance
from tests.chains.conftest import fixture_json

NOW = datetime(2026, 8, 7, tzinfo=UTC)
PROV = Provenance(provider="trongrid", retrieved_at=NOW, payload_sha256="a" * 64)
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


@pytest.fixture
def adapter() -> TronAdapter:
    return TronAdapter(None)  # type: ignore[arg-type]  # normalize needs no pool


def trc20_rows() -> list[dict[str, Any]]:
    return fixture_json("tron_trc20.json")["data"]


def native_rows() -> list[dict[str, Any]]:
    return fixture_json("tron_native.json")["data"]


def chain_tx(native: dict[str, Any] | None, tokens: list[dict[str, Any]]) -> ChainTransaction:
    tx_id = str((native or {}).get("txID") or (tokens[0]["transaction_id"] if tokens else ""))
    return ChainTransaction(
        chain="tron",
        tx_hash=tx_id,
        raw={"native": native, "tokens": tokens, "prov_native": PROV, "prov_token": PROV},
        provenance=PROV,
    )


class TestAddressEncoding:
    def test_hex_converts_to_known_base58_contract(self) -> None:
        """Ground truth: this hex IS the USDT-TRC20 contract."""
        assert hex_to_base58("41a614f803b6fd780986a42c78ec9c7f77e6ded13c") == USDT_CONTRACT

    def test_base58_passes_through_untouched(self) -> None:
        assert hex_to_base58(USDT_CONTRACT) == USDT_CONTRACT

    def test_conversion_is_stable(self) -> None:
        h = "418e4db52262cc6dbf7158cb57b2d8da67bd62cd91"
        once = hex_to_base58(h)
        assert once.startswith("T") and len(once) == 34
        assert hex_to_base58(once) == once  # idempotent

    def test_recognizes_tron_addresses_only(self, adapter: TronAdapter) -> None:
        assert adapter.recognizes(USDT_CONTRACT)
        assert not adapter.recognizes("0x" + "a" * 40)  # EVM
        assert not adapter.recognizes("3PeVz6zCzRWsRq9YfZYbfbP92ZYDNyMUCC")  # Bitcoin
        assert not adapter.recognizes("DNVZMSqeRH18Xa4MCTrb1MndNf3Npg4MEwqswo23eWkf")  # Solana

    def test_canonical_address_normalizes_hex(self, adapter: TronAdapter) -> None:
        assert adapter.canonical_address("41a614f803b6fd780986a42c78ec9c7f77e6ded13c") == (
            USDT_CONTRACT
        )


class TestCapabilities:
    def test_declared(self, adapter: TronAdapter) -> None:
        assert adapter.supports(Capability.ADDRESS_HISTORY)
        assert adapter.supports(Capability.TOKEN_TRANSFERS)
        # no UTXO set, no EVM logs/receipts — declared absences
        assert not adapter.supports(Capability.UTXO_LOOKUP)
        assert not adapter.supports(Capability.LOGS)


class TestTrc20Normalization:
    async def test_token_transfer_matches_the_feed(self, adapter: TronAdapter) -> None:
        row = trc20_rows()[0]
        normalized = await adapter.normalize(chain_tx(None, [row]))
        assert len(normalized.movements) == 1
        m = normalized.movements[0]
        assert m.kind is MovementKind.TOKEN
        assert m.amount == int(row["value"])
        assert m.from_address is not None and m.from_address.value == row["from"]
        assert m.to_address is not None and m.to_address.value == row["to"]
        assert m.asset.contract == row["token_info"]["address"]
        assert m.asset.symbol == row["token_info"]["symbol"]
        assert m.asset.decimals == int(row["token_info"]["decimals"])

    async def test_usdt_decimals_are_read_not_guessed(self, adapter: TronAdapter) -> None:
        """USDT-TRC20 is the dominant rail; wrong decimals misreport value
        by 10^6, so they must come from the payload."""
        usdt = [r for r in trc20_rows() if r["token_info"]["symbol"] == "USDT"]
        assert usdt, "fixture should contain USDT transfers"
        normalized = await adapter.normalize(chain_tx(None, [usdt[0]]))
        assert normalized.movements[0].asset.decimals == 6

    async def test_timestamp_converted_from_millis(self, adapter: TronAdapter) -> None:
        row = trc20_rows()[0]
        normalized = await adapter.normalize(chain_tx(None, [row]))
        expected = datetime.fromtimestamp(int(row["block_timestamp"]) / 1000, tz=UTC)
        assert normalized.tx.timestamp == expected

    async def test_multiple_transfers_get_distinct_identities(self, adapter: TronAdapter) -> None:
        rows = trc20_rows()[:4]
        normalized = await adapter.normalize(chain_tx(None, rows))
        keys = [m.dedup_key for m in normalized.movements]
        assert len(keys) == len(set(keys))
        assert all(k and k.startswith("trc20:") for k in keys)

    async def test_renormalizing_is_identical(self, adapter: TronAdapter) -> None:
        rows = trc20_rows()[:3]
        assert await adapter.normalize(chain_tx(None, rows)) == await adapter.normalize(
            chain_tx(None, rows)
        )


class TestNativeNormalization:
    async def test_native_transfer_converts_hex_endpoints(self, adapter: TronAdapter) -> None:
        row = native_rows()[0]
        normalized = await adapter.normalize(chain_tx(row, []))
        native = [m for m in normalized.movements if m.asset == TRX_ASSET]
        assert len(native) == 1
        m = native[0]
        value = row["raw_data"]["contract"][0]["parameter"]["value"]
        assert m.amount == int(value["amount"])
        # hex in the payload, Base58 in the canonical model
        assert m.from_address is not None and m.from_address.value.startswith("T")
        assert m.to_address is not None and m.to_address.value.startswith("T")
        assert m.from_address.value == hex_to_base58(value["owner_address"])

    async def test_failed_transaction_moves_nothing(self, adapter: TronAdapter) -> None:
        row = {**native_rows()[0], "ret": [{"contractRet": "REVERT"}]}
        normalized = await adapter.normalize(chain_tx(row, []))
        assert [m for m in normalized.movements if m.asset == TRX_ASSET] == []

    async def test_contract_call_emits_no_native_movement(self, adapter: TronAdapter) -> None:
        """A TriggerSmartContract moves value via TRC-20 events, not natively —
        counting it as a TRX transfer would invent value."""
        calls = [
            t
            for t in fixture_json("tron_txs.json")["data"]
            if any(
                c.get("type") == "TriggerSmartContract"
                for c in (t.get("raw_data") or {}).get("contract", [])
            )
        ]
        assert calls, "fixture should contain contract calls"
        normalized = await adapter.normalize(chain_tx(calls[0], []))
        assert [m for m in normalized.movements if m.asset == TRX_ASSET] == []

    async def test_unconfirmed_refused(self, adapter: TronAdapter) -> None:
        row = {**native_rows()[0], "block_timestamp": 0}
        with pytest.raises(ValueError, match="unconfirmed"):
            await adapter.normalize(chain_tx(row, []))

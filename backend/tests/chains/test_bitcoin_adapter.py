"""Bitcoin adapter against recorded real payloads (assertions derived from
the fixtures themselves, so re-recording never breaks the suite)."""

from datetime import UTC, datetime
from typing import Any

import pytest

from cipherchain.chains.base import ChainTransaction
from cipherchain.chains.bitcoin import BTC_ASSET, BitcoinAdapter
from cipherchain.core.models import Address, Capability, MovementKind, Provenance
from tests.chains.conftest import fixture_json


def expected_halves(raw_tx: dict[str, Any]) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    inputs = [
        (vin["prevout"]["scriptpubkey_address"], vin["prevout"]["value"])
        for vin in raw_tx.get("vin", [])
        if not vin.get("is_coinbase")
        and (vin.get("prevout") or {}).get("scriptpubkey_address")
        and (vin.get("prevout") or {}).get("value")
    ]
    outputs = [
        (vout["scriptpubkey_address"], vout["value"])
        for vout in raw_tx.get("vout", [])
        if vout.get("scriptpubkey_address") and vout.get("value")
    ]
    return inputs, outputs


async def test_capability_declarations(fixture_pool) -> None:
    adapter = BitcoinAdapter(fixture_pool)
    assert adapter.supports(Capability.ADDRESS_HISTORY)
    assert not adapter.supports(Capability.TOKEN_TRANSFERS)  # declared absence
    assert not adapter.supports(Capability.INTERNAL_TRACES)


async def test_address_history_returns_confirmed_transactions(fixture_pool, manifest) -> None:
    adapter = BitcoinAdapter(fixture_pool)
    page = await adapter.address_history(Address("bitcoin", manifest["btc_address"]))
    fixture_txs = fixture_json("btc_address_txs.json")
    confirmed = [t for t in fixture_txs if t.get("status", {}).get("confirmed")]
    assert len(page.items) == len(confirmed)
    assert {item.tx_hash for item in page.items} == {t["txid"] for t in confirmed}
    assert all(item.chain == "bitcoin" for item in page.items)
    assert page.next_cursor is None  # tiny history: single page


async def test_normalize_emits_utxo_halves_matching_fixture(fixture_pool, manifest) -> None:
    adapter = BitcoinAdapter(fixture_pool)
    tx = await adapter.transaction(manifest["btc_txid"])
    normalized = await adapter.normalize(tx)

    raw = fixture_json("btc_tx.json")
    expected_inputs, expected_outputs = expected_halves(raw)

    inputs = [m for m in normalized.movements if m.kind is MovementKind.UTXO_INPUT]
    outputs = [m for m in normalized.movements if m.kind is MovementKind.UTXO_OUTPUT]

    assert [(m.from_address.value, m.amount) for m in inputs if m.from_address] == expected_inputs
    assert [(m.to_address.value, m.amount) for m in outputs if m.to_address] == expected_outputs
    assert all(m.asset == BTC_ASSET for m in normalized.movements)
    assert all(m.tx.tx_hash == manifest["btc_txid"] for m in normalized.movements)
    # index is the vin/vout POSITION (inputs and outputs each start at 0);
    # dedup_key carries the vantage-stable identity and is unique per tx.
    assert [m.index for m in inputs] == list(range(len(inputs)))
    assert [m.index for m in outputs] == list(range(len(outputs)))
    keys = [m.dedup_key for m in normalized.movements]
    assert len(keys) == len(set(keys))  # unique identity per movement
    assert all(k.startswith(("in:", "out:")) for k in keys if k)
    assert normalized.tx.timestamp.tzinfo is not None
    assert normalized.tx.block_number == raw["status"]["block_height"]


async def test_normalize_is_idempotent(fixture_pool, manifest) -> None:
    adapter = BitcoinAdapter(fixture_pool)
    tx = await adapter.transaction(manifest["btc_txid"])
    assert await adapter.normalize(tx) == await adapter.normalize(tx)


async def test_normalize_refuses_unconfirmed(fixture_pool) -> None:
    adapter = BitcoinAdapter(fixture_pool)
    raw = fixture_json("btc_tx.json")
    raw["status"] = {"confirmed": False}
    unconfirmed = ChainTransaction(
        chain="bitcoin",
        tx_hash=raw["txid"],
        raw=raw,
        provenance=Provenance(
            provider="test", retrieved_at=datetime(2026, 8, 7, tzinfo=UTC), payload_sha256="a" * 64
        ),
    )
    with pytest.raises(ValueError, match="unconfirmed"):
        await adapter.normalize(unconfirmed)


async def test_second_history_call_hits_cache(fixture_pool, manifest) -> None:
    adapter = BitcoinAdapter(fixture_pool)
    address = Address("bitcoin", manifest["btc_address"])
    await adapter.address_history(address)
    await adapter.address_history(address)
    hits = fixture_pool.metrics.snapshot()["cache_hits"]
    assert hits.get("bitcoin/address_history") == 1  # TTL cache absorbs the repeat

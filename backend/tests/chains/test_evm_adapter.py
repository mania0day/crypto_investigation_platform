"""EVM adapter against recorded real payloads — assertions derived from the
fixtures themselves (re-recording never breaks the suite)."""

from datetime import UTC, datetime
from typing import Any

from cipherchain.chains.base import ChainTransaction
from cipherchain.chains.evm import ETHEREUM_CONFIG, EvmAdapter
from cipherchain.core.models import Address, Capability, MovementKind
from tests.chains.conftest import fixture_json

TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def transfer_logs(receipt: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        log
        for log in receipt.get("logs", [])
        if log.get("topics")
        and len(log["topics"]) == 3
        and str(log["topics"][0]).lower() == TRANSFER_TOPIC
    ]


async def test_capability_declarations(fixture_pool) -> None:
    adapter = EvmAdapter(ETHEREUM_CONFIG, fixture_pool)
    assert adapter.chain == "ethereum"
    assert adapter.supports(Capability.ADDRESS_HISTORY)
    assert adapter.supports(Capability.TOKEN_TRANSFERS)
    assert not adapter.supports(Capability.UTXO_LOOKUP)  # declared absence
    # Contract-delivered native value is only visible through this feed.
    assert adapter.supports(Capability.INTERNAL_TRACES)


async def test_history_merges_txlist_and_tokentx_by_hash(fixture_pool, manifest) -> None:
    adapter = EvmAdapter(ETHEREUM_CONFIG, fixture_pool)
    page = await adapter.address_history(Address("ethereum", manifest["eth_address"]))

    txlist = fixture_json("eth_txlist.json")["result"]
    tokentx = fixture_json("eth_tokentx.json")["result"]
    expected_hashes = {str(r["hash"]).lower() for r in txlist} | {
        str(r["hash"]).lower() for r in tokentx
    }
    assert {item.tx_hash for item in page.items} == expected_hashes
    assert page.next_cursor is None  # both fixture pages are below the limit

    # newest-first ordering by row timestamp
    times = [
        int((item.raw["txlist_row"] or item.raw["token_rows"][0])["timeStamp"])
        for item in page.items
    ]  # type: ignore[index]
    assert times == sorted(times, reverse=True)


async def test_normalize_etherscan_native_row(fixture_pool, manifest) -> None:
    adapter = EvmAdapter(ETHEREUM_CONFIG, fixture_pool)
    page = await adapter.address_history(Address("ethereum", manifest["eth_address"]))
    txlist_rows = {str(r["hash"]).lower(): r for r in fixture_json("eth_txlist.json")["result"]}

    checked = 0
    for item in page.items:
        row = txlist_rows.get(item.tx_hash)
        if not row or row.get("isError") == "1" or int(row.get("value", "0")) == 0:
            continue
        normalized = await adapter.normalize(item)
        native = [m for m in normalized.movements if m.kind is MovementKind.NATIVE]
        assert len(native) == 1
        movement = native[0]
        assert movement.amount == int(row["value"])
        assert movement.from_address and movement.from_address.value == row["from"].lower()
        assert movement.to_address and movement.to_address.value == row["to"].lower()
        assert movement.asset.symbol == "ETH" and movement.asset.decimals == 18
        assert normalized.tx.timestamp == datetime.fromtimestamp(int(row["timeStamp"]), tz=UTC)
        checked += 1
    assert checked >= 1  # the recorded fixture contains at least one value transfer


async def test_normalize_etherscan_token_rows(fixture_pool, manifest) -> None:
    adapter = EvmAdapter(ETHEREUM_CONFIG, fixture_pool)
    page = await adapter.address_history(Address("ethereum", manifest["eth_address"]))

    token_items = [item for item in page.items if item.raw["token_rows"]]  # type: ignore[index]
    assert token_items  # tokentx fixture is non-empty by construction
    item = token_items[0]
    rows = item.raw["token_rows"]  # type: ignore[index]
    normalized = await adapter.normalize(item)
    tokens = [m for m in normalized.movements if m.kind is MovementKind.TOKEN]
    expected = [r for r in rows if str(r.get("from") or "") and str(r.get("to") or "")]
    assert len(tokens) == len(expected)
    for movement, row in zip(tokens, expected, strict=True):
        assert movement.amount == int(row["value"])
        assert movement.asset.contract == str(row["contractAddress"]).lower()
        assert movement.asset.decimals == int(row.get("tokenDecimal") or 0)
        assert movement.from_address and movement.from_address.value == row["from"].lower()


async def test_failed_tx_moves_no_value(fixture_pool, manifest) -> None:
    adapter = EvmAdapter(ETHEREUM_CONFIG, fixture_pool)
    page = await adapter.address_history(Address("ethereum", manifest["eth_address"]))
    item = next(i for i in page.items if i.raw["txlist_row"])  # type: ignore[index]
    raw = dict(item.raw)  # type: ignore[arg-type]
    raw["txlist_row"] = {**raw["txlist_row"], "isError": "1"}
    failed = ChainTransaction(
        chain=item.chain, tx_hash=item.tx_hash, raw=raw, provenance=item.provenance
    )
    normalized = await adapter.normalize(failed)
    assert [m for m in normalized.movements if m.kind is MovementKind.NATIVE] == []


async def test_rpc_dialect_decodes_transfer_logs(fixture_pool, manifest) -> None:
    adapter = EvmAdapter(ETHEREUM_CONFIG, fixture_pool)
    tx = await adapter.transaction(manifest["eth_token_tx"])
    normalized = await adapter.normalize(tx)

    tx_obj = fixture_json("eth_rpc_tx.json")["result"]
    receipt = fixture_json("eth_rpc_receipt.json")["result"]
    block = fixture_json("eth_rpc_block.json")["result"]
    logs = transfer_logs(receipt)

    tokens = [m for m in normalized.movements if m.kind is MovementKind.TOKEN]
    assert len(tokens) == len(logs)
    for movement, log in zip(tokens, logs, strict=True):
        data = str(log.get("data") or "0x")
        assert movement.amount == (int(data, 16) if data not in ("0x", "") else 0)
        assert movement.asset.contract == str(log["address"]).lower()
        assert movement.from_address is not None
        assert movement.from_address.value == "0x" + str(log["topics"][1])[-40:].lower()
        assert movement.to_address is not None
        assert movement.to_address.value == "0x" + str(log["topics"][2])[-40:].lower()

    natives = [m for m in normalized.movements if m.kind is MovementKind.NATIVE]
    expects_native = receipt.get("status") == "0x1" and int(tx_obj.get("value", "0x0"), 16) > 0
    assert len(natives) == (1 if expects_native else 0)
    assert normalized.tx.timestamp == datetime.fromtimestamp(int(block["timestamp"], 16), tz=UTC)
    assert normalized == await adapter.normalize(tx)  # idempotent


async def test_rpc_lookups_are_cached_forever(fixture_pool, manifest) -> None:
    adapter = EvmAdapter(ETHEREUM_CONFIG, fixture_pool)
    await adapter.transaction(manifest["eth_token_tx"])
    await adapter.transaction(manifest["eth_token_tx"])
    hits = fixture_pool.metrics.snapshot()["cache_hits"]
    assert hits.get("ethereum/tx_lookup") == 1
    assert hits.get("ethereum/tx_receipt") == 1
    assert hits.get("ethereum/block_lookup") == 1

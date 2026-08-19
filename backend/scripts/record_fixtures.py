#!/usr/bin/env python
"""Record real provider payloads as replay fixtures (one-time; needs network + keys).

Doubles as a live smoke test: every recorded fixture passed through the real
vendor clients, so envelope handling is verified against reality, not mocks.
Discovery picks small, currently-active addresses so fixtures stay tiny.

Raw response bytes are saved verbatim — tests replay them through
MockTransport so the full pool → client → adapter path runs offline.
No credentials ever appear in fixtures (keys live only in request URLs).
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from cipherchain.core.config import Settings
from cipherchain.core.models import Capability
from cipherchain.providers.base import ProviderRequest
from cipherchain.providers.clients import EtherscanV2Provider, EvmRpcProvider, MempoolSpaceProvider

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "chains" / "fixtures"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
MEMPOOL_BASE = "https://mempool.space/api"


def save(name: str, raw: bytes) -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    (FIXTURES / name).write_bytes(raw)
    print(f"  saved {name} ({len(raw)} bytes)")


async def record_bitcoin(http: httpx.AsyncClient) -> dict[str, Any]:
    print("BTC: discovering a small-history address ...")
    provider = MempoolSpaceProvider(http)
    blocks = (await http.get(f"{MEMPOOL_BASE}/blocks")).json()
    chosen: str | None = None
    for block in blocks[1:5]:
        txs = (await http.get(f"{MEMPOOL_BASE}/block/{block['id']}/txs")).json()
        for tx in txs[1:25]:
            for vout in tx.get("vout", []):
                address = vout.get("scriptpubkey_address")
                if not address:
                    continue
                stats = (await http.get(f"{MEMPOOL_BASE}/address/{address}")).json()
                await asyncio.sleep(0.15)
                if 1 <= stats["chain_stats"]["tx_count"] <= 6:
                    chosen = address
                    break
            if chosen:
                break
        if chosen:
            break
    if not chosen:
        raise SystemExit("BTC discovery failed: no small address found in recent blocks")
    print(f"  address: {chosen}")

    history = await provider.execute(
        ProviderRequest("bitcoin", Capability.ADDRESS_HISTORY, {"address": chosen})
    )
    save("btc_address_txs.json", history.raw)
    confirmed = [t for t in history.payload if t.get("status", {}).get("confirmed")]
    if not confirmed:
        raise SystemExit("BTC discovery failed: no confirmed txs for chosen address")
    txid = confirmed[0]["txid"]
    tx = await provider.execute(ProviderRequest("bitcoin", Capability.TX_LOOKUP, {"tx_hash": txid}))
    save("btc_tx.json", tx.raw)
    return {"btc_address": chosen, "btc_txid": txid}


async def record_ethereum(http: httpx.AsyncClient, settings: Settings) -> dict[str, Any]:
    print("ETH: fetching latest block ...")
    rpc = EvmRpcProvider(
        http,
        name="drpc",
        url=f"https://lb.drpc.org/ogrpc?network=ethereum&dkey={settings.drpc_api_key}",
        chain="ethereum",
    )
    etherscan = EtherscanV2Provider(
        http, api_key=settings.etherscan_api_key or "", chain_ids={"ethereum": 1}
    )
    block = await rpc.execute(
        ProviderRequest(
            "ethereum", Capability.BLOCK_LOOKUP, {"tag": "latest", "full_transactions": True}
        )
    )
    txs = block.payload["transactions"]
    print(f"  block {int(block.payload['number'], 16)} with {len(txs)} txs")

    print("ETH: discovering a small-history EOA ...")
    small: tuple[str, Any] | None = None
    checked = 0
    for tx in txs:
        if tx.get("input") in ("0x", "") and tx.get("to") and int(tx.get("value", "0x0"), 16) > 0:
            history = await etherscan.execute(
                ProviderRequest(
                    "ethereum", Capability.ADDRESS_HISTORY, {"address": tx["to"], "offset": 50}
                )
            )
            await asyncio.sleep(0.25)
            checked += 1
            if 1 <= len(history.payload) <= 40:
                small = (tx["to"], history)
                break
            if checked >= 12:
                break
    if small is None:
        raise SystemExit("ETH discovery failed: no small-history EOA found; rerun")
    eth_address, history = small
    save("eth_txlist.json", history.raw)
    print(f"  address: {eth_address} ({len(history.payload)} txs)")

    print("ETH: finding a tx carrying ERC-20 transfers ...")
    token_pair: tuple[dict[str, Any], Any] | None = None
    for tx in txs[:40]:
        receipt = await rpc.execute(
            ProviderRequest("ethereum", Capability.TX_RECEIPT, {"tx_hash": tx["hash"]})
        )
        await asyncio.sleep(0.1)
        logs = receipt.payload.get("logs") or []
        if any(
            log.get("topics")
            and log["topics"][0].lower() == TRANSFER_TOPIC
            and len(log["topics"]) == 3
            for log in logs
        ):
            token_pair = (tx, receipt)
            break
    if token_pair is None:
        raise SystemExit("ETH discovery failed: no ERC-20 transfer in latest block; rerun")
    token_tx, token_receipt = token_pair
    save("eth_rpc_receipt.json", token_receipt.raw)

    tx_detail = await rpc.execute(
        ProviderRequest("ethereum", Capability.TX_LOOKUP, {"tx_hash": token_tx["hash"]})
    )
    save("eth_rpc_tx.json", tx_detail.raw)
    block_detail = await rpc.execute(
        ProviderRequest(
            "ethereum", Capability.BLOCK_LOOKUP, {"number": int(token_tx["blockNumber"], 16)}
        )
    )
    save("eth_rpc_block.json", block_detail.raw)

    transfer_log = next(
        log
        for log in token_receipt.payload["logs"]
        if log.get("topics")
        and log["topics"][0].lower() == TRANSFER_TOPIC
        and len(log["topics"]) == 3
    )
    token_sender = "0x" + transfer_log["topics"][1][-40:]
    token_history = await etherscan.execute(
        ProviderRequest(
            "ethereum", Capability.TOKEN_TRANSFERS, {"address": token_sender, "offset": 10}
        )
    )
    save("eth_tokentx.json", token_history.raw)
    return {
        "eth_address": eth_address,
        "eth_token_tx": token_tx["hash"],
        "eth_token_sender": token_sender,
    }


async def main() -> None:
    settings = Settings()
    if not settings.etherscan_api_key or not settings.drpc_api_key:
        sys.exit("ETHERSCAN_API_KEY and DRPC_API_KEY must be set (see .env)")
    async with httpx.AsyncClient(timeout=25) as http:
        btc = await record_bitcoin(http)
        eth = await record_ethereum(http, settings)
    manifest = {**btc, **eth, "recorded_at": datetime.now(UTC).isoformat()}
    (FIXTURES / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print("manifest:", json.dumps(manifest, indent=2))


if __name__ == "__main__":
    asyncio.run(main())

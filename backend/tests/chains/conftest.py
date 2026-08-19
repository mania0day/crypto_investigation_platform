"""Replay harness: recorded fixtures served through the REAL stack.

Requests flow pool → vendor client → MockTransport(fixture bytes), so
adapter tests exercise routing, envelope parsing, and normalization exactly
as production does — minus the network.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from cipherchain.providers.cache import InMemoryCache
from cipherchain.providers.clients import EtherscanV2Provider, EvmRpcProvider, MempoolSpaceProvider
from cipherchain.providers.pool import ProviderPool

FIXTURES = Path(__file__).parent / "fixtures"

# Recorded live: a wallet whose entire native funding arrives as internal traces
# from the sanctioned Tornado Cash 0.1 ETH pool, invisible in its `txlist`.
MIXER_FUNDED_ADDRESS = "0x0a5b2bf3ccfb44c1d22f07eed9553ecba752d4ad"
TORNADO_01_ETH_POOL = "0x12d66f87a04a9e220743712ce6d9bb1b5616b8fc"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fixture_json(name: str) -> Any:
    return json.loads(fixture_bytes(name))


@pytest.fixture(scope="session")
def manifest() -> dict[str, Any]:
    data: dict[str, Any] = fixture_json("manifest.json")
    return data


def _route(request: httpx.Request) -> httpx.Response:
    host, path = request.url.host, request.url.path
    if host == "api.etherscan.io":
        action = str(request.url.params.get("action") or "")
        address = str(request.url.params.get("address") or "").lower()
        # The mixer-funded wallet is the forensic case: its native funding exists
        # ONLY as an internal trace, so its three feeds are recorded separately.
        by_action = (
            {
                "txlist": "eth_internal_mixer_txlist.json",
                "tokentx": "eth_internal_mixer_tokentx.json",
                "txlistinternal": "eth_internal_mixer.json",
            }
            if address == MIXER_FUNDED_ADDRESS
            else {
                "txlist": "eth_txlist.json",
                "tokentx": "eth_tokentx.json",
                "txlistinternal": "eth_txlistinternal.json",
            }
        )
        name = by_action.get(action)
        if name:
            return httpx.Response(200, content=fixture_bytes(name))
    if host == "rpc.fixture":
        body = json.loads(request.content)
        by_method = {
            "eth_getTransactionByHash": "eth_rpc_tx.json",
            "eth_getTransactionReceipt": "eth_rpc_receipt.json",
            "eth_getBlockByNumber": "eth_rpc_block.json",
        }
        name = by_method.get(body["method"])
        if name:
            return httpx.Response(200, content=fixture_bytes(name))
    if host == "mempool.space":
        if path.endswith("/txs") or "/txs/chain/" in path:
            return httpx.Response(200, content=fixture_bytes("btc_address_txs.json"))
        if "/tx/" in path:
            return httpx.Response(200, content=fixture_bytes("btc_tx.json"))
    return httpx.Response(404, text=f"unrouted fixture request: {request.url}")


@pytest.fixture
async def fixture_pool() -> AsyncIterator[ProviderPool]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(_route)) as http:
        pool = ProviderPool(cache=InMemoryCache())
        pool.register(MempoolSpaceProvider(http), priority=10)
        pool.register(
            EtherscanV2Provider(http, api_key="fixture", chain_ids={"ethereum": 1}), priority=10
        )
        pool.register(
            EvmRpcProvider(
                http, name="rpc-fixture", url="https://rpc.fixture/eth", chain="ethereum"
            ),
            priority=20,
        )
        yield pool

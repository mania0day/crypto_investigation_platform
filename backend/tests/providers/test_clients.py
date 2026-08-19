"""Vendor clients against mock transports: envelopes, quirks, error mapping."""

import json
from collections.abc import AsyncIterator, Callable

import httpx
import pytest

from cipherchain.core.errors import (
    ProviderRateLimited,
    ProviderResponseInvalid,
    ProviderUnavailable,
    ResourceNotFound,
)
from cipherchain.core.hashing import sha256_hex
from cipherchain.core.models import Capability
from cipherchain.providers.base import ProviderRequest
from cipherchain.providers.clients import (
    EtherscanV2Provider,
    EvmRpcProvider,
    MempoolSpaceProvider,
)

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
async def http_factory() -> AsyncIterator[Callable[[Handler], httpx.AsyncClient]]:
    clients: list[httpx.AsyncClient] = []

    def factory(handler: Handler) -> httpx.AsyncClient:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        clients.append(client)
        return client

    yield factory
    for client in clients:
        await client.aclose()


class TestEtherscan:
    def request(self) -> ProviderRequest:
        return ProviderRequest("ethereum", Capability.ADDRESS_HISTORY, {"address": "0xAbC"})

    async def test_success_sends_key_and_chainid(self, http_factory) -> None:
        seen: dict[str, str] = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen["url"] = str(req.url)
            return httpx.Response(
                200, json={"status": "1", "message": "OK", "result": [{"hash": "0x1"}]}
            )

        provider = EtherscanV2Provider(
            http_factory(handler), api_key="SECRET", chain_ids={"ethereum": 1}
        )
        response = await provider.execute(self.request())
        assert response.payload == [{"hash": "0x1"}]
        assert response.payload_sha256 == sha256_hex(response.raw)
        assert "apikey=SECRET" in seen["url"] and "chainid=1" in seen["url"]
        # the cache key never sees the credential
        assert "SECRET" not in json.dumps(dict(self.request().params))

    async def test_empty_history_is_valid(self, http_factory) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"status": "0", "message": "No transactions found", "result": []}
            )

        provider = EtherscanV2Provider(
            http_factory(handler), api_key="k", chain_ids={"ethereum": 1}
        )
        response = await provider.execute(self.request())
        assert response.payload == []

    async def test_rate_limit_in_body(self, http_factory) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "status": "0",
                    "message": "NOTOK",
                    "result": "Max rate limit reached, please use API Key",
                },
            )

        provider = EtherscanV2Provider(
            http_factory(handler), api_key="k", chain_ids={"ethereum": 1}
        )
        with pytest.raises(ProviderRateLimited):
            await provider.execute(self.request())

    async def test_server_error_maps_to_unavailable(self, http_factory) -> None:
        provider = EtherscanV2Provider(
            http_factory(lambda req: httpx.Response(502)),
            api_key="k",
            chain_ids={"ethereum": 1},
        )
        with pytest.raises(ProviderUnavailable):
            await provider.execute(self.request())

    async def test_invalid_key_never_retried_as_rate_limit(self, http_factory) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"status": "0", "message": "NOTOK", "result": "Invalid API Key"}
            )

        provider = EtherscanV2Provider(
            http_factory(handler), api_key="bad", chain_ids={"ethereum": 1}
        )
        with pytest.raises(ProviderResponseInvalid):
            await provider.execute(self.request())


class TestMempoolSpace:
    async def test_tx_lookup_and_404(self, http_factory) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path.endswith("/tx/f00d"):
                return httpx.Response(200, json={"txid": "f00d", "vin": [], "vout": []})
            return httpx.Response(404, text="Transaction not found")

        provider = MempoolSpaceProvider(http_factory(handler))
        found = await provider.execute(
            ProviderRequest("bitcoin", Capability.TX_LOOKUP, {"tx_hash": "f00d"})
        )
        assert found.payload["txid"] == "f00d"
        with pytest.raises(ResourceNotFound):
            await provider.execute(
                ProviderRequest("bitcoin", Capability.TX_LOOKUP, {"tx_hash": "dead"})
            )

    async def test_address_history_pagination_path(self, http_factory) -> None:
        paths: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            paths.append(req.url.path)
            return httpx.Response(200, json=[])

        provider = MempoolSpaceProvider(http_factory(handler))
        await provider.execute(
            ProviderRequest("bitcoin", Capability.ADDRESS_HISTORY, {"address": "bc1q"})
        )
        await provider.execute(
            ProviderRequest(
                "bitcoin",
                Capability.ADDRESS_HISTORY,
                {"address": "bc1q", "after_txid": "aaa"},
            )
        )
        assert paths == ["/api/address/bc1q/txs", "/api/address/bc1q/txs/chain/aaa"]

    async def test_block_by_height_two_step(self, http_factory) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            if "block-height" in req.url.path:
                return httpx.Response(200, text="deadbeef")
            assert req.url.path.endswith("/block/deadbeef")
            return httpx.Response(200, json={"id": "deadbeef", "height": 5})

        provider = MempoolSpaceProvider(http_factory(handler))
        response = await provider.execute(
            ProviderRequest("bitcoin", Capability.BLOCK_LOOKUP, {"height": 5})
        )
        assert response.payload == {"id": "deadbeef", "height": 5}


class TestEvmRpc:
    def provider(self, http: httpx.AsyncClient) -> EvmRpcProvider:
        return EvmRpcProvider(http, name="drpc", url="https://rpc.test/eth", chain="ethereum")

    async def test_tx_lookup_success(self, http_factory) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            body = json.loads(req.content)
            assert body["method"] == "eth_getTransactionByHash"
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"hash": "0x1"}})

        response = await self.provider(http_factory(handler)).execute(
            ProviderRequest("ethereum", Capability.TX_LOOKUP, {"tx_hash": "0x1"})
        )
        assert response.payload == {"hash": "0x1"}

    async def test_null_result_is_not_found(self, http_factory) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": None})

        with pytest.raises(ResourceNotFound):
            await self.provider(http_factory(handler)).execute(
                ProviderRequest("ethereum", Capability.TX_LOOKUP, {"tx_hash": "0x404"})
            )

    async def test_rpc_rate_error(self, http_factory) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {"code": -32005, "message": "rate limited"},
                },
            )

        with pytest.raises(ProviderRateLimited):
            await self.provider(http_factory(handler)).execute(
                ProviderRequest("ethereum", Capability.TX_LOOKUP, {"tx_hash": "0x1"})
            )

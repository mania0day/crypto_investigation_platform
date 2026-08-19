"""Generic EVM JSON-RPC client — Class B raw node access.

One instance per (vendor, chain, endpoint URL): dRPC, Ankr, Alchemy, Infura
and later endpoints all use this same class with different names and URLs —
vendors are configuration, not code.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from cipherchain.core.errors import (
    ProviderRateLimited,
    ProviderResponseInvalid,
    ResourceNotFound,
)
from cipherchain.core.hashing import sha256_hex
from cipherchain.core.models import Capability
from cipherchain.providers.base import Provider, ProviderRequest, ProviderResponse
from cipherchain.providers.clients._http import perform

_CAPABILITIES = frozenset(
    {
        Capability.TX_LOOKUP,
        Capability.TX_RECEIPT,
        Capability.BLOCK_LOOKUP,
        Capability.BALANCE,
        Capability.LOGS,
    }
)

# result: null is an authoritative "does not exist" for these lookups.
_NULLABLE_MEANS_MISSING = frozenset(
    {Capability.TX_LOOKUP, Capability.TX_RECEIPT, Capability.BLOCK_LOOKUP}
)


class EvmRpcProvider(Provider):
    def __init__(self, http: httpx.AsyncClient, *, name: str, url: str, chain: str) -> None:
        self.name = name
        self._http = http
        self._url = url
        self._chain = chain

    def supports(self, chain: str, capability: Capability) -> bool:
        return chain == self._chain and capability in _CAPABILITIES

    @staticmethod
    def _method_and_params(request: ProviderRequest) -> tuple[str, list[Any]]:
        params = dict(request.params)
        capability = request.capability
        if capability is Capability.TX_LOOKUP:
            return "eth_getTransactionByHash", [str(params["tx_hash"])]
        if capability is Capability.TX_RECEIPT:
            return "eth_getTransactionReceipt", [str(params["tx_hash"])]
        if capability is Capability.BLOCK_LOOKUP:
            block = hex(int(params["number"])) if "number" in params else str(params["tag"])
            return "eth_getBlockByNumber", [block, bool(params.get("full_transactions", False))]
        if capability is Capability.BALANCE:
            return "eth_getBalance", [str(params["address"]), "latest"]
        if capability is Capability.LOGS:
            return "eth_getLogs", [dict(params["filter"])]
        raise ValueError(f"unsupported capability {capability}")  # pragma: no cover

    async def execute(self, request: ProviderRequest) -> ProviderResponse:
        method, rpc_params = self._method_and_params(request)
        response = await perform(
            self._http,
            self.name,
            "POST",
            self._url,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": rpc_params},
        )
        if response.status_code >= 400:
            raise ProviderResponseInvalid(self.name, f"HTTP {response.status_code}")
        raw = response.content
        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderResponseInvalid(self.name, "non-JSON body") from exc
        if not isinstance(body, dict):
            raise ProviderResponseInvalid(self.name, "malformed JSON-RPC envelope")
        error = body.get("error")
        if error:
            code = error.get("code") if isinstance(error, dict) else None
            message = str(error.get("message", "")) if isinstance(error, dict) else str(error)
            lowered = message.lower()
            if code in (-32005, 429) or "rate" in lowered or "too many" in lowered:
                raise ProviderRateLimited(self.name, message)
            raise ProviderResponseInvalid(self.name, f"RPC error {code}: {message}")
        if "result" not in body:
            raise ProviderResponseInvalid(self.name, "missing result")
        result = body["result"]
        if result is None and request.capability in _NULLABLE_MEANS_MISSING:
            raise ResourceNotFound(self.name, f"{method} returned null")
        return ProviderResponse(
            provider=self.name,
            retrieved_at=datetime.now(UTC),
            payload=result,
            raw=raw,
            payload_sha256=sha256_hex(raw),
        )

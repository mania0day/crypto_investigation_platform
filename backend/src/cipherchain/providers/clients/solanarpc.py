"""Solana JSON-RPC client.

Solana has no batched address-history endpoint: history is a signature list
(``getSignaturesForAddress``) plus one ``getTransaction`` per signature. The
adapter absorbs that fan-out; the pool cache makes repeats free.
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
        Capability.ADDRESS_HISTORY,
        Capability.TX_LOOKUP,
        Capability.BALANCE,
        Capability.BLOCK_LOOKUP,
    }
)


class SolanaRpcProvider(Provider):
    def __init__(
        self, http: httpx.AsyncClient, *, name: str, url: str, chain: str = "solana"
    ) -> None:
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
        if capability is Capability.ADDRESS_HISTORY:
            options: dict[str, Any] = {"limit": int(params.get("limit", 25))}
            if params.get("before"):
                options["before"] = str(params["before"])
            return "getSignaturesForAddress", [str(params["address"]), options]
        if capability is Capability.TX_LOOKUP:
            return "getTransaction", [
                str(params["tx_hash"]),
                {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0},
            ]
        if capability is Capability.BALANCE:
            return "getBalance", [str(params["address"])]
        if capability is Capability.BLOCK_LOOKUP:
            return "getBlock", [
                int(params["slot"]),
                {
                    "encoding": "jsonParsed",
                    "maxSupportedTransactionVersion": 0,
                    "transactionDetails": "accounts",
                    "rewards": False,
                },
            ]
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
            if code == 429 or "rate" in lowered or "too many" in lowered:
                raise ProviderRateLimited(self.name, message)
            raise ProviderResponseInvalid(self.name, f"RPC error {code}: {message}")
        if "result" not in body:
            raise ProviderResponseInvalid(self.name, "missing result")
        result = body["result"]
        # A null result for a specific lookup is an authoritative "no such
        # thing" — an answer, not a failure, so it must not trigger failover.
        if result is None and request.capability in (
            Capability.TX_LOOKUP,
            Capability.BLOCK_LOOKUP,
        ):
            raise ResourceNotFound(self.name, f"{method} returned null")
        if request.capability is Capability.BALANCE and isinstance(result, dict):
            result = result.get("value")
        return ProviderResponse(
            provider=self.name,
            retrieved_at=datetime.now(UTC),
            payload=result,
            raw=raw,
            payload_sha256=sha256_hex(raw),
        )

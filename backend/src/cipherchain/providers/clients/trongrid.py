"""TronGrid client.

Serves Tron history keylessly at a modest rate; an optional free API key
raises the limit and is sent as ``TRON-PRO-API-KEY`` when configured.

Two separate history endpoints exist — native transactions and TRC-20
transfers — and the adapter merges them, so this client just maps each
capability to its endpoint.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from cipherchain.core.errors import ProviderResponseInvalid, ResourceNotFound
from cipherchain.core.hashing import sha256_hex
from cipherchain.core.models import Capability
from cipherchain.providers.base import Provider, ProviderRequest, ProviderResponse
from cipherchain.providers.clients._http import perform

_CAPABILITIES = frozenset(
    {
        Capability.ADDRESS_HISTORY,
        Capability.TOKEN_TRANSFERS,
        Capability.TX_LOOKUP,
    }
)


class TronGridProvider(Provider):
    name = "trongrid"

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        api_key: str | None = None,
        base_url: str = "https://api.trongrid.io",
        chain: str = "tron",
    ) -> None:
        self._http = http
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._chain = chain

    def supports(self, chain: str, capability: Capability) -> bool:
        return chain == self._chain and capability in _CAPABILITIES

    def _headers(self) -> dict[str, str]:
        # Credentials attach here, never in ProviderRequest.params — so they
        # can never leak into a cache key.
        return {"TRON-PRO-API-KEY": self._api_key} if self._api_key else {}

    async def execute(self, request: ProviderRequest) -> ProviderResponse:
        params = dict(request.params)
        capability = request.capability
        limit = int(params.get("limit", 100))

        if capability is Capability.ADDRESS_HISTORY:
            path = f"/v1/accounts/{params['address']}/transactions"
        elif capability is Capability.TOKEN_TRANSFERS:
            path = f"/v1/accounts/{params['address']}/transactions/trc20"
        elif capability is Capability.TX_LOOKUP:
            # Single-transaction lookup is a POST on the wallet API.
            response = await perform(
                self._http,
                self.name,
                "POST",
                f"{self._base_url}/wallet/gettransactionbyid",
                json={"value": str(params["tx_hash"])},
                headers=self._headers(),
            )
            return self._respond(response, expect_list=False)
        else:  # pragma: no cover - pool routes only supported capabilities
            raise ProviderResponseInvalid(self.name, f"unsupported capability {capability}")

        query: dict[str, Any] = {"limit": limit}
        # TronGrid pages by opaque fingerprint. It travels in `params` — never a
        # credential — so it lands in the cache key and page 2 cannot collide
        # with page 1.
        fingerprint = params.get("fingerprint")
        if fingerprint:
            query["fingerprint"] = str(fingerprint)

        response = await perform(
            self._http,
            self.name,
            "GET",
            f"{self._base_url}{path}",
            params=query,
            headers=self._headers(),
        )
        return self._respond(response, expect_list=True)

    def _respond(self, response: httpx.Response, *, expect_list: bool) -> ProviderResponse:
        if response.status_code == 404:
            raise ResourceNotFound(self.name, "not found")
        if response.status_code >= 400:
            raise ProviderResponseInvalid(self.name, f"HTTP {response.status_code}")
        raw = response.content
        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderResponseInvalid(self.name, "non-JSON body") from exc

        if expect_list:
            if not isinstance(body, dict) or "data" not in body:
                raise ProviderResponseInvalid(self.name, "missing data envelope")
            if body.get("success") is False:
                raise ProviderResponseInvalid(self.name, str(body.get("error"))[:200])
            # The WHOLE envelope, not just `data`: `meta.fingerprint` is how
            # TronGrid pages, and discarding it here is what left the Tron
            # adapter unable to page at all — so every Tron address was read
            # once and then recorded as fully read.
            payload: Any = body
        else:
            # gettransactionbyid returns {} for an unknown hash — an
            # authoritative answer, not a failure.
            if not body:
                raise ResourceNotFound(self.name, "no such transaction")
            payload = body

        return ProviderResponse(
            provider=self.name,
            retrieved_at=datetime.now(UTC),
            payload=payload,
            raw=raw,
            payload_sha256=sha256_hex(raw),
        )

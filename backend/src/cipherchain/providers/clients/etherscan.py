"""Etherscan V2 — Class A indexed history for the whole Etherscan chain
family through one key and a ``chainid`` parameter.

Quirks encoded here (verified live 2026-08-07):
- The envelope is {"status", "message", "result"}; status "0" covers BOTH
  benign emptiness ("No transactions found") and errors, and ``result``
  may be a string carrying the error text.
- Rate limiting arrives as status "0" with "rate limit" in the text, not
  as HTTP 429.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import httpx

from cipherchain.core.errors import ProviderRateLimited, ProviderResponseInvalid
from cipherchain.core.hashing import sha256_hex
from cipherchain.core.models import Capability
from cipherchain.providers.base import Provider, ProviderRequest, ProviderResponse
from cipherchain.providers.clients._http import perform

_ACTIONS: Mapping[Capability, str] = {
    Capability.ADDRESS_HISTORY: "txlist",
    Capability.TOKEN_TRANSFERS: "tokentx",
    Capability.INTERNAL_TRACES: "txlistinternal",
}

_EMPTY_MARKERS = ("no transactions found", "no internal transactions found", "no token transfers")


class EtherscanV2Provider(Provider):
    name = "etherscan"

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        api_key: str,
        chain_ids: Mapping[str, int],
        base_url: str = "https://api.etherscan.io/v2/api",
    ) -> None:
        self._http = http
        self._api_key = api_key
        self._chain_ids = dict(chain_ids)
        self._base_url = base_url

    def supports(self, chain: str, capability: Capability) -> bool:
        return chain in self._chain_ids and capability in _ACTIONS

    async def execute(self, request: ProviderRequest) -> ProviderResponse:
        params = dict(request.params)
        query = {
            "chainid": str(self._chain_ids[request.chain]),
            "module": "account",
            "action": _ACTIONS[request.capability],
            "address": str(params["address"]),
            "startblock": str(params.get("startblock", 0)),
            "endblock": str(params.get("endblock", 99_999_999)),
            "page": str(params.get("page", 1)),
            "offset": str(params.get("offset", 100)),
            "sort": str(params.get("sort", "desc")),
            "apikey": self._api_key,  # credentials attach here, never in request.params
        }
        response = await perform(self._http, self.name, "GET", self._base_url, params=query)
        raw = response.content
        body = self._parse_envelope(response)
        payload = self._extract_result(body)
        return ProviderResponse(
            provider=self.name,
            retrieved_at=datetime.now(UTC),
            payload=payload,
            raw=raw,
            payload_sha256=sha256_hex(raw),
        )

    def _parse_envelope(self, response: httpx.Response) -> dict[str, Any]:
        if response.status_code >= 400:
            raise ProviderResponseInvalid(self.name, f"HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderResponseInvalid(self.name, "non-JSON body") from exc
        if not isinstance(body, dict) or "status" not in body:
            raise ProviderResponseInvalid(self.name, "missing envelope")
        return body

    def _extract_result(self, body: dict[str, Any]) -> list[Any]:
        status = body.get("status")
        result = body.get("result")
        if status == "1":
            if not isinstance(result, list):
                raise ProviderResponseInvalid(self.name, "status 1 without list result")
            return result
        message = str(body.get("message", ""))
        text = f"{message} {result}".strip() if isinstance(result, str) else message
        lowered = text.lower()
        if "rate limit" in lowered:
            raise ProviderRateLimited(self.name, text)
        if any(marker in lowered for marker in _EMPTY_MARKERS) or (
            isinstance(result, list) and not result
        ):
            return []
        if "invalid api key" in lowered:
            raise ProviderResponseInvalid(self.name, "invalid API key")
        raise ProviderResponseInvalid(self.name, text or "status 0 without message")

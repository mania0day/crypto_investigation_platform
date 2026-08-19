"""mempool.space — Class A/B Bitcoin data, keyless public instance.

Transaction payloads include prevout data on inputs, which is what lets the
Bitcoin adapter emit UTXO input halves without extra lookups. Self-hosting
(the mempool repo) is the documented escape hatch if public limits bind;
``base_url`` is the only thing that changes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from cipherchain.core.errors import ProviderResponseInvalid, ResourceNotFound
from cipherchain.core.hashing import sha256_hex
from cipherchain.core.models import Capability
from cipherchain.providers.base import Provider, ProviderRequest, ProviderResponse
from cipherchain.providers.clients._http import perform

_CAPABILITIES = frozenset(
    {
        Capability.ADDRESS_HISTORY,
        Capability.TX_LOOKUP,
        Capability.UTXO_LOOKUP,
        Capability.BLOCK_LOOKUP,
    }
)


def _seg(value: object) -> str:
    """Percent-encode a single URL path segment (no unescaped '/', '?', '#')."""
    return quote(str(value), safe="")


class MempoolSpaceProvider(Provider):
    name = "mempool.space"

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        base_url: str = "https://mempool.space/api",
        chain: str = "bitcoin",
    ) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._chain = chain

    def supports(self, chain: str, capability: Capability) -> bool:
        return chain == self._chain and capability in _CAPABILITIES

    async def _get(self, path: str) -> httpx.Response:
        response = await perform(self._http, self.name, "GET", f"{self._base_url}{path}")
        if response.status_code == 404:
            raise ResourceNotFound(self.name, f"not found: {path}")
        if response.status_code >= 400:
            raise ProviderResponseInvalid(
                self.name, f"HTTP {response.status_code}: {response.text[:200]}"
            )
        return response

    async def execute(self, request: ProviderRequest) -> ProviderResponse:
        # Every caller-supplied value is percent-encoded before it enters the
        # URL path: an unencoded address/txid could carry ``../`` or ``?``/``#``
        # and forge the request to an unintended path (REVIEW_FINDINGS.md #13).
        params = dict(request.params)
        capability = request.capability
        if capability is Capability.ADDRESS_HISTORY:
            address = _seg(params["address"])
            after_txid = params.get("after_txid")
            path = f"/address/{address}/txs"
            if after_txid:
                path = f"/address/{address}/txs/chain/{_seg(after_txid)}"
            response = await self._get(path)
        elif capability is Capability.TX_LOOKUP:
            response = await self._get(f"/tx/{_seg(params['tx_hash'])}")
        elif capability is Capability.UTXO_LOOKUP:
            response = await self._get(f"/address/{_seg(params['address'])}/utxo")
        elif capability is Capability.BLOCK_LOOKUP:
            if "hash" in params:
                response = await self._get(f"/block/{_seg(params['hash'])}")
            else:
                # /block-height returns the block hash as plain text.
                height_response = await self._get(f"/block-height/{int(params['height'])}")
                response = await self._get(f"/block/{_seg(height_response.text.strip())}")
        else:  # pragma: no cover - pool routes only supported capabilities
            raise ProviderResponseInvalid(self.name, f"unsupported capability {capability}")

        raw = response.content
        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise ProviderResponseInvalid(self.name, "non-JSON body") from exc
        return ProviderResponse(
            provider=self.name,
            retrieved_at=datetime.now(UTC),
            payload=payload,
            raw=raw,
            payload_sha256=sha256_hex(raw),
        )

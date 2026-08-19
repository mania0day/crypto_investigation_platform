"""Blockscout — the keyless fallback tier for EVM history.

Registered at the lowest priority in the pool, so it is reached only after
the keyed providers have failed or spent their quota. That is the whole
point: when Etherscan's allowance is gone, a trace should slow down, not
stop (REACHING_THE_VASP.md §5).

Why this and not page scraping
------------------------------
The instruction was "if the API is exhausted, fetch it another way, even if
it is slow". Scraping the obvious explorers does not satisfy that: measured
2026-08-13, ``mempool.space`` and ``blockstream.info`` address pages are
JavaScript shells — the address does not appear in the served HTML at all —
and ``etherscan.io/robots.txt`` answers a redirect to a challenge. Parsing
those means driving a headless browser per address for data we can request
directly. Blockscout publishes the same data over a documented, keyless,
``Allow: /`` API, so it is both faster and cleaner, and it drops into the
identical place in the pool.

Vendor quirks encoded here (verified live 2026-08-13)
-----------------------------------------------------
- The envelope is Etherscan's ``{"status", "message", "result"}`` and the
  ``txlist`` / ``tokentx`` row shapes are identical, so ``EvmAdapter``
  normalizes them unchanged.
- ``txlistinternal`` is the exception: its rows carry **``transactionHash``**
  where Etherscan carries ``hash``. Left alone this raises ``KeyError:
  'hash'`` inside the adapter on the first internal-trace fetch — caught by
  a live call, not by a mock. It is repaired HERE rather than in the adapter,
  because a vendor's spelling must not become something the layers above the
  pool have to know about.
- ``status`` may be **"2"**: a partial answer carrying real rows, seen on
  ``txlistinternal`` with "Some internal transactions within this block
  range have not yet been processed". Treating it as an error would discard
  live movements, so the rows are kept. The residual incompleteness is NOT
  yet reflected in the coverage statement — see the note on `_PARTIAL`.
- The chain is chosen by HOST, not by a ``chainid`` parameter.
- No credential exists, so there is nothing to keep out of the cache key.
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

# Public instances, one per chain. Adding a chain is a table edit.
DEFAULT_INSTANCES: Mapping[str, str] = {
    "ethereum": "https://eth.blockscout.com/api",
    "polygon": "https://polygon.blockscout.com/api",
}

_EMPTY_MARKERS = ("no transactions found", "no internal transactions found", "no token transfers")

# Status "2" means "these rows are real, and there may be more we have not
# indexed yet". Keeping the rows is right — dropping real movements to punish
# a vendor's honesty would be worse — but a reader cannot currently tell this
# feed was partial, because truncation is reported from the page cursor.
_PARTIAL = "2"


def _align_rows(rows: list[Any]) -> list[Any]:
    """Spell the transaction hash the way every other EVM feed spells it.

    Only ``payload`` is touched. ``raw`` stays the untouched vendor bytes, so
    the digest a finding cites still verifies against what was transmitted.
    """
    aligned: list[Any] = []
    for row in rows:
        if isinstance(row, dict) and "hash" not in row and "transactionHash" in row:
            row = {**row, "hash": row["transactionHash"]}
        aligned.append(row)
    return aligned


class BlockscoutProvider(Provider):
    name = "blockscout"

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        instances: Mapping[str, str] | None = None,
        user_agent: str = "CipherChain-investigation/0.1 (+blockchain investigation research)",
    ) -> None:
        self._http = http
        self._instances = dict(instances or DEFAULT_INSTANCES)
        # Identify ourselves rather than impersonating a browser: this is a
        # tool making documented API calls, and it should be attributable.
        self._headers = {"User-Agent": user_agent}

    def supports(self, chain: str, capability: Capability) -> bool:
        return chain in self._instances and capability in _ACTIONS

    async def execute(self, request: ProviderRequest) -> ProviderResponse:
        params = dict(request.params)
        query = {
            "module": "account",
            "action": _ACTIONS[request.capability],
            "address": str(params["address"]),
            "startblock": str(params.get("startblock", 0)),
            "endblock": str(params.get("endblock", 99_999_999)),
            "page": str(params.get("page", 1)),
            "offset": str(params.get("offset", 100)),
            "sort": str(params.get("sort", "desc")),
        }
        response = await perform(
            self._http,
            self.name,
            "GET",
            self._instances[request.chain],
            params=query,
            headers=self._headers,
        )
        raw = response.content
        payload = _align_rows(self._extract_result(self._parse_envelope(response)))
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
        if not isinstance(body, dict) or "result" not in body:
            raise ProviderResponseInvalid(self.name, "missing envelope")
        return body

    def _extract_result(self, body: dict[str, Any]) -> list[Any]:
        status = str(body.get("status", ""))
        result = body.get("result")
        if status in ("1", _PARTIAL):
            if not isinstance(result, list):
                raise ProviderResponseInvalid(self.name, f"status {status} without list result")
            return result
        message = str(body.get("message", ""))
        text = f"{message} {result}".strip() if isinstance(result, str) else message
        lowered = text.lower()
        if "rate limit" in lowered or "too many requests" in lowered:
            raise ProviderRateLimited(self.name, text)
        if any(marker in lowered for marker in _EMPTY_MARKERS) or (
            isinstance(result, list) and not result
        ):
            return []
        raise ProviderResponseInvalid(self.name, text or f"status {status} without message")

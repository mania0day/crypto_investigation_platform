"""Provider contracts: requests, responses, and the Provider interface."""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from cipherchain.core.hashing import sha256_canonical_json
from cipherchain.core.models import Capability, Provenance


class CachePolicy(enum.Enum):
    """How responses for a capability may be cached.

    Immutable chain data (a transaction, a receipt, a mined block) is cached
    forever. Views that grow over time (an address's history) get a TTL.
    Live state (a balance) is never cached.
    """

    FOREVER = "forever"
    TTL = "ttl"
    NEVER = "never"


DEFAULT_CACHE_POLICIES: Mapping[Capability, CachePolicy] = {
    Capability.TX_LOOKUP: CachePolicy.FOREVER,
    Capability.TX_RECEIPT: CachePolicy.FOREVER,
    Capability.BLOCK_LOOKUP: CachePolicy.FOREVER,
    Capability.INTERNAL_TRACES: CachePolicy.TTL,
    Capability.LOGS: CachePolicy.TTL,
    Capability.ADDRESS_HISTORY: CachePolicy.TTL,
    Capability.TOKEN_TRANSFERS: CachePolicy.TTL,
    Capability.UTXO_LOOKUP: CachePolicy.TTL,
    Capability.BALANCE: CachePolicy.NEVER,
}


# A provider that answered with fewer rows than the feed holds, and that cannot
# mint a cursor for the rest, sets this key on its PAYLOAD so the adapter above
# can carry the shortfall out in ``HistoryPage.truncated``.
#
# On the payload rather than on ``ProviderResponse`` because ``ProviderPool``
# stores and replays only (provider, retrieved_at, payload_json, raw,
# payload_sha256): a new response FIELD would be dropped by the cache, so the
# first read of a Tron address would report the short read and every read for
# the rest of the TTL would report that same address as completely read. On the
# payload rather than in the manifest (``raw``) because no adapter parses the
# manifest — the fetch tier recorded its truncation there and it reached nothing
# above the provider, which is how a cut read arrived in the report as a
# finished one.
SHORT_READ_KEY = "cipherchain_short_read"


@dataclass(frozen=True)
class ProviderRequest:
    """A capability request in canonical, vendor-neutral form.

    ``params`` must never contain credentials — clients attach API keys at
    execute time. The cache key is derived from (chain, capability, params),
    so any provider's answer can serve any other provider's identical
    request, and secrets can never leak into cache keys.
    """

    chain: str
    capability: Capability
    params: Mapping[str, Any] = field(default_factory=dict)

    def cache_key(self) -> str:
        return sha256_canonical_json(
            {
                "chain": self.chain,
                "capability": str(self.capability),
                "params": dict(self.params),
            }
        )


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    """A provider's answer plus everything evidence needs.

    ``raw`` is the exact bytes the vendor sent; ``payload_sha256`` is their
    digest — the content address findings cite (vision §4). ``payload`` is
    the parsed, capability-specific value.
    """

    provider: str
    retrieved_at: datetime
    payload: Any
    raw: bytes
    payload_sha256: str
    from_cache: bool = False

    def provenance(self) -> Provenance:
        return Provenance(
            provider=self.provider,
            retrieved_at=self.retrieved_at,
            payload_sha256=self.payload_sha256,
        )


class Provider(ABC):
    """One external data source. Implementations map capabilities to vendor
    endpoints and translate vendor errors into the core error hierarchy:

    - transport failure / 5xx      → ProviderUnavailable   (retried, breaker counts it)
    - HTTP 429 / vendor rate error → ProviderRateLimited   (retried, limiter penalized)
    - contract violation           → ProviderResponseInvalid (never retried on this provider)
    - authoritative "no such thing"→ ResourceNotFound       (an answer; propagated)
    """

    name: str = "provider"

    @abstractmethod
    def supports(self, chain: str, capability: Capability) -> bool: ...

    @abstractmethod
    async def execute(self, request: ProviderRequest) -> ProviderResponse: ...

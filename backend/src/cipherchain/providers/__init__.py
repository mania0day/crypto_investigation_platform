"""Provider SDK — capability-routed access to external data sources.

The pool is the single gateway to the outside world for chain data. Every
call passes the middleware pipeline (CAPABILITY_MATRIX.md §8):

    capability router → cache → rate limiter → retry → circuit breaker → vendor

with metrics wrapping the entire pipeline. The cache is checked before the
rate limiter on purpose: chain data is immutable and a cache hit must cost
zero rate budget.

Nothing above this package may know which vendor served a request; provider
names surface only in provenance and metrics.

Vendor clients live in ``cipherchain.providers.clients``; nothing above the pool
imports them directly.
"""

from cipherchain.providers.base import (
    CachePolicy,
    Provider,
    ProviderRequest,
    ProviderResponse,
)
from cipherchain.providers.breaker import CircuitBreaker, CircuitState
from cipherchain.providers.cache import CacheBackend, CachedEntry, InMemoryCache
from cipherchain.providers.metrics import MetricsRegistry
from cipherchain.providers.pool import ProviderLimits, ProviderPool
from cipherchain.providers.ratelimit import TokenBucket

__all__ = [
    "CacheBackend",
    "CachePolicy",
    "CachedEntry",
    "CircuitBreaker",
    "CircuitState",
    "InMemoryCache",
    "MetricsRegistry",
    "Provider",
    "ProviderLimits",
    "ProviderPool",
    "ProviderRequest",
    "ProviderResponse",
    "TokenBucket",
]

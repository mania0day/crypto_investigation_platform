"""ProviderPool — the middleware pipeline around every vendor call.

    capability router → cache → rate limiter → retry → circuit breaker → vendor
    (metrics wrap the entire pipeline)

Semantics per error class:
- ProviderRateLimited: the vendor is alive but throttling — penalize the
  bucket, honor Retry-After, retry the SAME provider; never trips the
  breaker.
- ProviderUnavailable: transport/5xx — counts toward the breaker, retried
  with jittered exponential backoff, then fail over to the next provider.
- ProviderResponseInvalid: a malformed answer is not transient — never
  retried on the same provider; fail over immediately.
- ResourceNotFound: an authoritative answer — propagate without failover.

Clock, sleep, and rng are injectable so every behavior above is testable
deterministically and instantly.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from cipherchain.core.errors import (
    AllProvidersFailed,
    ProviderRateLimited,
    ProviderResponseInvalid,
    ProviderUnavailable,
    ResourceNotFound,
)
from cipherchain.core.hashing import canonical_json_bytes
from cipherchain.core.models import Capability
from cipherchain.providers.base import (
    DEFAULT_CACHE_POLICIES,
    CachePolicy,
    Provider,
    ProviderRequest,
    ProviderResponse,
)
from cipherchain.providers.breaker import CircuitBreaker, CircuitState
from cipherchain.providers.cache import CacheBackend, CachedEntry
from cipherchain.providers.metrics import MetricsRegistry
from cipherchain.providers.ratelimit import TokenBucket


@dataclass(frozen=True, slots=True)
class ProviderLimits:
    """Rate limit for one registered provider, pinned at/below its free tier."""

    rate_per_sec: float = 5.0
    burst: float = 5.0


@dataclass
class _Registered:
    provider: Provider
    bucket: TokenBucket
    breaker: CircuitBreaker
    priority: int


class ProviderPool:
    def __init__(
        self,
        *,
        cache: CacheBackend | None = None,
        metrics: MetricsRegistry | None = None,
        max_attempts_per_provider: int = 3,
        backoff_base: float = 0.5,
        backoff_cap: float = 8.0,
        max_honoured_retry_after: float = 10.0,
        ttl_seconds: float = 300.0,
        cache_policies: Mapping[Capability, CachePolicy] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        if max_attempts_per_provider < 1:
            raise ValueError("max_attempts_per_provider must be >= 1")
        self._cache = cache
        self._metrics = metrics if metrics is not None else MetricsRegistry()
        self._max_attempts = max_attempts_per_provider
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._max_honoured_retry_after = max_honoured_retry_after
        self._ttl_seconds = ttl_seconds
        self._policies = dict(cache_policies or DEFAULT_CACHE_POLICIES)
        self._clock = clock
        self._sleep = sleep
        self._rng = rng
        self._registered: list[_Registered] = []

    @property
    def metrics(self) -> MetricsRegistry:
        return self._metrics

    def register(
        self,
        provider: Provider,
        *,
        limits: ProviderLimits | None = None,
        priority: int = 100,
        failure_threshold: int = 5,
        reset_timeout: float = 30.0,
    ) -> None:
        limits = limits or ProviderLimits()
        self._registered.append(
            _Registered(
                provider=provider,
                bucket=TokenBucket(
                    limits.rate_per_sec, limits.burst, clock=self._clock, sleep=self._sleep
                ),
                breaker=CircuitBreaker(
                    failure_threshold=failure_threshold,
                    reset_timeout=reset_timeout,
                    clock=self._clock,
                ),
                priority=priority,
            )
        )
        self._registered.sort(key=lambda r: r.priority)

    def _backoff(self, attempt: int) -> float:
        delay = min(self._backoff_cap, self._backoff_base * (2.0**attempt))
        return delay * (0.5 + 0.5 * self._rng())

    async def _cache_lookup(
        self, request: ProviderRequest, policy: CachePolicy
    ) -> ProviderResponse | None:
        if self._cache is None or policy is CachePolicy.NEVER:
            return None
        max_age = None if policy is CachePolicy.FOREVER else self._ttl_seconds
        entry = await self._cache.get(request.cache_key(), max_age_seconds=max_age)
        if entry is None:
            return None
        self._metrics.record_cache_hit(request.chain, str(request.capability))
        return ProviderResponse(
            provider=entry.provider,
            retrieved_at=entry.retrieved_at,
            payload=json.loads(entry.payload_json),
            raw=entry.raw,
            payload_sha256=entry.payload_sha256,
            from_cache=True,
        )

    async def _cache_store(
        self, request: ProviderRequest, response: ProviderResponse, policy: CachePolicy
    ) -> None:
        if self._cache is None or policy is CachePolicy.NEVER:
            return
        await self._cache.put(
            request.cache_key(),
            CachedEntry(
                chain=request.chain,
                capability=str(request.capability),
                provider=response.provider,
                retrieved_at=response.retrieved_at,
                payload_json=canonical_json_bytes(response.payload),
                raw=response.raw,
                payload_sha256=response.payload_sha256,
            ),
        )

    async def fetch(self, request: ProviderRequest) -> ProviderResponse:
        policy = self._policies.get(request.capability, CachePolicy.FOREVER)
        cached = await self._cache_lookup(request, policy)
        if cached is not None:
            return cached

        capability = str(request.capability)
        candidates = [
            r for r in self._registered if r.provider.supports(request.chain, request.capability)
        ]
        if not candidates:
            raise AllProvidersFailed(request.chain, capability, 0)

        attempts_total = 0
        for position, reg in enumerate(candidates):
            if position > 0:
                self._metrics.record_fallback(request.chain, capability)
            if not reg.breaker.allow():
                continue
            name = reg.provider.name
            for attempt in range(self._max_attempts):
                await reg.bucket.acquire()
                started = self._clock()
                try:
                    response = await reg.provider.execute(request)
                except ProviderRateLimited as exc:
                    attempts_total += 1
                    self._metrics.record_error(name, capability, "rate_limited")
                    reg.bucket.penalize()
                    if attempt + 1 >= self._max_attempts:
                        break
                    delay = exc.retry_after if exc.retry_after is not None else None
                    if delay is not None and delay > self._max_honoured_retry_after:
                        # A long Retry-After is the vendor saying "not for a
                        # while". Sleeping it out is the one thing this pool
                        # must not do: a 3600s header seen twice freezes a trace
                        # for two hours, which is stopped, not slowed — the
                        # opposite of why the fallback tiers exist. Honouring it
                        # only makes sense with nowhere else to go, and having
                        # somewhere else to go is what a pool IS.
                        #
                        # So the bucket keeps the penalty (we do not hammer them
                        # on the next call) and we fail over NOW.
                        break
                    await self._sleep(delay if delay is not None else self._backoff(attempt))
                except ProviderUnavailable:
                    attempts_total += 1
                    self._metrics.record_error(name, capability, "unavailable")
                    reg.breaker.record_failure()
                    if reg.breaker.state is CircuitState.OPEN or attempt + 1 >= self._max_attempts:
                        break
                    await self._sleep(self._backoff(attempt))
                except ProviderResponseInvalid:
                    attempts_total += 1
                    self._metrics.record_error(name, capability, "invalid")
                    reg.breaker.record_failure()
                    break  # a malformed answer is not transient: fail over
                except ResourceNotFound:
                    # An answer, not a failure — the provider works fine.
                    reg.breaker.record_success()
                    self._metrics.record_success(name, capability, self._clock() - started)
                    raise
                else:
                    reg.breaker.record_success()
                    self._metrics.record_success(name, capability, self._clock() - started)
                    await self._cache_store(request, response, policy)
                    return response

        raise AllProvidersFailed(request.chain, capability, attempts_total)

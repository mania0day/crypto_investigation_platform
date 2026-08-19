"""Pipeline semantics: cache-first, retry, failover, breaker, not-found."""

from datetime import UTC, datetime

import pytest

from cipherchain.core.errors import (
    AllProvidersFailed,
    ProviderRateLimited,
    ProviderResponseInvalid,
    ProviderUnavailable,
    ResourceNotFound,
)
from cipherchain.core.hashing import canonical_json_bytes, sha256_canonical_json
from cipherchain.core.models import Capability
from cipherchain.providers.base import Provider, ProviderRequest, ProviderResponse
from cipherchain.providers.cache import InMemoryCache
from cipherchain.providers.pool import ProviderLimits, ProviderPool

NOW = datetime(2026, 8, 7, tzinfo=UTC)
Outcome = Exception | dict[str, object]


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeProvider(Provider):
    def __init__(
        self,
        name: str,
        outcomes: list[Outcome],
        *,
        chain: str = "ethereum",
        capabilities: frozenset[Capability] = frozenset({Capability.TX_LOOKUP, Capability.BALANCE}),
    ) -> None:
        self.name = name
        self._outcomes = outcomes
        self._chain = chain
        self._capabilities = capabilities
        self.calls = 0

    def supports(self, chain: str, capability: Capability) -> bool:
        return chain == self._chain and capability in self._capabilities

    async def execute(self, request: ProviderRequest) -> ProviderResponse:
        outcome = self._outcomes[min(self.calls, len(self._outcomes) - 1)]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return ProviderResponse(
            provider=self.name,
            retrieved_at=NOW,
            payload=outcome,
            raw=canonical_json_bytes(outcome),
            payload_sha256=sha256_canonical_json(outcome),
        )


def make_pool(clock: FakeClock, **kwargs: object) -> ProviderPool:
    return ProviderPool(
        cache=InMemoryCache(clock=clock),
        clock=clock,
        sleep=clock.sleep,
        rng=lambda: 1.0,  # deterministic full-jitter
        **kwargs,  # type: ignore[arg-type]
    )


def tx_request() -> ProviderRequest:
    return ProviderRequest("ethereum", Capability.TX_LOOKUP, {"tx_hash": "0xabc"})


async def test_success_then_cache_hit() -> None:
    clock = FakeClock()
    pool = make_pool(clock)
    provider = FakeProvider("a", [{"hash": "0xabc"}])
    pool.register(provider)

    first = await pool.fetch(tx_request())
    second = await pool.fetch(tx_request())

    assert first.payload == {"hash": "0xabc"} and not first.from_cache
    assert second.from_cache and second.payload == first.payload
    assert second.provider == "a" and second.payload_sha256 == first.payload_sha256
    assert provider.calls == 1  # immutable data fetched exactly once
    snapshot = pool.metrics.snapshot()
    assert snapshot["cache_hits"] == {"ethereum/tx_lookup": 1}


async def test_balance_is_never_cached() -> None:
    clock = FakeClock()
    pool = make_pool(clock)
    provider = FakeProvider("a", [{"balance": "0x1"}])
    pool.register(provider)
    request = ProviderRequest("ethereum", Capability.BALANCE, {"address": "0xa"})

    await pool.fetch(request)
    await pool.fetch(request)
    assert provider.calls == 2


async def test_failover_after_retries_exhausted() -> None:
    clock = FakeClock()
    pool = make_pool(clock)
    failing = FakeProvider("a", [ProviderUnavailable("a", "boom")])
    healthy = FakeProvider("b", [{"ok": True}])
    pool.register(failing, priority=1)
    pool.register(healthy, priority=2)

    response = await pool.fetch(tx_request())

    assert response.provider == "b"
    assert failing.calls == 3  # max_attempts_per_provider
    assert len(clock.sleeps) == 2  # backoff between the three attempts
    assert clock.sleeps[1] > clock.sleeps[0]  # exponential
    assert pool.metrics.snapshot()["fallbacks"] == {"ethereum/tx_lookup": 1}


async def test_invalid_response_fails_over_without_retry() -> None:
    clock = FakeClock()
    pool = make_pool(clock)
    invalid = FakeProvider("a", [ProviderResponseInvalid("a", "garbage")])
    healthy = FakeProvider("b", [{"ok": True}])
    pool.register(invalid, priority=1)
    pool.register(healthy, priority=2)

    response = await pool.fetch(tx_request())
    assert response.provider == "b"
    assert invalid.calls == 1  # malformed answers are never retried


async def test_rate_limit_retries_same_provider_honoring_retry_after() -> None:
    clock = FakeClock()
    pool = make_pool(clock)
    provider = FakeProvider("a", [ProviderRateLimited("a", "429", retry_after=7.5), {"ok": True}])
    pool.register(provider, limits=ProviderLimits(rate_per_sec=100, burst=5))

    response = await pool.fetch(tx_request())

    assert response.provider == "a"
    assert provider.calls == 2
    assert 7.5 in clock.sleeps  # Retry-After honored
    series = pool.metrics.snapshot()["providers"]["a/tx_lookup"]
    assert series["rate_limited"] == 1 and series["success"] == 1


async def test_a_long_retry_after_fails_over_instead_of_sleeping_it_out() -> None:
    """A vendor saying "come back in an hour" must not freeze the trace.

    Retry-After was honoured verbatim, so a 3600s header stalled the pool for an
    hour — and with retries, twice. That is *stopped*, not slowed, and the whole
    point of the fallback tiers is that a spent provider costs speed rather than
    the run. Honouring a long wait only makes sense with nowhere else to go, and
    having somewhere else to go is what a pool is.
    """
    clock = FakeClock()
    pool = make_pool(clock)
    throttled = FakeProvider("a", [ProviderRateLimited("a", "429", retry_after=3600.0)])
    healthy = FakeProvider("b", [{"ok": True}])
    pool.register(throttled, limits=ProviderLimits(rate_per_sec=100, burst=5), priority=0)
    pool.register(healthy, limits=ProviderLimits(rate_per_sec=100, burst=5), priority=1)

    response = await pool.fetch(tx_request())

    assert response.provider == "b", "should have failed over to the healthy provider"
    assert throttled.calls == 1, "must not retry a provider that asked for an hour"
    assert not any(s > 10.0 for s in clock.sleeps), f"slept too long: {clock.sleeps}"


async def test_a_short_retry_after_is_still_honoured_exactly() -> None:
    """The cap must not turn every throttle into a failover — a vendor asking
    for a few seconds is worth waiting for, since the alternative tiers are
    slower than the wait."""
    clock = FakeClock()
    pool = make_pool(clock)
    provider = FakeProvider("a", [ProviderRateLimited("a", "429", retry_after=4.0), {"ok": True}])
    pool.register(provider, limits=ProviderLimits(rate_per_sec=100, burst=5))

    response = await pool.fetch(tx_request())

    assert response.provider == "a"
    assert 4.0 in clock.sleeps


async def test_breaker_opens_and_skips_provider() -> None:
    clock = FakeClock()
    pool = make_pool(clock, max_attempts_per_provider=1)
    provider = FakeProvider("a", [ProviderUnavailable("a", "down")])
    pool.register(provider, failure_threshold=2, reset_timeout=60.0)

    with pytest.raises(AllProvidersFailed) as first:
        await pool.fetch(tx_request())
    assert first.value.attempts == 1
    with pytest.raises(AllProvidersFailed):
        await pool.fetch(tx_request())
    assert provider.calls == 2  # breaker now open

    with pytest.raises(AllProvidersFailed) as third:
        await pool.fetch(tx_request())
    assert provider.calls == 2  # not called: circuit open
    assert third.value.attempts == 0


async def test_not_found_propagates_without_failover() -> None:
    clock = FakeClock()
    pool = make_pool(clock)
    answering = FakeProvider("a", [ResourceNotFound("a", "no such tx")])
    untouched = FakeProvider("b", [{"ok": True}])
    pool.register(answering, priority=1)
    pool.register(untouched, priority=2)

    with pytest.raises(ResourceNotFound):
        await pool.fetch(tx_request())
    assert untouched.calls == 0  # an answer, not a failure


async def test_no_capable_provider() -> None:
    clock = FakeClock()
    pool = make_pool(clock)
    pool.register(FakeProvider("a", [{"ok": True}]))  # ethereum only

    with pytest.raises(AllProvidersFailed) as exc:
        await pool.fetch(ProviderRequest("bitcoin", Capability.TX_LOOKUP, {"tx_hash": "f00d"}))
    assert exc.value.attempts == 0

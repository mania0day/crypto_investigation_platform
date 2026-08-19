"""Async token bucket, pinned at or below each provider's free tier."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable


class TokenBucket:
    """Classic token bucket: ``rate`` tokens/second, up to ``capacity`` burst.

    ``acquire`` waits until a token is available and returns the seconds it
    waited. ``penalize`` drains the bucket — called when a vendor answers
    429, meaning our configured rate was still too fast for them right now.

    Clock and sleep are injectable so tests are deterministic and instant.
    """

    def __init__(
        self,
        rate: float,
        capacity: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if rate <= 0 or capacity < 1:
            raise ValueError("rate must be > 0 and capacity >= 1")
        self._rate = float(rate)
        self._capacity = float(capacity)
        self._tokens = float(capacity)
        self._clock = clock
        self._sleep = sleep
        self._updated = clock()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self._capacity, self._tokens + (now - self._updated) * self._rate)
        self._updated = now

    async def acquire(self) -> float:
        waited = 0.0
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return waited
                needed = (1.0 - self._tokens) / self._rate
            await self._sleep(needed)
            waited += needed

    def penalize(self) -> None:
        """Drain to zero: the vendor said we were too fast (HTTP 429)."""
        self._tokens = 0.0
        self._updated = self._clock()

    @property
    def available(self) -> float:
        self._refill()
        return self._tokens

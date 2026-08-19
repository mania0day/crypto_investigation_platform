"""Circuit breaker: a failing provider leaves rotation; probes re-admit it."""

from __future__ import annotations

import enum
import time
from collections.abc import Callable


class CircuitState(enum.Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Per-provider breaker.

    CLOSED: calls flow; ``failure_threshold`` consecutive failures open it.
    OPEN: calls are refused until ``reset_timeout`` elapses.
    HALF_OPEN: exactly one probe call is admitted; success closes the
    circuit, failure reopens it (and restarts the timer).

    Methods are synchronous and unlocked: the pool runs on one event loop
    and never awaits between ``allow`` and the recording call.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        reset_timeout: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1 or reset_timeout <= 0:
            raise ValueError("failure_threshold >= 1 and reset_timeout > 0 required")
        self._threshold = failure_threshold
        self._reset_timeout = reset_timeout
        self._clock = clock
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._probe_inflight = False

    @property
    def state(self) -> CircuitState:
        return self._state

    def allow(self) -> bool:
        """May a call proceed right now? (HALF_OPEN admits a single probe.)"""
        if self._state is CircuitState.CLOSED:
            return True
        if self._state is CircuitState.OPEN:
            if self._clock() - self._opened_at >= self._reset_timeout:
                self._state = CircuitState.HALF_OPEN
                self._opened_at = self._clock()
                self._probe_inflight = True
                return True
            return False
        # HALF_OPEN. A probe that never records its result (a 429 that only
        # penalizes the bucket, an untranslated error, a cancellation) must not
        # wedge the breaker open forever: once reset_timeout elapses, release
        # the stale slot and admit a fresh probe (REVIEW_FINDINGS.md #9).
        if self._probe_inflight:
            if self._clock() - self._opened_at >= self._reset_timeout:
                self._opened_at = self._clock()
                return True
            return False
        self._probe_inflight = True
        return True

    def record_success(self) -> None:
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._probe_inflight = False

    def record_failure(self) -> None:
        if self._state is CircuitState.HALF_OPEN:
            self._trip()
            return
        self._failures += 1
        if self._failures >= self._threshold:
            self._trip()

    def _trip(self) -> None:
        self._state = CircuitState.OPEN
        self._opened_at = self._clock()
        self._failures = 0
        self._probe_inflight = False

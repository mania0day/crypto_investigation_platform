from cipherchain.providers.breaker import CircuitBreaker, CircuitState


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_threshold_opens_circuit() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=2, reset_timeout=30.0, clock=clock)
    assert breaker.allow()
    breaker.record_failure()
    assert breaker.state is CircuitState.CLOSED
    breaker.record_failure()
    assert breaker.state is CircuitState.OPEN
    assert not breaker.allow()


def test_half_open_single_probe_then_close() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout=30.0, clock=clock)
    breaker.record_failure()
    assert not breaker.allow()

    clock.now = 31.0
    assert breaker.allow()  # single probe admitted
    assert breaker.state is CircuitState.HALF_OPEN
    assert not breaker.allow()  # concurrent probe refused

    breaker.record_success()
    assert breaker.state is CircuitState.CLOSED
    assert breaker.allow()


def test_half_open_failure_reopens_with_fresh_timer() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout=30.0, clock=clock)
    breaker.record_failure()
    clock.now = 31.0
    assert breaker.allow()
    breaker.record_failure()  # probe failed
    assert breaker.state is CircuitState.OPEN
    clock.now = 60.0
    assert not breaker.allow()  # timer restarted at 31.0
    clock.now = 61.5
    assert breaker.allow()

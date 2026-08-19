from cipherchain.providers.ratelimit import TokenBucket


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


async def test_burst_then_wait() -> None:
    clock = FakeClock()
    bucket = TokenBucket(rate=1.0, capacity=2, clock=clock, sleep=clock.sleep)
    assert await bucket.acquire() == 0.0
    assert await bucket.acquire() == 0.0  # burst capacity
    waited = await bucket.acquire()
    assert waited == 1.0  # refill at 1 token/sec
    assert clock.sleeps == [1.0]


async def test_penalize_drains_bucket() -> None:
    clock = FakeClock()
    bucket = TokenBucket(rate=2.0, capacity=2, clock=clock, sleep=clock.sleep)
    bucket.penalize()
    waited = await bucket.acquire()
    assert waited == 0.5  # empty bucket at 2/sec: half a second per token

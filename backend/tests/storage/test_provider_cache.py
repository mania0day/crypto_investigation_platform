"""Durable provider cache honors the CacheBackend protocol semantics."""

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncEngine

from cipherchain.providers.cache import CachedEntry
from cipherchain.storage.db import create_session_factory
from cipherchain.storage.provider_cache import PostgresProviderCache


def entry(retrieved_at: datetime, provider: str = "etherscan") -> CachedEntry:
    return CachedEntry(
        chain="ethereum",
        capability="tx_lookup",
        provider=provider,
        retrieved_at=retrieved_at,
        payload_json=b'{"result":"ok"}',
        raw=b'{"jsonrpc":"2.0","result":"ok"}',
        payload_sha256="e" * 64,
    )


async def test_put_get_round_trip(engine: AsyncEngine) -> None:
    cache = PostgresProviderCache(create_session_factory(engine))
    stored = entry(datetime.now(UTC))
    await cache.put("k" * 64, stored)
    loaded = await cache.get("k" * 64)
    assert loaded == stored


async def test_miss_returns_none(engine: AsyncEngine) -> None:
    cache = PostgresProviderCache(create_session_factory(engine))
    assert await cache.get("m" * 64) is None


async def test_max_age_expiry(engine: AsyncEngine) -> None:
    cache = PostgresProviderCache(create_session_factory(engine))
    stale = entry(datetime.now(UTC) - timedelta(hours=2))
    await cache.put("s" * 64, stale)
    assert await cache.get("s" * 64) is not None  # FOREVER policy: no age limit
    assert await cache.get("s" * 64, max_age_seconds=300) is None  # TTL policy: stale


async def test_put_overwrites_with_newer(engine: AsyncEngine) -> None:
    cache = PostgresProviderCache(create_session_factory(engine))
    await cache.put("o" * 64, entry(datetime.now(UTC) - timedelta(hours=1), provider="drpc"))
    newer = entry(datetime.now(UTC), provider="alchemy")
    await cache.put("o" * 64, newer)
    loaded = await cache.get("o" * 64)
    assert loaded is not None and loaded.provider == "alchemy"

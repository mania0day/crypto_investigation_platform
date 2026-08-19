"""Postgres-backed provider cache — the durable CacheBackend.

Implements the same protocol as InMemoryCache, so the pool cannot tell the
difference. Chain data being immutable, entries never expire on their own;
TTL capabilities pass ``max_age_seconds`` and are re-fetched when stale.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cipherchain.providers.cache import CachedEntry
from cipherchain.storage.tables import ProviderCacheRow


class PostgresProviderCache:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def get(self, key: str, *, max_age_seconds: float | None = None) -> CachedEntry | None:
        async with self._sessions() as session:
            row = await session.get(ProviderCacheRow, key)
            if row is None:
                return None
            if max_age_seconds is not None:
                age = (datetime.now(UTC) - row.retrieved_at).total_seconds()
                if age > max_age_seconds:
                    return None
            return CachedEntry(
                chain=row.chain,
                capability=row.capability,
                provider=row.provider,
                retrieved_at=row.retrieved_at,
                payload_json=bytes(row.payload_json),
                raw=bytes(row.raw),
                payload_sha256=row.payload_sha256,
            )

    async def put(self, key: str, entry: CachedEntry) -> None:
        values = {
            "cache_key": key,
            "chain": entry.chain,
            "capability": entry.capability,
            "provider": entry.provider,
            "retrieved_at": entry.retrieved_at,
            "payload_sha256": entry.payload_sha256,
            "raw": entry.raw,
            "payload_json": entry.payload_json,
        }
        stmt = pg_insert(ProviderCacheRow).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[ProviderCacheRow.cache_key],
            set_={k: v for k, v in values.items() if k != "cache_key"},
        )
        async with self._sessions() as session:
            await session.execute(stmt)
            await session.commit()

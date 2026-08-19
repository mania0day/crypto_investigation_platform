"""Response cache: protocol + in-memory backend.

Entries store both the raw vendor bytes (evidence: the digest cited by
findings is of these bytes) and the canonical JSON of the parsed payload
(so a hit reconstructs exactly what the client returned). The
Postgres-backed implementation (``provider_cache`` table) implements the
same protocol in the storage package.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class CachedEntry:
    chain: str
    capability: str
    provider: str
    retrieved_at: datetime
    payload_json: bytes
    raw: bytes
    payload_sha256: str


class CacheBackend(Protocol):
    async def get(self, key: str, *, max_age_seconds: float | None = None) -> CachedEntry | None:
        """Return the entry unless absent or older than ``max_age_seconds``."""
        ...

    async def put(self, key: str, entry: CachedEntry) -> None: ...


class InMemoryCache:
    """Process-local cache backend. Suitable for tests and single runs."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._data: dict[str, tuple[float, CachedEntry]] = {}

    async def get(self, key: str, *, max_age_seconds: float | None = None) -> CachedEntry | None:
        item = self._data.get(key)
        if item is None:
            return None
        stored_at, entry = item
        if max_age_seconds is not None and self._clock() - stored_at > max_age_seconds:
            return None
        return entry

    async def put(self, key: str, entry: CachedEntry) -> None:
        self._data[key] = (self._clock(), entry)

    def __len__(self) -> int:
        return len(self._data)

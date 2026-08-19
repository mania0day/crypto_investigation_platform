#!/usr/bin/env python
"""End-to-end run of the acquisition data plane, against the real world.

    settings → pool (durable Postgres cache) → vendor clients → adapters
             → canonical movements → fact store → traversal queries

Subjects are the two small real addresses from the recorded-fixture
manifest, so the demo stays polite to free tiers. Run it twice: the second
run should show cache hits and (within the history TTL) zero vendor calls —
the "never pay for the same immutable data twice" rule, live.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

from cipherchain.core.config import Settings
from cipherchain.core.logging import configure_logging
from cipherchain.core.models import Address
from cipherchain.runtime import build_chain_registry, build_provider_pool
from cipherchain.storage.db import create_engine, create_session_factory
from cipherchain.storage.provider_cache import PostgresProviderCache
from cipherchain.storage.repositories import FactRepository

MANIFEST = Path(__file__).resolve().parents[1] / "tests" / "chains" / "fixtures" / "manifest.json"


async def trace_address(registry, session_factory, chain: str, address_value: str) -> None:
    adapter = registry.get(chain)
    address = Address(chain, address_value)
    page = await adapter.address_history(address, limit=25)

    new_movements = 0
    async with session_factory() as session:
        facts = FactRepository(session)
        for item in page.items:
            normalized = await adapter.normalize(item)
            _, inserted = await facts.store_movements(
                normalized.tx,
                list(normalized.movements),
                raw_sha256=item.provenance.payload_sha256,
            )
            new_movements += inserted
        address_id = await facts.get_or_create_address(address)
        await session.commit()

        incoming = await facts.movements_to_address(address_id, limit=3)
        outgoing = await facts.movements_from_address(address_id, limit=3)

    print(f"\n=== {chain} · {address_value}")
    print(f"  fetched {len(page.items)} confirmed txs → {new_movements} NEW movements stored")
    for movement in incoming:
        print(
            f"   in  {movement.tx_hash[:18]}…  {movement.amount:>24}  {movement.kind}"
            f"  @ {movement.timestamp:%Y-%m-%d %H:%M}"
        )
    for movement in outgoing:
        print(
            f"   out {movement.tx_hash[:18]}…  {movement.amount:>24}  {movement.kind}"
            f"  @ {movement.timestamp:%Y-%m-%d %H:%M}"
        )


async def main() -> None:
    configure_logging()
    settings = Settings()
    if not settings.etherscan_api_key:
        sys.exit("ETHERSCAN_API_KEY missing — check .env")
    database_url = os.environ.get("DATABASE_URL", settings.database_url)
    manifest = json.loads(MANIFEST.read_text())

    engine = create_engine(database_url)
    session_factory = create_session_factory(engine)
    try:
        async with httpx.AsyncClient(timeout=25) as http:
            pool = build_provider_pool(settings, http, cache=PostgresProviderCache(session_factory))
            registry = build_chain_registry(pool)
            print(f"chains wired: {registry.chains()}  ·  db: {database_url.split('@')[-1]}")

            await trace_address(registry, session_factory, "bitcoin", manifest["btc_address"])
            await trace_address(registry, session_factory, "ethereum", manifest["eth_address"])

        snapshot = pool.metrics.snapshot()
        print("\n=== pool metrics")
        for key, series in sorted(snapshot["providers"].items()):
            print(
                f"  {key:<32} calls={series['success']:>2}"
                f"  p50={series['latency_p50'] * 1000:6.0f}ms"
                f"  rate_limited={series['rate_limited']}"
            )
        print(f"  cache_hits: {snapshot['cache_hits'] or '—'}")
        print(f"  fallbacks:  {snapshot['fallbacks'] or '—'}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

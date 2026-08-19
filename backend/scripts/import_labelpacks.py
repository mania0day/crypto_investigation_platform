#!/usr/bin/env python
"""Ingest the labelpacks in ``labels/`` into the lifecycle store.

The operator workflow is unchanged — drop a ``*.json`` pack in ``labels/``,
run this — but the attributor no longer reads the files: every label now
enters through ``IntelService.ingest``, arrives with the verification method
its pack declares, and lives under the lifecycle (LABEL_INTELLIGENCE.md §4).
Idempotent by construction: an unchanged row is 'unchanged' and writes no
event, so re-running after adding one pack touches only what changed.

Usage:
    DATABASE_URL=postgresql+asyncpg://… python scripts/import_labelpacks.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from cipherchain.core.config import get_settings
from cipherchain.intel.policy import TRUSTED_METHODS, IntelClaim
from cipherchain.intel.service import IntelService
from cipherchain.storage.db import create_engine, create_session_factory

LABELS_DIR = Path(__file__).resolve().parents[2] / "labels"


def pack_claims(path: Path) -> tuple[str, list[IntelClaim]]:
    """One pack file → claims, using the pack's own declared provenance."""
    data = json.loads(path.read_text())
    source = str(data["source"])
    method = str(data.get("method", ""))
    if method not in TRUSTED_METHODS:
        # A pack without a recognized trusted method would arrive PENDING —
        # 74,939 labels quietly unable to name anything. Refusing is louder.
        raise SystemExit(
            f"{path.name}: method {method!r} is not a trusted tier "
            f"({', '.join(sorted(TRUSTED_METHODS))}) — declare one in the pack."
        )
    source_date = datetime.fromisoformat(str(data["source_date"])).replace(tzinfo=UTC)
    default_confidence = float(data.get("default_confidence", 0.8))
    retrieved_at = datetime.now(UTC)
    claims = [
        IntelClaim(
            chain=str(row["chain"]),
            address=str(row["address"]),
            entity=str(row["entity"]),
            category=str(row["category"]),
            role=str(row.get("role", "unknown")),
            confidence=float(row.get("confidence", default_confidence)),
            method=method,
            source=source,
            retrieved_at=retrieved_at,
            source_date=source_date,
        )
        for row in data["labels"]
    ]
    return source, claims


def refuse_claim_collisions(packs: list[tuple[Path, str, list[IntelClaim]]]) -> None:
    """Two rows claiming the same (chain, address) under the same source —
    across packs OR within one file — would collide on the store's claim
    identity: the second silently overwrites the first, the survivor decided
    by nothing but ORDER, and every re-import flips the row while writing
    'updated' events forever. That happened live (11 exchange/infrastructure
    double-tags, resolved by pack filename order). Order is not a decision:
    refuse, name the rows, make the operator resolve the data.
    """
    seen: dict[tuple[str, str, str], str] = {}
    collisions: list[str] = []
    for path, source, claims in packs:
        for claim in claims:
            key = (claim.chain, claim.address.lower(), source)
            if key in seen:
                collisions.append(f"  {claim.chain} {claim.address}: {seen[key]} vs {path.name}")
            else:
                seen[key] = path.name
    if collisions:
        raise SystemExit(
            "claim collisions — same (chain, address, source) more than once; "
            "the store would keep whichever row imported last:\n" + "\n".join(sorted(collisions))
        )


async def main() -> None:
    url = os.environ.get("DATABASE_URL") or get_settings().database_url
    engine = create_engine(url)
    factory = create_session_factory(engine)
    outcomes: Counter[str] = Counter()
    by_chain: Counter[str] = Counter()
    packs = [(path, *pack_claims(path)) for path in sorted(LABELS_DIR.glob("*.json"))]
    refuse_claim_collisions(packs)
    try:
        for path, source, claims in packs:
            print(f"{path.name}: {len(claims)} labels from {source!r}", flush=True)
            async with factory() as session:
                service = IntelService(session)
                for claim in claims:
                    outcomes[await service.ingest(claim)] += 1
                    by_chain[claim.chain] += 1
                await session.commit()
        print(
            f"\ndone: {outcomes['added']} added, {outcomes['updated']} updated, "
            f"{outcomes['unchanged']} unchanged"
        )
        for chain, count in sorted(by_chain.items()):
            print(f"  {chain}: {count}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

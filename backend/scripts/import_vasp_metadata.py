#!/usr/bin/env python
"""Ingest the curated VASP metadata file into the ``vasp_metadata`` table.

Same operator workflow as ``import_labelpacks.py``: edit the JSON, run this.
The rows go in through ``VaspMetadataRepository`` rather than SQL written
here, so the table has exactly one definition and one upsert in the codebase.

Idempotent, and deliberately more than "ends in the same state": a row whose
seven stored fields already match is not written AT ALL. That matters beyond
tidiness — this is the "who do we serve" record, ``created_at`` is the only
trace of when a fact was first established, and a re-import that rewrites
every row each night destroys it.

The table stores the entity STEM (its documented join key), while the file
keeps the display name: a label reaching the report says "Binance (deposit
address)", which stems to "binance", and that is what a SQL consumer joins
on.

Usage:
    DATABASE_URL=postgresql+asyncpg://… python scripts/import_vasp_metadata.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections import Counter
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from cipherchain.core.config import get_settings
from cipherchain.intel.vasp_metadata import (
    DEFAULT_METADATA_PATH,
    VaspMetadata,
    VaspMetadataIndex,
    load_vasp_metadata,
)
from cipherchain.storage.db import create_engine, create_session_factory
from cipherchain.storage.repositories import StoredVaspMetadata, VaspMetadataRepository


def already_matches(stored: StoredVaspMetadata, row: VaspMetadata) -> bool:
    """Is every stored field already what the file says?

    Compares the whole record, nulls included. A field the file has cleared
    is a difference: the repository's upsert REPLACES, and treating a
    now-null jurisdiction as "no change" would leave a superseded one in a
    table people file against.

    ``source_date`` compares as a plain date on both sides: the file dates a
    row to the day and ``vasp_metadata.source_date`` is a DATE column (model
    and migration agree). Widening either side to an instant would invent a
    time nobody read off the source and make every re-import an "update".
    """
    return (
        stored.jurisdiction == row.jurisdiction
        and stored.legal_entity == row.legal_entity
        and stored.kyc_regime == row.kyc_regime
        and stored.kyc_since == row.kyc_since
        and stored.le_request_channel == row.le_request_channel
        and stored.source == row.source
        and stored.source_date == row.source_date
    )


async def upsert_metadata(session: AsyncSession, row: VaspMetadata) -> str:
    """One operator in. Returns ``added`` | ``updated`` | ``unchanged``."""
    repository = VaspMetadataRepository(session)
    stored = await repository.for_entity(row.stem)
    if stored is not None and already_matches(stored, row):
        return "unchanged"
    await repository.upsert(
        entity=row.stem,
        jurisdiction=row.jurisdiction,
        legal_entity=row.legal_entity,
        kyc_regime=row.kyc_regime,
        kyc_since=row.kyc_since,
        le_request_channel=row.le_request_channel,
        source=row.source,
        source_date=row.source_date,
    )
    return "updated" if stored is not None else "added"


async def import_index(session: AsyncSession, index: VaspMetadataIndex) -> Counter[str]:
    outcomes: Counter[str] = Counter()
    for row in index:
        outcomes[await upsert_metadata(session, row)] += 1
    return outcomes


async def main(path: Path = DEFAULT_METADATA_PATH) -> None:
    # Parsed before the database is touched: a malformed file must fail
    # before it can half-import.
    index = load_vasp_metadata(path)
    url = os.environ.get("DATABASE_URL") or get_settings().database_url
    engine = create_engine(url)
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            outcomes = await import_index(session, index)
            await session.commit()
    finally:
        await engine.dispose()

    print(f"{path.name}: {len(index)} operators")
    print(
        f"done: {outcomes['added']} added, {outcomes['updated']} updated, "
        f"{outcomes['unchanged']} unchanged"
    )
    # The point of the file is naming a respondent, so report how many rows
    # can actually do that. A silent import of fifteen unusable rows would
    # print identically to a useful one.
    unserviceable = [row.entity for row in index if not row.is_serviceable]
    print(f"  name a legal entity and a jurisdiction: {len(index) - len(unserviceable)}")
    if unserviceable:
        print(f"  forum or respondent still unestablished: {', '.join(unserviceable)}")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

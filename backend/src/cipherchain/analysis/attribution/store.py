"""The Attributor implementation: an in-memory index over label sources.

Composes any number of sources (vendored OFAC, user labelpacks, later a
database) into one lookup. Multiple sources may label the same address;
all matching claims are returned, highest confidence first, so the engine
and the report show *every* claim rather than silently picking one.
"""

from __future__ import annotations

from collections.abc import Iterable

from cipherchain.analysis.attribution.labels import LabelRecord, LabelSource, normalize_address
from cipherchain.core.models import Address
from cipherchain.investigation.attribution import AttributionResult


class LabelStoreAttributor:
    """Implements the ``Attributor`` port over one or more label sources."""

    def __init__(self, sources: Iterable[LabelSource] = ()) -> None:
        self._index: dict[tuple[str, str], list[LabelRecord]] = {}
        self.source_names: list[str] = []
        for source in sources:
            self.add_source(source)

    def add_source(self, source: LabelSource) -> None:
        self.source_names.append(source.name)
        for record in source.records():
            self._index.setdefault(record.key, []).append(record)

    def __len__(self) -> int:
        return len(self._index)

    async def attribute(self, address: Address) -> tuple[AttributionResult, ...]:
        records = self._index.get((address.chain, normalize_address(address.value)))
        if not records:
            return ()
        ordered = sorted(records, key=lambda r: (-r.confidence, r.source, r.entity))
        return tuple(
            AttributionResult(
                entity=record.entity,
                category=record.category,
                source=record.source,
                confidence=record.confidence,
                source_date=record.source_date,
                role=record.role,
            )
            for record in ordered
        )

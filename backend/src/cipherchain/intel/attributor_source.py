"""The bridge from the lifecycle store to the attributor.

The attributor keeps its existing read interface — an in-memory index built
at startup — but its label rows now come from the lifecycle store instead of
startup-loaded files. Only ``active`` rows cross this bridge, which is the
entire enforcement point of the no-false-positives contract: pending and
retired rows are not filtered out by the attributor, they simply never reach
it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cipherchain.analysis.attribution.labels import LabelRecord
from cipherchain.analysis.attribution.store import LabelStoreAttributor
from cipherchain.analysis.sanctions.ofac import OfacSanctionsSource
from cipherchain.core.models import Address
from cipherchain.investigation.attribution import AddressRole, AttributionResult
from cipherchain.storage.repositories import LabelRepository, StoredLabel

logger = logging.getLogger(__name__)


class StoredLabelSource:
    """Adapts active lifecycle rows to the ``LabelSource`` protocol.

    ``source`` on each record stays the ORIGINAL source (the pack, the PoR
    file, the corroborated report) — that is the citation an investigator
    needs. The store is plumbing, not provenance.
    """

    name = "label-store"

    def __init__(self, rows: Iterable[StoredLabel]) -> None:
        self._rows = list(rows)

    def records(self) -> Iterable[LabelRecord]:
        for row in self._rows:
            yield LabelRecord(
                chain=row.chain,
                address=row.address,
                entity=row.entity,
                category=row.category,
                source=row.source,
                confidence=row.confidence,
                source_date=row.source_date,
                role=AddressRole(row.role),
            )


class RefreshingAttributor:
    """The attributor, plus noticing when the harvester has been.

    The index is built once and held in memory, which is why naming 400
    addresses costs zero network calls. The cost of that speed is staleness: a
    daily harvest lands in the ``labels`` table and a long-running server never
    sees it. A week-uptime deployment answers with week-old attribution and
    reports it with today's date — the failure is invisible, because a label
    that is merely OUT OF DATE looks exactly like a label that is absent, and
    "no named endpoint" is a conclusion an investigator acts on.

    Re-reading 75,000 rows per lookup is not the answer either. ``label_events``
    is append-only with a monotonic id, so one indexed ``max()`` says whether
    anything changed at all, and the expensive rebuild happens only when it did
    — once per harvest rather than once per address.

    The check is itself rate-limited: a run touches thousands of addresses, and
    a query per address would replace a stale-data problem with a load problem.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        inner: LabelStoreAttributor,
        watermark: int,
        *,
        check_interval: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sessions = session_factory
        self._inner = inner
        self._watermark = watermark
        self._check_interval = check_interval
        self._clock = clock
        self._checked_at = clock()
        self._lock = asyncio.Lock()

    @property
    def source_names(self) -> list[str]:
        return self._inner.source_names

    def __len__(self) -> int:
        return len(self._inner)

    async def attribute(self, address: Address) -> tuple[AttributionResult, ...]:
        await self._maybe_refresh()
        return await self._inner.attribute(address)

    async def _maybe_refresh(self) -> None:
        if self._clock() - self._checked_at < self._check_interval:
            return
        async with self._lock:
            # Re-checked under the lock: a thousand concurrent lookups all see a
            # stale timestamp at once, and without this they would all queue to
            # rebuild the same index.
            if self._clock() - self._checked_at < self._check_interval:
                return
            self._checked_at = self._clock()
            async with self._sessions() as session:
                latest = await LabelRepository(session).latest_event_id()
                if latest == self._watermark:
                    return
                rows = await LabelRepository(session).active_labels()
            rebuilt = LabelStoreAttributor()
            rebuilt.add_source(OfacSanctionsSource())
            rebuilt.add_source(StoredLabelSource(rows))
            previous, self._watermark = self._watermark, latest
            self._inner = rebuilt
            logger.info(
                "label store changed (event %d -> %d): reloaded %d active labels",
                previous,
                latest,
                len(rows),
            )


async def build_store_attributor(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    check_interval: float = 60.0,
) -> RefreshingAttributor:
    """The production attributor: vendored OFAC plus the lifecycle store.

    An empty store degrades LOUDLY, never silently: sanctions screening still
    works (OFAC is vendored), but naming is off until the operator runs
    ``scripts/import_labelpacks.py``. There is deliberately no quiet fallback
    to reading labelpack files directly — two sources of truth would make
    "where did this label come from" unanswerable, and that question is the
    product.

    Wrapped so the daily harvest actually reaches a running server. Without it
    the whole harvest subsystem is write-only from the API's point of view:
    labels land in the table, the process holds an index built at startup, and
    nothing connects the two until someone restarts it. Pass
    ``check_interval=0`` to check on every lookup (tests), or a large value to
    pin a run to one snapshot.
    """
    async with session_factory() as session:
        repository = LabelRepository(session)
        rows = await repository.active_labels()
        watermark = await repository.latest_event_id()
    if not rows:
        logger.warning(
            "label store is EMPTY — endpoint naming is disabled (sanctions "
            "screening still active). Run scripts/import_labelpacks.py to "
            "ingest the labelpacks in labels/."
        )
    attributor = LabelStoreAttributor()
    attributor.add_source(OfacSanctionsSource())
    attributor.add_source(StoredLabelSource(rows))
    logger.info(
        "attributor ready: %d active labels from the lifecycle store (watermark %d)",
        len(rows),
        watermark,
    )
    return RefreshingAttributor(
        session_factory, attributor, watermark, check_interval=check_interval
    )

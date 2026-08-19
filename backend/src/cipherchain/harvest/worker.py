"""The cycle: refresh every source, then re-settle the whole store.

``IntelService.reconcile()`` has been built and tested since the lifecycle
landed and has never had a production caller (REACHING_THE_VASP.md §6.3). This
worker is that caller. It adds no policy of its own — every rule about what may
name an operator already lives in ``intel/policy.py``, and the one job here is
to not go around it.

Three properties this file exists to hold
-----------------------------------------
- **Nothing writes to the label store directly.** Claims go through
  ``IntelService.ingest``, so ``pending -> active -> retired`` stays the only
  path and every transition still writes its event in the same transaction as
  the row change. A harvester that reached for ``LabelRepository`` would be
  able to activate a claim with no event behind it — the exact hole the
  lifecycle was built to close.
- **One source's failure ends that source, not the cycle.** Each source
  commits in its own session, so a rejected file cannot roll back the sources
  that already succeeded, and a publisher being down for a day does not stop
  the other two from refreshing.
- **A quiet re-harvest stays quiet.** An unchanged claim is ``unchanged`` and
  writes no event, by design (``IntelService.ingest``): an audit trail that
  logs every re-read of an unchanged source drowns the transitions it exists to
  show. The report below counts those re-confirmations so the operator can see
  the cycle ran, without any of it reaching ``label_events``.

Reconcile runs last, and once. It is a whole-store pass — demotions of claims
whose corroboration stopped holding, then promotions of claims a trusted source
now agrees with — so running it per source would do the same work N times and
still only be correct on the last pass.

Why the report carries a DATE per source
----------------------------------------
"A quiet re-harvest stays quiet" is a good property and it was also, on its
own, this subsystem's blind spot. A source whose publisher froze in June keeps
succeeding: the drop file is still on disk, every claim re-ingests as
``unchanged``, the outcome is clean, and the cycle exits 0 — forever. Nothing
in the old report could tell that apart from a healthy day. So each outcome
now records the newest ``source_date`` among the claims it produced, which is
the publisher's own statement of when the document was last true, and
:meth:`HarvestReport.stale_sources` is what the scheduler shouts about.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cipherchain.harvest.sources import (
    DEFAULT_STALE_AFTER_DAYS,
    HarvestError,
    HarvestSource,
    SourceNotSupplied,
)
from cipherchain.intel.service import IntelService
from cipherchain.storage.tables import LabelRow

logger = logging.getLogger(__name__)

_DAY = 86400.0


@dataclass(frozen=True, slots=True)
class SourceOutcome:
    """What one source did this cycle. ``error`` set means it did nothing."""

    source: str
    claims: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    error: str | None = None
    # A drop-only source nobody has ever supplied. Distinct from `error`
    # because the reader's next move differs: "do the download for the first
    # time" is a setup step, "the file that was working is gone" is a
    # regression. Both still contributed nothing this cycle.
    not_supplied: bool = False
    # The newest date the source's own document declared. Not `retrieved_at`:
    # re-reading a stale file at 03:15 today says nothing about the file.
    published_at: datetime | None = None
    stale_after_days: int = DEFAULT_STALE_AFTER_DAYS

    @property
    def ok(self) -> bool:
        return self.error is None

    def age_days(self, now: datetime) -> float | None:
        """How old the publication is, or ``None`` when the source produced no
        dated claim at all — a failed source, which is already reported on its
        own terms and must not be double-counted as stale."""
        if self.published_at is None:
            return None
        return (now - self.published_at).total_seconds() / _DAY

    def is_stale(self, now: datetime, *, stale_after_days: int | None = None) -> bool:
        age = self.age_days(now)
        limit = self.stale_after_days if stale_after_days is None else stale_after_days
        return age is not None and age > limit


@dataclass(frozen=True, slots=True)
class HarvestReport:
    started_at: datetime
    finished_at: datetime
    sources: tuple[SourceOutcome, ...] = ()
    promoted: tuple[int, ...] = ()
    demoted: tuple[int, ...] = ()
    reconcile_error: str | None = None

    @property
    def failed_sources(self) -> tuple[SourceOutcome, ...]:
        """Sources that BROKE. A drop-only source nobody has ever supplied is
        excluded — it is a setup step, and counting it here makes the cycle
        exit 1 every day forever on any deployment that has not done the
        Binance and OKX downloads. See :class:`SourceNotSupplied`."""
        return tuple(
            outcome for outcome in self.sources if not outcome.ok and not outcome.not_supplied
        )

    @property
    def unsupplied_sources(self) -> tuple[SourceOutcome, ...]:
        return tuple(outcome for outcome in self.sources if outcome.not_supplied)

    def stale_sources(self, *, stale_after_days: int | None = None) -> tuple[SourceOutcome, ...]:
        """Sources that succeeded on a document nobody has republished lately.

        Measured against ``finished_at`` rather than a fresh clock, so the
        report is a closed record: printed now or read out of a log next month,
        it says the same thing. Each source's own threshold applies unless the
        operator overrides it for the whole run.
        """
        return tuple(
            outcome
            for outcome in self.sources
            if outcome.is_stale(self.finished_at, stale_after_days=stale_after_days)
        )

    def summary(self, *, stale_after_days: int | None = None) -> str:
        parts = [
            f"{outcome.source}: "
            + (
                outcome.error
                if outcome.error is not None
                else f"{outcome.added} added, {outcome.updated} updated, "
                f"{outcome.unchanged} unchanged (of {outcome.claims})"
            )
            for outcome in self.sources
        ]
        for outcome in self.unsupplied_sources:
            parts.append(
                f"AWAITING DROP: {outcome.source} has never been supplied. Not a failure — "
                "download the publisher's file onto a machine that can reach them and put it "
                "in the drop directory (drops/README.md)."
            )
        parts.append(
            f"reconcile: {len(self.promoted)} promoted, {len(self.demoted)} demoted"
            if self.reconcile_error is None
            else f"reconcile FAILED: {self.reconcile_error}"
        )
        # Last, and shouted. A stale source is the one failure here that looks
        # exactly like success on every other line of this report.
        for outcome in self.stale_sources(stale_after_days=stale_after_days):
            age = outcome.age_days(self.finished_at) or 0.0
            limit = outcome.stale_after_days if stale_after_days is None else stale_after_days
            parts.append(
                f"STALE: {outcome.source} last published "
                f"{outcome.published_at.isoformat() if outcome.published_at else '?'} — "
                f"{age:.0f} days ago, and it may go {limit}. Its rows are still in the store "
                "and are ageing; coverage decays quietly, so go and check the publisher."
            )
        return "\n".join(parts)


class HarvestWorker:
    """Runs one harvest cycle over a fixed set of sources."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        sources: Sequence[HarvestSource],
    ) -> None:
        self._session_factory = session_factory
        self._sources = list(sources)

    async def run(self) -> HarvestReport:
        started = datetime.now(UTC)
        # One read, before anything else touches the store, so that grading a
        # missing drop cannot interleave with a source's own ingest.
        contributed = await self._sources_that_have_contributed()
        outcomes = [await self._harvest(source, contributed) for source in self._sources]
        promoted: tuple[int, ...] = ()
        demoted: tuple[int, ...] = ()
        reconcile_error: str | None = None
        try:
            async with self._session_factory() as session:
                result = await IntelService(session).reconcile()
                await session.commit()
            promoted, demoted = tuple(result.promoted), tuple(result.demoted)
        except Exception as exc:  # the report must survive any failure below it
            # A reconcile that blew up must not discard the harvest that
            # preceded it: those rows are committed, and the next cycle will
            # re-settle them. Losing the report would lose the only record that
            # the cycle ran at all.
            reconcile_error = repr(exc)
            logger.exception("harvest: reconcile failed")
        return HarvestReport(
            started_at=started,
            finished_at=datetime.now(UTC),
            sources=tuple(outcomes),
            promoted=promoted,
            demoted=demoted,
            reconcile_error=reconcile_error,
        )

    async def _sources_that_have_contributed(self) -> frozenset[str]:
        """Which sources have ever put a label in this store.

        Read ONCE per cycle, before any source runs, rather than per source at
        the moment one turns out to be unsupplied. Both work; reading it up
        front is what makes the cycle's database usage independent of how many
        drops happen to be missing today, so a test — or a connection pool —
        cannot see its session accounting shift because somebody deleted a file.

        The drop directory cannot answer this question: an empty directory looks
        identical whether nobody ever supplied a file or somebody deleted one
        that was working. Retired labels count, because a source whose rows have
        all retired still worked once, and grading that as "awaiting its first
        drop" would hide a regression behind a setup message.
        """
        try:
            async with self._session_factory() as session:
                rows = await session.scalars(select(LabelRow.source).distinct())
                return frozenset(rows)
        except Exception:  # pragma: no cover - grading must never end a cycle
            # If the store cannot be read, grade DOWN to the harsher answer:
            # every unsupplied source reports as a plain failure. Saying
            # "awaiting first drop" on a database error would file a broken
            # source under "nothing to worry about".
            logger.exception("harvest: could not read which sources have contributed")
            return frozenset()

    async def _harvest(self, source: HarvestSource, contributed: frozenset[str]) -> SourceOutcome:
        name = source.spec.name
        limit = source.spec.stale_after_days
        retrieved_at = datetime.now(UTC)
        try:
            document = await source.load()
            claims = source.parse(document, retrieved_at=retrieved_at)
        except SourceNotSupplied as exc:
            # Never supplied is setup; supplied-then-gone is a regression. The
            # store is the only thing that knows which, because the drop
            # directory looks identical in both cases.
            ever = name in contributed
            logger.info(
                "harvest: %s contributed nothing (%s)%s",
                name,
                exc,
                "" if ever else " — awaiting its first drop",
            )
            return SourceOutcome(
                source=name,
                error=str(exc),
                not_supplied=not ever,
                stale_after_days=limit,
            )
        except HarvestError as exc:
            logger.info("harvest: %s contributed nothing (%s)", name, exc)
            return SourceOutcome(source=name, error=str(exc), stale_after_days=limit)
        except Exception as exc:  # containment is the point
            # A parser meets whatever an operator dropped in the directory, and
            # a document malformed in a way no parser anticipated raises
            # whatever the standard library raises. Letting that through would
            # take down the two healthy sources and the reconcile pass with it —
            # which is the property this class exists to hold, so it holds for
            # every exception rather than only the polite ones.
            logger.exception("harvest: %s failed while reading its document", name)
            return SourceOutcome(source=name, error=repr(exc), stale_after_days=limit)
        # The publisher's own date, taken from the claims rather than from the
        # document, because that is the value that actually reached the store —
        # a parser and a loader disagreeing about the date would otherwise show
        # up as a healthy report over rows dated something else.
        published = max((claim.source_date for claim in claims if claim.source_date), default=None)
        counts: Counter[str] = Counter()
        try:
            async with self._session_factory() as session:
                service = IntelService(session)
                for claim in claims:
                    counts[await service.ingest(claim)] += 1
                await session.commit()
        except Exception as exc:  # one source must not be able to end the cycle
            logger.exception("harvest: %s failed while ingesting", name)
            return SourceOutcome(
                source=name, claims=len(claims), error=repr(exc), stale_after_days=limit
            )
        outcome = SourceOutcome(
            source=name,
            claims=len(claims),
            added=counts["added"],
            updated=counts["updated"],
            unchanged=counts["unchanged"],
            published_at=published,
            stale_after_days=limit,
        )
        logger.info(
            "harvest: %s — %d added, %d updated, %d unchanged (of %d claims), published %s",
            name,
            outcome.added,
            outcome.updated,
            outcome.unchanged,
            outcome.claims,
            published.isoformat() if published else "undated",
        )
        return outcome

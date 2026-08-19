"""The daily cycle against real storage.

``IntelService.reconcile()`` has been tested since the lifecycle landed and has
never had a production caller. These tests are about the caller: that it goes
THROUGH the service rather than around it, that one source cannot take the
cycle down with it, and that a quiet re-harvest stays quiet.

The last one matters more than it looks. An audit trail that logs every re-read
of an unchanged source drowns the transitions it exists to show, and the whole
value of ``label_events`` is that a reader can reconstruct how a label came to
be able to name somebody.

``TestStaleness`` is the other side of that coin, and it is here because the
quietness above had a cost. A source whose publisher froze keeps succeeding —
same file, same claims, ``unchanged`` every morning — and the cycle exited 0
for as long as that lasted. These tests pin the alarm that had to exist for
"a source that has silently produced nothing for three weeks" to be a thing the
scheduler can actually say.
"""

from __future__ import annotations

import shutil
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cipherchain.harvest.exchanges import (
    BINANCE,
    COINBASE,
    COINBASE_PARSERS,
    OKX,
    manual_drop_sources,
)
from cipherchain.harvest.parsers import parse_coinbase_reserves
from cipherchain.harvest.sources import (
    FirstAvailableSource,
    HttpDocumentSource,
    ManualDropSource,
    SourceSpec,
)
from cipherchain.harvest.worker import HarvestReport, HarvestWorker, SourceOutcome
from cipherchain.intel.policy import IntelClaim
from cipherchain.intel.service import CORROBORATOR, IntelService
from cipherchain.storage.repositories import LabelRepository

FIXTURES = Path(__file__).parent / "fixtures"
BINANCE_ADDRESS = "0x28c6c06298d514db089934071355e5743bf21d60"
NOW = datetime(2026, 8, 16, 3, 15, tzinfo=UTC)


def report_of(*sources: SourceOutcome, finished: datetime = NOW) -> HarvestReport:
    return HarvestReport(started_at=finished, finished_at=finished, sources=sources)


def published(days_ago: float, *, allowed: int, name: str = "a-source") -> SourceOutcome:
    return SourceOutcome(
        source=name,
        claims=1,
        unchanged=1,
        published_at=NOW - timedelta(days=days_ago),
        stale_after_days=allowed,
    )


def drop(tmp_path: Path, name: str, fixture: str, date: str = "2026-08-14") -> Path:
    suffix = fixture.rsplit(".", 1)[-1]
    target = tmp_path / f"{name}__{date}.{suffix}"
    shutil.copyfile(FIXTURES / fixture, target)
    return target


def worker(sessions: async_sessionmaker[AsyncSession], tmp_path: Path) -> HarvestWorker:
    return HarvestWorker(sessions, manual_drop_sources(tmp_path))


async def labels(sessions: async_sessionmaker[AsyncSession]) -> list[Any]:
    async with sessions() as session:
        return await LabelRepository(session).active_labels()


async def events(sessions: async_sessionmaker[AsyncSession]) -> list[Any]:
    async with sessions() as session:
        return await LabelRepository(session).events_after(0, limit=1000)


async def report_a_community_claim(
    sessions: async_sessionmaker[AsyncSession], *, entity: str, address: str
) -> None:
    async with sessions() as session:
        await IntelService(session).ingest(
            IntelClaim(
                chain="ethereum",
                address=address,
                entity=entity,
                category="vasp",
                role="operational",
                confidence=0.4,
                method="community",
                source="report:key-1",
                retrieved_at=datetime(2026, 8, 1, tzinfo=UTC),
                reporter="key-1",
            )
        )
        await session.commit()


class TestCycle:
    async def test_a_dropped_disclosure_becomes_active_labels_with_their_events(
        self, sessions: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        drop(tmp_path, BINANCE.name, "binance_labelpack.json")
        report = await worker(sessions, tmp_path).run()

        binance = next(o for o in report.sources if o.source == BINANCE.name)
        assert (binance.added, binance.claims) == (3, 3)
        rows = await labels(sessions)
        assert len(rows) == 3
        assert {row.source for row in rows} == {BINANCE.name}
        assert {row.method for row in rows} == {"first_party_published"}
        # Every arrival is on the record, and the actor is the SOURCE, not the
        # worker: the worker holds no claim of its own.
        assert {(e.kind, e.actor) for e in await events(sessions)} == {("added", BINANCE.name)}

    async def test_a_second_cycle_over_an_unchanged_drop_writes_no_new_events(
        self, sessions: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        drop(tmp_path, BINANCE.name, "binance_labelpack.json")
        await worker(sessions, tmp_path).run()
        before = len(await events(sessions))

        second = await worker(sessions, tmp_path).run()

        binance = next(o for o in second.sources if o.source == BINANCE.name)
        assert (binance.unchanged, binance.added, binance.updated) == (3, 0, 0)
        assert len(await events(sessions)) == before

    async def test_a_source_with_nothing_dropped_does_not_stop_the_others(
        self, sessions: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        """A publisher that has released nothing is a normal cycle. It is still
        reported: "no drop this week" has to be visible, because coverage
        decaying quietly is what makes a run answer "no named endpoint" as
        though that were the chain's fault."""
        drop(tmp_path, OKX.name, "okx_proof_of_reserves.csv", date="2026-08-16")
        report = await worker(sessions, tmp_path).run()

        by_source = {o.source: o for o in report.sources}
        assert by_source[OKX.name].added == 5
        assert by_source[COINBASE.name].error is not None
        assert by_source[BINANCE.name].error is not None
        # Both contributed nothing and both are graded as AWAITING rather than
        # broken: neither has ever put a label in this store, so there is
        # nothing to have regressed. `failed_sources` is what the exit code and
        # the sync panel paint red, and a drop-only source nobody has supplied
        # yet would otherwise paint it red every day forever.
        assert len(report.failed_sources) == 0
        assert {o.source for o in report.unsupplied_sources} == {COINBASE.name, BINANCE.name}
        assert len(await labels(sessions)) == 5

    async def test_a_rejected_file_does_not_roll_back_a_good_source(
        self, sessions: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        """Each source commits in its own session precisely so this holds. One
        corrupt download must not undo the two that arrived intact."""
        drop(tmp_path, BINANCE.name, "binance_labelpack.json")
        broken = drop(tmp_path, OKX.name, "okx_proof_of_reserves.csv", date="2026-08-16")
        broken.write_text("<html>Access denied</html>")

        report = await worker(sessions, tmp_path).run()

        by_source = {o.source: o for o in report.sources}
        assert by_source[OKX.name].error is not None
        assert by_source[BINANCE.name].added == 3
        assert len(await labels(sessions)) == 3

    async def test_a_source_that_raises_something_unforeseen_is_contained_too(
        self, sessions: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        """Containment cannot depend on the failure being a polite one. A source
        that raises anything at all still ends itself and nothing else — the
        other sources commit and the reconcile pass runs."""

        spec = SourceSpec(
            name="unforeseen",
            entity="Somebody",
            method="first_party_published",
            document_url="https://example.test/disclosure",
        )

        class Exploding:
            def __init__(self) -> None:
                self.spec = spec

            async def load(self) -> Any:
                raise ZeroDivisionError("something nobody wrote a branch for")

            def parse(self, document: Any, *, retrieved_at: datetime) -> Any:
                raise AssertionError("unreachable")

        drop(tmp_path, BINANCE.name, "binance_labelpack.json")
        sources = [*manual_drop_sources(tmp_path), Exploding()]
        report = await HarvestWorker(sessions, sources).run()  # type: ignore[arg-type]

        by_source = {o.source: o for o in report.sources}
        assert "ZeroDivisionError" in (by_source["unforeseen"].error or "")
        assert by_source[BINANCE.name].added == 3
        assert report.reconcile_error is None
        assert len(await labels(sessions)) == 3

    async def test_the_summary_names_every_source_and_the_reconcile_pass(
        self, sessions: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        drop(tmp_path, BINANCE.name, "binance_labelpack.json")
        summary = (await worker(sessions, tmp_path).run()).summary()
        for spec in (COINBASE, BINANCE, OKX):
            assert spec.name in summary
        assert "reconcile" in summary


class TestLifecycleIsNotBypassed:
    async def test_a_community_report_is_promoted_only_by_corroboration(
        self, sessions: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        """The harvester never promotes anything itself. It ingests a trusted
        source's claim and runs the reconcile pass, and the pass is what
        recognises that an independent source now agrees — with the corroborator
        named on the row, and 'corroboration-bot' as the actor because the bot
        holds no claim of its own."""
        await report_a_community_claim(sessions, entity="Binance", address=BINANCE_ADDRESS)
        drop(tmp_path, BINANCE.name, "binance_labelpack.json")

        report = await worker(sessions, tmp_path).run()

        assert len(report.promoted) == 1
        async with sessions() as session:
            rows = await LabelRepository(session).claims_for("ethereum", BINANCE_ADDRESS)
        promoted = next(row for row in rows if row.source == "report:key-1")
        assert promoted.status == "active"
        assert promoted.corroborated_by == BINANCE.name
        assert promoted.method == "community"  # still a report; only its STATUS changed
        kinds = {(e.kind, e.actor) for e in await events(sessions)}
        assert ("promoted", CORROBORATOR) in kinds

    async def test_an_uncorroborated_report_is_still_pending_after_a_cycle(
        self, sessions: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        """ "Binance Charity" is plausibly a different operator, so a harvest of
        Binance's own addresses does not confirm it. A cycle that ran is not a
        cycle that promoted."""
        await report_a_community_claim(sessions, entity="Binance Charity", address=BINANCE_ADDRESS)
        drop(tmp_path, BINANCE.name, "binance_labelpack.json")

        report = await worker(sessions, tmp_path).run()

        assert report.promoted == ()
        async with sessions() as session:
            pending = await LabelRepository(session).pending_labels()
        assert [row.entity for row in pending] == ["Binance Charity"]


class TestFailureContainment:
    async def test_a_failing_reconcile_still_reports_the_harvest_that_committed(
        self, sessions: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        """The harvested rows are already committed. Losing the report on top of
        a failed reconcile would lose the only record that the cycle ran, and
        the next cycle re-settles the store anyway."""

        class FailsAfter:
            def __init__(self, factory: async_sessionmaker[AsyncSession], allowed: int) -> None:
                self._factory = factory
                self._allowed = allowed
                self.calls = 0

            def __call__(self) -> AsyncIterator[AsyncSession]:
                self.calls += 1
                if self.calls > self._allowed:
                    raise RuntimeError("database went away")
                return self._factory()  # type: ignore[return-value]

        drop(tmp_path, BINANCE.name, "binance_labelpack.json")
        # Two sessions precede the reconcile pass: the cycle reads which sources
        # have ever contributed (once, up front — see
        # `_sources_that_have_contributed`), then binance ingests. coinbase and
        # okx have no drop and open nothing, so the count does not move when a
        # drop appears or disappears.
        factory = FailsAfter(sessions, allowed=2)
        report = await HarvestWorker(
            factory,  # type: ignore[arg-type]
            manual_drop_sources(tmp_path),
        ).run()

        assert report.reconcile_error is not None
        assert next(o for o in report.sources if o.source == BINANCE.name).added == 3
        assert len(await labels(sessions)) == 3


class TestNeverSuppliedVersusGone:
    """The grading that keeps the panel readable.

    Binance and OKX can never fetch for themselves — their disclosure pages
    answer a bot check, and getting past one is out of bounds. So on any
    deployment where nobody has done the download by hand, treating them as
    failures paints the panel red every morning forever, and a warning light
    that is always on is one nobody reads on the morning OFAC breaks.
    """

    async def test_a_source_nobody_ever_supplied_is_not_a_failure(
        self, sessions: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        report = await HarvestWorker(sessions, manual_drop_sources(tmp_path)).run()
        assert report.failed_sources == ()
        assert {o.source for o in report.unsupplied_sources} == {
            COINBASE.name,
            BINANCE.name,
            OKX.name,
        }
        assert "AWAITING DROP" in report.summary()

    async def test_a_drop_that_worked_and_then_vanished_IS_a_failure(
        self, sessions: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        """The case the grading must not swallow. Once a source has put labels
        in the store, a directory with no file in it means somebody deleted
        what was working — coverage is now ageing silently, which is exactly
        the condition this subsystem exists to shout about."""
        path = drop(tmp_path, BINANCE.name, "binance_labelpack.json")
        first = await HarvestWorker(sessions, manual_drop_sources(tmp_path)).run()
        assert next(o for o in first.sources if o.source == BINANCE.name).added > 0

        path.unlink()
        second = await HarvestWorker(sessions, manual_drop_sources(tmp_path)).run()
        binance = next(o for o in second.sources if o.source == BINANCE.name)
        assert binance.not_supplied is False
        assert [o.source for o in second.failed_sources] == [BINANCE.name]
        # coinbase and okx never contributed, so they stay graded as setup even
        # in the same cycle that reports a real regression beside them.
        assert {o.source for o in second.unsupplied_sources} == {COINBASE.name, OKX.name}


    async def test_a_fetchable_source_is_never_merely_awaiting_a_drop(
        self, sessions: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        """Coinbase and OFAC reach the drop path only as a fallback behind a
        fetch that failed. Grading either as "awaiting its first drop" would
        file a dead publisher under "somebody still has to do the download",
        and nobody would go and look.

        It holds because `FirstAvailableSource.load` raises a plain
        SourceUnavailable summarising every transport rather than re-raising
        the last one — which is easy to "tidy" away, hence this test.
        """
        source = FirstAvailableSource(
            COINBASE,
            [
                HttpDocumentSource(
                    COINBASE,
                    httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(503))),
                    parser=parse_coinbase_reserves,
                    media="html",
                ),
                ManualDropSource(COINBASE, tmp_path, parsers=COINBASE_PARSERS),
            ],
            parsers=COINBASE_PARSERS,
        )
        report = await HarvestWorker(sessions, [source]).run()
        outcome = report.sources[0]
        assert outcome.not_supplied is False
        assert report.failed_sources == (outcome,)


class TestStaleness:
    """Coverage decaying quietly is this subsystem's real failure, and until
    now nothing could see it. Every case here succeeds — no error, no
    exception, rows committed — and the cycle still has to say so."""

    def test_a_publication_inside_its_window_is_not_stale(self) -> None:
        assert report_of(published(2, allowed=3)).stale_sources() == ()

    def test_a_source_still_serving_the_same_old_document_is_stale(self) -> None:
        """The three-weeks case. The drop is on disk, the claims re-ingest as
        `unchanged`, every other line of the report reads green — and nobody
        has republished the document since June."""
        stale = report_of(published(40, allowed=35, name="binance-proof-of-reserves"))
        assert [outcome.source for outcome in stale.stale_sources()] == [
            "binance-proof-of-reserves"
        ]

    def test_each_source_is_judged_by_its_own_publishers_cadence(self) -> None:
        """A page that restates itself hourly and a monthly proof-of-reserves
        file cannot share a threshold: one number would either cry every fourth
        week or never fire."""
        report = report_of(
            published(5, allowed=3, name="coinbase-cbbtc-reserves"),
            published(5, allowed=35, name="okx-proof-of-reserves"),
        )
        assert [outcome.source for outcome in report.stale_sources()] == ["coinbase-cbbtc-reserves"]

    def test_a_source_that_failed_is_not_also_counted_as_stale(self) -> None:
        """It contributed nothing, which is exit 1 and already named. Counting
        it twice would make the stale list the place failures go to be
        double-reported, and the list has to stay worth reading."""
        report = report_of(SourceOutcome("okx-proof-of-reserves", error="no drop"))
        assert report.failed_sources != ()
        assert report.stale_sources() == ()

    def test_an_operator_can_hold_a_tighter_line_across_every_source(self) -> None:
        report = report_of(published(10, allowed=35, name="okx-proof-of-reserves"))
        assert report.stale_sources() == ()
        assert len(report.stale_sources(stale_after_days=7)) == 1

    def test_the_summary_names_the_source_the_date_and_the_window(self) -> None:
        """ "Something is stale" is not actionable at 3am. The line has to say
        which publisher, how old, and what it was allowed to be."""
        summary = report_of(published(40, allowed=35, name="binance-proof-of-reserves")).summary()
        assert "STALE: binance-proof-of-reserves" in summary
        assert "40 days ago" in summary
        assert "may go 35" in summary

    def test_staleness_is_measured_against_the_cycles_own_clock(self) -> None:
        """The report is a closed record. Read out of a log next month it must
        still say what it said when it was printed, so `finished_at` is the
        `now` — not whatever time it happens to be when somebody asks."""
        outcome = published(40, allowed=35)
        fresh = HarvestReport(
            started_at=NOW,
            finished_at=outcome.published_at or NOW,  # cycle ran the day it was published
            sources=(outcome,),
        )
        assert fresh.stale_sources() == ()

    async def test_the_cycle_records_the_date_the_publisher_declared(
        self, sessions: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        """End of the wire: the drop's declared date reaches the outcome, which
        is what the alarm reads. Taken from the CLAIMS rather than the
        document, so a loader and a parser disagreeing about the date cannot
        show up as a healthy report over rows dated something else."""
        drop(tmp_path, OKX.name, "okx_proof_of_reserves.csv", date="2026-06-01")
        report = await worker(sessions, tmp_path).run()
        okx = next(o for o in report.sources if o.source == OKX.name)
        assert okx.published_at == datetime(2026, 6, 1, tzinfo=UTC)
        assert okx.ok  # nothing failed; that is the entire problem
        assert [o.source for o in report.stale_sources()] == [OKX.name]


class TestScheduler:
    """The cron contract. Exit 1 for "a source contributed nothing" is
    deliberate: a publisher that has silently produced nothing for three weeks
    is exactly the state nobody notices, and a green cron job would hide it."""

    @staticmethod
    def _run(
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        report: Any,
        *extra: str,
        expected_stale_after_days: int | None = None,
    ) -> int:
        from cipherchain.harvest import scheduler

        async def fake(
            database_url: str, drop_dir: Path, *, stale_after_days: int | None = None
        ) -> Any:
            # The override has to reach `run_once`, because that is where the
            # run row is closed with an exit code. If `main` applied it and
            # `run_once` did not, the cron mail and the sync panel would judge
            # the same cycle differently.
            assert stale_after_days == expected_stale_after_days
            return report

        monkeypatch.setattr(scheduler, "run_once", fake)
        return scheduler.main(
            ["--drop-dir", str(tmp_path), "--database-url", "postgresql+asyncpg://x/y", *extra]
        )

    def test_a_clean_cycle_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        now = datetime.now(UTC)
        report = HarvestReport(
            started_at=now,
            finished_at=now,
            sources=(SourceOutcome("okx", claims=1, added=1, published_at=now),),
        )
        assert self._run(monkeypatch, tmp_path, report) == 0

    def test_a_source_that_contributed_nothing_exits_one(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        now = datetime.now(UTC)
        report = HarvestReport(
            started_at=now, finished_at=now, sources=(SourceOutcome("okx", error="no drop"),)
        )
        assert self._run(monkeypatch, tmp_path, report) == 1

    def test_a_failed_reconcile_exits_one(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        now = datetime.now(UTC)
        report = HarvestReport(started_at=now, finished_at=now, reconcile_error="database gone")
        assert self._run(monkeypatch, tmp_path, report) == 1

    def test_a_cycle_where_nothing_failed_and_nothing_is_fresh_exits_three(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Not 0, because the labels are ageing. Not 1, because nothing broke —
        and telling an operator a source failed when it did not sends them to
        look in the wrong place."""
        report = report_of(published(40, allowed=35, name="binance-proof-of-reserves"))
        assert self._run(monkeypatch, tmp_path, report) == 3

    def test_an_outright_failure_outranks_staleness(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A run can be both. A source that is down is the more urgent, so the
        exit code says that; the summary still prints both lines."""
        report = report_of(
            SourceOutcome("okx-proof-of-reserves", error="no drop"),
            published(40, allowed=35, name="binance-proof-of-reserves"),
        )
        assert self._run(monkeypatch, tmp_path, report) == 1

    def test_the_operators_override_reaches_the_exit_code(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        report = report_of(published(10, allowed=35, name="okx-proof-of-reserves"))
        assert self._run(monkeypatch, tmp_path, report) == 0
        assert (
            self._run(
                monkeypatch, tmp_path, report, "--stale-after-days", "7",
                expected_stale_after_days=7,
            )
            == 3
        )

    def test_a_misspelt_threshold_stops_the_run_instead_of_disarming_it(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A typo in the env var must not quietly mean "no threshold" — that
        would switch the alarm off in exactly the deployment careless enough to
        make the typo."""
        monkeypatch.setenv("CIPHERCHAIN_HARVEST_STALE_AFTER_DAYS", "fortnightly")
        report = report_of(published(1, allowed=35))
        with pytest.raises(SystemExit) as raised:
            self._run(monkeypatch, tmp_path, report)
        # 2, not 1: "the run was misconfigured" and "a source failed" send an
        # operator to two different places.
        assert raised.value.code == 2

    async def test_a_cycle_with_no_sources_at_all_still_reconciles(
        self, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        report = await HarvestWorker(sessions, []).run()
        assert report.sources == ()
        assert report.reconcile_error is None
        assert report.finished_at >= report.started_at


@pytest.mark.parametrize("date", ["2026-08-14", "2026-01-01"])
async def test_the_drop_date_becomes_the_claims_source_date(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path, date: str
) -> None:
    """The CSV carries no date of its own, so the operator's declaration in the
    file name is the provenance a reader weighs the claim by."""
    drop(tmp_path, OKX.name, "okx_proof_of_reserves.csv", date=date)
    await worker(sessions, tmp_path).run()
    rows = await labels(sessions)
    assert {row.source_date for row in rows} == {datetime.fromisoformat(date).replace(tzinfo=UTC)}

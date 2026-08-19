"""The sync panel's contract: what a run row means when somebody reads it.

Every test here is about a state that is easy to render as its opposite. A
cycle in which every source failed is the one that matters most — it touches
no label, so anything derived from the labels table would paint it green.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cipherchain.harvest.runs import (
    STALLED_AFTER_SECONDS,
    CycleAlreadyRunning,
    close_run,
    open_run,
    start_cycle,
    sync_status,
)
from cipherchain.harvest.worker import HarvestReport, SourceOutcome


def report(*sources: SourceOutcome, reconcile_error: str | None = None) -> HarvestReport:
    now = datetime.now(UTC)
    return HarvestReport(
        started_at=now - timedelta(seconds=30),
        finished_at=now,
        sources=sources,
        reconcile_error=reconcile_error,
    )


def published(days_ago: float, *, name: str, allowed: int = 35, **kw: object) -> SourceOutcome:
    return SourceOutcome(
        name,
        published_at=datetime.now(UTC) - timedelta(days=days_ago),
        stale_after_days=allowed,
        **kw,  # type: ignore[arg-type]
    )


DROPS = Path("drops")


class TestSyncStatus:
    async def test_a_database_nobody_has_harvested_says_so(self, session: AsyncSession) -> None:
        """Not 'idle'. On a fresh deployment the difference is the whole
        message: idle means the timer works, never_run means nobody enabled it
        and every label in the store arrived by hand."""
        status = await sync_status(session, drop_dir=DROPS)
        assert status.state == "never_run"
        assert status.attention == ("no harvest cycle has ever run on this database",)

    async def test_an_open_row_is_a_cycle_in_flight(
        self, session: AsyncSession, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        await open_run(sessions)
        assert (await sync_status(session, drop_dir=DROPS)).state == "syncing"

    async def test_an_open_row_left_too_long_is_a_killed_run_not_a_slow_one(
        self, session: AsyncSession, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """The failure this catches is a reboot at 03:15 still claiming to be
        syncing at lunchtime. 'stalled' is a distinct word from both 'syncing'
        and 'failed' because the operator's next move differs: nothing is
        running, nothing is broken, and the next cycle will start clean."""
        await open_run(sessions)
        await session.execute(
            text("UPDATE harvest_runs SET started_at = now() - make_interval(secs => :s)"),
            {"s": STALLED_AFTER_SECONDS + 60},
        )
        await session.commit()
        status = await sync_status(session, drop_dir=DROPS)
        assert status.state == "stalled"
        assert "never finished" in status.attention[0]

    async def test_a_cycle_where_every_source_failed_is_not_green(
        self, session: AsyncSession, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """The reason this table exists. Such a cycle writes no label, so
        `max(labels.retrieved_at)` — the tempting shortcut — still reads as
        yesterday's healthy run and renders silence as success."""
        run_id = await open_run(sessions)
        await close_run(
            sessions,
            run_id,
            report=report(SourceOutcome("okx-proof-of-reserves", error="no drop")),
            exit_code=1,
        )
        status = await sync_status(session, drop_dir=DROPS)
        assert status.state == "idle"
        assert status.outcome == "failed"
        assert any("contributed nothing" in line for line in status.attention)

    async def test_a_crash_still_closes_its_row(
        self, session: AsyncSession, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """A cycle that raised must not leave a row that reads as in-flight —
        the crash would present on screen as a sync that is still going."""
        run_id = await open_run(sessions)
        await close_run(sessions, run_id, report=None, exit_code=1, error="RuntimeError: boom")
        status = await sync_status(session, drop_dir=DROPS)
        assert status.state == "idle"
        assert status.outcome == "failed"
        assert status.error == "RuntimeError: boom"

    @pytest.mark.parametrize(
        ("transport", "phrase"),
        [("manual_drop", "download the publisher's current file"), ("fetch_or_drop", "gone quiet")],
    )
    async def test_staleness_is_worded_by_whose_move_it_is(
        self,
        session: AsyncSession,
        sessions: async_sessionmaker[AsyncSession],
        transport: str,
        phrase: str,
    ) -> None:
        """A stale FETCHED source means the publisher stopped; a stale DROPPED
        source means the drop directory holds an old file. The cron summary says
        'STALE' for both, which is right for a log line and useless on a panel
        where the reader is deciding what to do next."""
        name = {
            "manual_drop": "binance-proof-of-reserves",
            "fetch_or_drop": "coinbase-cbbtc-reserves",
        }[transport]
        run_id = await open_run(sessions)
        await close_run(
            sessions,
            run_id,
            report=report(published(90, name=name, allowed=35, claims=1, unchanged=1)),
            exit_code=3,
        )
        status = await sync_status(session, drop_dir=DROPS)
        assert status.outcome == "stale"
        assert any(phrase in line for line in status.attention), status.attention

    async def test_a_source_the_last_cycle_never_mentioned_is_neither_ok_nor_broken(
        self, session: AsyncSession, sessions: async_sessionmaker[AsyncSession]
    ) -> None:
        """Tri-state, not boolean. A source added to the cycle since the last
        run has no outcome to report, and rendering that as a cross would send
        an operator looking for a break that has not happened."""
        run_id = await open_run(sessions)
        await close_run(
            sessions,
            run_id,
            report=report(published(1, name="ofac-sdn", claims=914, added=914)),
            exit_code=0,
        )
        status = await sync_status(session, drop_dir=DROPS)
        by_name = {row["source"]: row for row in status.sources}
        assert by_name["ofac-sdn"]["ok"] is True
        assert by_name["binance-proof-of-reserves"]["ok"] is None

    async def test_the_panel_describes_the_cycle_that_actually_runs(
        self, session: AsyncSession
    ) -> None:
        """Sources come from `daily_sources`, not a second hand-kept list —
        which is how a source gets written and then never scheduled. Transport
        is read off the source object, so it cannot drift from what runs."""
        status = await sync_status(session, drop_dir=DROPS)
        transports = {row["source"]: row["transport"] for row in status.sources}
        assert transports["binance-proof-of-reserves"] == "manual_drop"
        assert transports["okx-proof-of-reserves"] == "manual_drop"
        assert transports["ofac-sdn"] == "fetch_or_drop"


class TestStartCycle:
    """The Sync-now button's contract."""

    async def test_a_second_cycle_is_refused_while_one_runs(
        self, session: AsyncSession, sessions: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        """Refused, not queued. Two concurrent reconciles over a half-written
        harvest can promote a label on evidence the other has not committed."""
        await open_run(sessions)
        with pytest.raises(CycleAlreadyRunning):
            await start_cycle(session, script=tmp_path / "harvest.sh", drop_dir=DROPS)

    async def test_a_killed_cycle_does_not_lock_the_button_out_forever(
        self, session: AsyncSession, sessions: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        """The open row of a killed run must not be mistaken for a live one, or
        a reboot at 03:15 would disable Sync-now until somebody edited the
        database. Same threshold the panel uses to say 'stalled', so the button
        starts working at exactly the moment the panel admits nothing is running.
        """
        await open_run(sessions)
        await session.execute(
            text("UPDATE harvest_runs SET started_at = now() - make_interval(secs => :s)"),
            {"s": STALLED_AFTER_SECONDS + 60},
        )
        await session.commit()
        script = tmp_path / "harvest.sh"
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o755)
        assert await start_cycle(session, script=script, drop_dir=DROPS) > 0

    async def test_a_finished_cycle_leaves_the_button_available(
        self, session: AsyncSession, sessions: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        run_id = await open_run(sessions)
        await close_run(sessions, run_id, report=report(), exit_code=0)
        script = tmp_path / "harvest.sh"
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o755)
        assert await start_cycle(session, script=script, drop_dir=DROPS) > 0

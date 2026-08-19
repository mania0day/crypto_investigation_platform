"""Recording that a harvest cycle happened, and answering "is it happening?".

The harvester exits between cycles by design (``scripts/harvest.sh``), so there
is no resident thing for the API to ask. This module is the seam: the scheduler
writes a row here, and :func:`sync_status` reads it back for the dashboard.

Two decisions worth knowing before reading the code.

**Success is not derivable from the labels table.** The obvious shortcut is
``max(labels.retrieved_at)``, and it is wrong in exactly the case that matters:
a cycle in which every source failed touches no label, so the newest
``retrieved_at`` still reads as yesterday's healthy run. Silence would render
as success on the very morning somebody needs to see red. A run row exists
whether or not the run achieved anything.

**A killed run is resolved by the reader, not hidden by the writer.** A cycle
that is SIGKILLed — the machine rebooted, the container was replaced — never
closes its row, so ``finished_at`` stays NULL and a naive reader reports
"syncing" forever. The fix is not a heartbeat column: that would let the writer
decide what "too long" is, when the answer depends on the deployment, and it
would quietly overwrite the fact that a run died. Instead :func:`sync_status`
compares the open row's age against :data:`STALLED_AFTER_SECONDS` and reports
``stalled`` — a named state an operator can act on, distinct from both
``syncing`` and ``failed``.
"""

from __future__ import annotations

import asyncio
import os
import socket
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cipherchain.harvest.exchanges import SourcePlan, describe_daily_plan
from cipherchain.harvest.worker import HarvestReport
from cipherchain.storage.tables import HarvestRunRow, LabelRow

# How long an unclosed run may sit before the reader calls it dead rather than
# in flight. Sized from the measured cycle, not guessed: the OFAC SDN document
# alone is ~28 MB and took 196 s on the host that measured it and 353.8 s on a
# slower one (harvest/sanctions.py), and it is the last source in the cycle. An
# hour is roughly ten times the worst measured cycle — long enough that a slow
# network morning is never mislabelled dead, short enough that a reboot at 03:15
# is not still claiming to be syncing at lunchtime.
STALLED_AFTER_SECONDS = 3600.0

# Reported per source so the panel can say WHO has to do something. Derived from
# the cycle itself (see `describe_daily_plan`) rather than restated here, because
# a second list of sources is how a source gets written and then never scheduled.
TRANSPORT_AUTOMATIC = "automatic"
TRANSPORT_MANUAL_DROP = "manual_drop"
TRANSPORT_FETCH_OR_DROP = "fetch_or_drop"

# How a claimed run row reaches the child process. An env var rather than an
# argv flag so `scripts/harvest.sh` needs no new option — it already passes
# its own arguments straight through to the scheduler.
RUN_ID_ENV = "CIPHERCHAIN_HARVEST_RUN_ID"


@dataclass(frozen=True, slots=True)
class SyncStatus:
    """What the dashboard's sync panel renders. One object, one query set."""

    state: str  # 'syncing' | 'idle' | 'stalled' | 'never_run'
    started_at: datetime | None = None
    finished_at: datetime | None = None
    outcome: str | None = None  # last CLOSED run's status: ok / failed / stale
    exit_code: int | None = None
    error: str | None = None
    host: str | None = None
    sources: tuple[dict[str, Any], ...] = ()
    labels_total: int = 0
    labels_by_chain: tuple[tuple[str, int], ...] = ()
    attention: tuple[str, ...] = field(default=())

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


async def open_run(session_factory: async_sessionmaker[AsyncSession]) -> int:
    """Record that a cycle has begun, and commit it immediately.

    Committed before the first source is contacted on purpose. The whole value
    of this row is that it exists while the work is happening; deferring the
    write until the end would make it a log line, and a cycle that hangs on a
    28 MB download would be indistinguishable from one that was never started.

    When ``CIPHERCHAIN_HARVEST_RUN_ID`` names a row, that row is ADOPTED rather
    than a new one created: the API claims a row before spawning this process
    (see :func:`start_cycle`) so that a double-click cannot start two cycles,
    and a child that opened its own row would leave the claimed one hanging
    open — reported as a stalled run that never existed.
    """
    claimed = os.environ.get(RUN_ID_ENV)
    if claimed:
        async with session_factory() as session:
            row = await session.get(HarvestRunRow, int(claimed))
            if row is not None and row.finished_at is None:
                # Restamped: the claim is when the button was pressed, this is
                # when work actually began, and the difference is process
                # startup the operator should not see as part of the cycle.
                row.started_at = datetime.now(UTC)
                row.host = socket.gethostname()
                await session.commit()
                return int(row.id)
        # The named row is gone or already closed. Falling through to open a
        # fresh one is right: the cycle IS about to run, and a run that happens
        # without a record is the one outcome this table exists to prevent.
    async with session_factory() as session:
        row = HarvestRunRow(status="running", host=socket.gethostname(), sources=[])
        session.add(row)
        await session.commit()
        return int(row.id)


async def close_run(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: int,
    *,
    report: HarvestReport | None,
    exit_code: int,
    error: str | None = None,
) -> None:
    """Close the row with what the cycle actually did.

    ``report`` is None only when the process raised before producing one; the
    row still closes, as 'failed', because an open row would otherwise be read
    as a run still in flight and the crash would present as a hang.
    """
    async with session_factory() as session:
        row = await session.get(HarvestRunRow, run_id)
        if row is None:  # pragma: no cover - the row is written by open_run
            return
        row.finished_at = datetime.now(UTC)
        row.exit_code = exit_code
        row.error = error if error is not None else _reconcile_error(report)
        row.sources = _source_rows(report)
        # Mirrors the exit code, which is already the operator's vocabulary from
        # cron mail. 2 (misconfigured) never reaches here — nothing ran, so no
        # row was opened.
        row.status = {0: "ok", 3: "stale"}.get(exit_code, "failed")
        await session.commit()


def _reconcile_error(report: HarvestReport | None) -> str | None:
    return None if report is None else report.reconcile_error


def _source_rows(report: HarvestReport | None) -> list[dict[str, Any]]:
    """Freeze each source's outcome as the report described it.

    ``stale`` is computed against the report's own ``finished_at`` rather than
    a fresh clock, matching :meth:`HarvestReport.stale_sources`, so this row
    says the same thing next month as it did when written.
    """
    if report is None:
        return []
    return [
        {
            "source": outcome.source,
            "ok": outcome.ok,
            "not_supplied": outcome.not_supplied,
            "error": outcome.error,
            "claims": outcome.claims,
            "added": outcome.added,
            "updated": outcome.updated,
            "unchanged": outcome.unchanged,
            "published_at": (
                outcome.published_at.isoformat() if outcome.published_at is not None else None
            ),
            "age_days": outcome.age_days(report.finished_at),
            "stale_after_days": outcome.stale_after_days,
            "stale": outcome.is_stale(report.finished_at),
        }
        for outcome in report.sources
    ]


async def latest_run(session: AsyncSession) -> HarvestRunRow | None:
    result = await session.execute(
        select(HarvestRunRow).order_by(HarvestRunRow.started_at.desc()).limit(1)
    )
    return result.scalars().first()


async def sync_status(session: AsyncSession, *, drop_dir: Path) -> SyncStatus:
    """Everything the dashboard's sync panel shows, in one call.

    The three facts are deliberately assembled together rather than exposed as
    three endpoints: they are read as one sentence ("the cycle ran at 03:15,
    OKX has not published in 40 days, the store holds 74,928 labels"), and
    split across polls they would drift out of step on screen.
    """
    run = await latest_run(session)
    plan = {entry.name: entry for entry in await describe_daily_plan(drop_dir)}
    by_chain = tuple(
        (str(chain), int(count))
        for chain, count in (
            await session.execute(
                select(LabelRow.chain, func.count())
                .where(LabelRow.status == "active")
                .group_by(LabelRow.chain)
                .order_by(func.count().desc())
            )
        ).all()
    )

    if run is None:
        # Never run is NOT idle. On a fresh deployment the difference is the
        # whole message: idle means the timer is working, never_run means
        # nobody has enabled it and every label in the store is whatever was
        # imported by hand.
        return SyncStatus(
            state="never_run",
            sources=tuple(_planned_only(entry) for entry in plan.values()),
            labels_total=sum(count for _, count in by_chain),
            labels_by_chain=by_chain,
            attention=("no harvest cycle has ever run on this database",),
        )

    open_for = (datetime.now(UTC) - run.started_at).total_seconds()
    if run.finished_at is None:
        state = "syncing" if open_for < STALLED_AFTER_SECONDS else "stalled"
    else:
        state = "idle"

    # Merge what the run reported with what the cycle intends to run. A source
    # in the plan but absent from the run is a source that was added since the
    # last cycle; showing it as unknown is more useful than omitting it, which
    # would read as "not configured".
    reported = {str(row.get("source")): dict(row) for row in (run.sources or [])}
    sources = []
    for name, entry in plan.items():
        row = reported.get(name) or _planned_only(entry)
        row["transport"] = entry.transport
        row["entity"] = entry.entity
        row["document_url"] = entry.document_url
        sources.append(row)

    return SyncStatus(
        state=state,
        started_at=run.started_at,
        finished_at=run.finished_at,
        outcome=run.status if run.finished_at is not None else None,
        exit_code=run.exit_code,
        error=run.error,
        host=run.host,
        sources=tuple(sources),
        labels_total=sum(count for _, count in by_chain),
        labels_by_chain=by_chain,
        attention=_attention(state, run, sources, open_for),
    )


def _planned_only(entry: SourcePlan) -> dict[str, Any]:
    """A source the newest run says nothing about — scheduled, never reported."""
    return {
        "source": entry.name,
        "entity": entry.entity,
        "transport": entry.transport,
        "document_url": entry.document_url,
        "ok": None,
        "not_supplied": None,
        "error": None,
        "published_at": None,
        "age_days": None,
        "stale": None,
        "stale_after_days": entry.stale_after_days,
    }


def _attention(
    state: str, run: HarvestRunRow, sources: list[dict[str, Any]], open_for: float
) -> tuple[str, ...]:
    """The panel's red lines: what a person has to do, in the words they need.

    Staleness is worded per transport because the action differs completely. A
    stale automatic source means the PUBLISHER stopped and somebody should go
    and look; a stale manual-drop source means the drop directory is holding an
    old file and the fix is a download. Saying "STALE" for both, as the cron
    summary does, is right for a log line and useless on a dashboard.
    """
    lines: list[str] = []
    if state == "stalled":
        lines.append(
            f"a cycle started {open_for / 3600:.1f}h ago on {run.host or 'an unknown host'} "
            "and never finished — it was killed, not hung; the next cycle will start clean"
        )
    if run.error:
        lines.append(f"reconcile failed: {run.error}")
    for row in sources:
        if row.get("not_supplied"):
            # Deliberately NOT phrased as a failure. This source has never been
            # supplied on this deployment, which is a setup step; wording it in
            # red alongside a genuine break is how a panel that is red every
            # morning stops being read at all.
            lines.append(
                f"{row['source']} is waiting for its first drop — download the publisher's "
                "file on a machine that can reach them and put it in the drop directory"
            )
        elif row.get("ok") is False:
            lines.append(f"{row['source']} contributed nothing: {row.get('error')}")
        elif row.get("stale"):
            age, limit = row.get("age_days") or 0.0, row.get("stale_after_days")
            if row.get("transport") == "manual_drop":
                lines.append(
                    f"{row['source']} is running on a {age:.0f}-day-old drop (its window is "
                    f"{limit}d) — download the publisher's current file into the drop directory"
                )
            else:
                lines.append(
                    f"{row['source']} keeps serving a document published {age:.0f} days ago "
                    f"(its window is {limit}d) — the fetch is fine, the publisher has gone quiet"
                )
    return tuple(lines)


class CycleAlreadyRunning(RuntimeError):
    """A cycle is in flight. Starting a second is refused, not queued.

    Two cycles against one store is not merely wasteful. Both would download
    the same 28 MB SDN document and both would call ``IntelService.reconcile``,
    which promotes and demotes labels by comparing sources against each other —
    running that twice concurrently over a half-written harvest is how a label
    gets promoted on evidence the other transaction is still writing.
    """


async def start_cycle(
    session: AsyncSession,
    *,
    script: Path,
    drop_dir: Path,
    env: dict[str, str] | None = None,
) -> int:
    """Launch a harvest cycle as a SEPARATE PROCESS and return its pid.

    A subprocess, never a task in this event loop. The API serves
    investigations; a 28 MB download plus a full reconcile inside a worker
    would block it, and under more than one worker the button would start one
    cycle per worker that happened to receive the click. ``scripts/harvest.sh``
    makes the same argument about not living inside the API, and pressing a
    button does not change where the work belongs — only who decides when.

    **The row is claimed HERE, before the child is spawned**, and handed down in
    ``CIPHERCHAIN_HARVEST_RUN_ID``. Letting the child open its own row is the
    obvious arrangement and it does not hold: the child needs seconds to boot
    Python, import, and reach the database, and a second press inside that
    window finds no open row and starts a second cycle. Measured — two presses
    a second apart both returned 201 and both cycles ran. Claiming first makes
    the guard and the record the same fact.
    """
    if await _in_flight(session):
        raise CycleAlreadyRunning("a harvest cycle is already running")
    row = HarvestRunRow(status="running", host=socket.gethostname(), sources=[])
    session.add(row)
    await session.commit()
    run_id = int(row.id)
    try:
        process = await asyncio.create_subprocess_exec(
            str(script),
            "--drop-dir",
            str(drop_dir),
            cwd=str(script.resolve().parent.parent),
            env={**os.environ, **(env or {}), RUN_ID_ENV: str(run_id)},
            # Detached from this request's lifetime: the cycle outlives the HTTP
            # response by minutes. stdout/stderr go nowhere rather than to a pipe
            # nobody drains — a full pipe buffer would hang the child part way
            # through the SDN download. The cycle's own record is the run row, and
            # its log is the service journal when systemd starts it.
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
    except BaseException as exc:
        # A claim that never became a cycle must not sit open for an hour
        # blocking the button and showing "syncing" for work nobody is doing.
        row.finished_at = datetime.now(UTC)
        row.status = "failed"
        row.exit_code = 1
        row.error = f"could not start {script}: {type(exc).__name__}: {exc}"
        await session.commit()
        raise
    return process.pid


async def _in_flight(session: AsyncSession) -> bool:
    """Is a cycle genuinely running right now?

    Not simply "is a row open" — a killed cycle leaves one open forever, and
    that must not lock the button out permanently. Same threshold the panel
    uses to call a run stalled, so the two can never disagree on screen: the
    moment it reads 'stalled', the button works again.
    """
    run = await latest_run(session)
    if run is None or run.finished_at is not None:
        return False
    return (datetime.now(UTC) - run.started_at).total_seconds() < STALLED_AFTER_SECONDS

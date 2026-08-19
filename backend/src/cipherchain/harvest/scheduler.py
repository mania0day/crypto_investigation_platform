"""The daily entry point — one cycle per invocation, then exit.

Cron, not a resident loop. The cycle is short, idempotent (an unchanged
re-harvest is ``unchanged`` and writes no event), and holds no state between
runs, so a process that exits is one fewer thing to supervise and a missed day
costs nothing but a day. A crontab line::

    15 3 * * *  cd /srv/cipherchain/backend && .venv/bin/python -m cipherchain.harvest.scheduler \\
                    --drop-dir /var/lib/cipherchain/drops >> /var/log/cipherchain/harvest.log 2>&1

Exit codes, so the cron mail means something:

===  ==========================================================================
0    every source contributed, on a document its publisher still stands behind,
     and the reconcile pass completed
1    at least one source contributed nothing, or reconcile failed — the rest of
     the cycle still committed, and the log names what did not
2    misconfiguration; nothing ran
3    every source contributed, but at least one of them contributed the SAME
     OLD DOCUMENT: nobody has republished it inside the window that source
     declares (:attr:`SourceSpec.stale_after_days`). Nothing failed. That is
     the point — see below
===  ==========================================================================

Exit 1 is deliberately not exit 0. A source that has silently produced nothing
for three weeks is the failure mode this whole subsystem has — coverage decays
quietly, and the tool keeps answering "no named endpoint" as if that were the
chain's fault rather than the store's.

Exit 3 exists because exit 1 could not see that failure. Exit 1 fires when a
source fails TODAY. The three-weeks case does not fail: the drop file is still
on disk, or the page still answers 200, and every claim re-ingests as
``unchanged``. Every line of the old report read green while the labels aged
out. So staleness is measured on the publisher's own date — the newest
``source_date`` that reached the store this cycle — and reported by name, with
a non-zero exit, on a cycle where nothing else went wrong.

Which sources need a human
--------------------------
``--drop-dir`` is where an operator puts files for the sources that cannot be
fetched. Which those are, why, and exactly how to name the file is recorded in
:mod:`cipherchain.harvest.exchanges`; the short version is that Coinbase and
the OFAC SDN list are automatic, and Binance and OKX are not, because both of
them bot-block.

How long a cycle takes, so nobody shortens it by accident: the SDN list is
~28 MB and streams at about 77 KB/s from here, so a normal run is minutes
rather than seconds and is almost entirely that one download
(:data:`cipherchain.harvest.sanctions.OFAC_SDN_TIMEOUT_SECONDS` carries the
arithmetic). A cron line that kills the job early turns a working sanctions
feed back into a source that reports "unavailable" every morning.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

import httpx

from cipherchain.core.config import get_settings
from cipherchain.harvest.exchanges import daily_sources
from cipherchain.harvest.runs import close_run, open_run
from cipherchain.harvest.worker import HarvestReport, HarvestWorker
from cipherchain.storage.db import create_engine, create_session_factory

logger = logging.getLogger(__name__)

DEFAULT_DROP_DIR = Path("drops")

# Generous, because one fetch of one page has no reason to be quick and a
# timeout here costs the whole source for the day. This is the ceiling for
# every SMALL request — robots.txt, a reserves page, the SDN endpoint's
# redirect. The one multi-megabyte download overrides it per request
# (`sanctions.OFAC_SDN_TIMEOUT_SECONDS`) rather than this being raised for
# everybody, which would hand a stalled 350 KB page the patience a 28 MB
# document needs.
FETCH_TIMEOUT_SECONDS = 30.0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cipherchain.harvest.scheduler",
        description="Refresh the harvest sources and re-settle the label lifecycle.",
    )
    parser.add_argument(
        "--drop-dir",
        type=Path,
        default=Path(os.environ.get("CIPHERCHAIN_DROP_DIR", DEFAULT_DROP_DIR)),
        help="directory the operator drops published disclosures into",
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="overrides DATABASE_URL / the configured default",
    )
    parser.add_argument(
        "--stale-after-days",
        type=int,
        default=_optional_int(os.environ.get("CIPHERCHAIN_HARVEST_STALE_AFTER_DAYS")),
        help=(
            "override every source's own staleness window (days). Each source ships "
            "one matched to how often its publisher actually republishes; this is for "
            "holding a tighter line across the board, not for silencing the alarm"
        ),
    )
    return parser.parse_args(argv)


def _optional_int(raw: str | None) -> int | None:
    """A malformed env var must not silently mean "no threshold" — that would
    turn the alarm off in exactly the deployment careless enough to typo it.

    Exits 2, because that is this module's documented code for "the run was
    misconfigured and nothing happened", and a cron mail that says 1 would send
    somebody looking for a source that failed.
    """
    if raw is None or not raw.strip():
        return None
    try:
        return int(raw)
    except ValueError:
        print(
            f"error: CIPHERCHAIN_HARVEST_STALE_AFTER_DAYS={raw!r} is not a number",
            file=sys.stderr,
        )
        raise SystemExit(2) from None


async def run_once(
    database_url: str, drop_dir: Path, *, stale_after_days: int | None = None
) -> HarvestReport:
    """One cycle. Separated from :func:`main` so a test drives it directly.

    ``follow_redirects`` is off deliberately: a moved document is a thing a
    person has to look at, not a thing to chase. See
    :class:`cipherchain.harvest.sources.HttpDocumentSource`.

    The cycle also records ITSELF, in ``harvest_runs``, so that something which
    exits between runs can still be asked "are you running?" by the dashboard
    (:mod:`cipherchain.harvest.runs`). The row opens before the first source is
    contacted and closes in a ``finally``, including when the cycle raises —
    an open row means in-flight, so a crash that left one open would render on
    screen as a sync that is still going.

    ``stale_after_days`` is threaded through only to decide the recorded exit
    code. The operator's override has to reach the row, or the panel would call
    a run healthy that the same run's cron mail called stale.
    """
    engine = create_engine(database_url)
    session_factory = create_session_factory(engine)
    try:
        run_id = await open_run(session_factory)
        report: HarvestReport | None = None
        failure: str | None = None
        try:
            async with httpx.AsyncClient(
                timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=False
            ) as http:
                worker = HarvestWorker(session_factory, daily_sources(drop_dir, http))
                report = await worker.run()
            return report
        except BaseException as exc:
            # BaseException, not Exception: a cycle cancelled or SIGINTed part
            # way through must still close its row. Leaving it open is the one
            # outcome that misreports — "syncing" forever — and the exception
            # is re-raised untouched, so nothing about the failure is swallowed.
            failure = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            await close_run(
                session_factory,
                run_id,
                report=report,
                exit_code=_exit_code(report, stale_after_days=stale_after_days),
                error=failure,
            )
    finally:
        await engine.dispose()


def _exit_code(report: HarvestReport | None, *, stale_after_days: int | None) -> int:
    """The scheduler's exit code, computed once and used twice.

    :func:`main` returns it to cron and :func:`close_run` stores it, and those
    two must not be able to disagree — a panel saying "ok" beside a cron mail
    saying "3" is worse than either alone. 2 is absent by construction: it means
    nothing ran, and nothing that never ran opened a row.
    """
    if report is None:
        return 1
    if report.reconcile_error is not None or report.failed_sources:
        return 1
    if report.stale_sources(stale_after_days=stale_after_days):
        return 3
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    args = _parse_args(argv)
    url = args.database_url or get_settings().database_url
    if not url:
        # Reachable only when the url is explicitly BLANK. `Settings.database_url`
        # carries a localhost default, so a host with no .env and no DATABASE_URL
        # does not land here — it tries localhost:5432 and exits 1 with a
        # connection error instead. That is the right split (a database that is
        # configured but unreachable is an operational failure, not a
        # misconfiguration) but it is worth saying, because "exit 2 means nothing
        # ran" invites the assumption that an unset DATABASE_URL produces it.
        print("error: no database url — set DATABASE_URL or pass --database-url", file=sys.stderr)
        return 2
    report = asyncio.run(run_once(url, args.drop_dir, stale_after_days=args.stale_after_days))
    print(report.summary(stale_after_days=args.stale_after_days))
    # Deliberately NOT recomputed here. `_exit_code` is what the run row was
    # closed with, and the cron mail and the dashboard have to be the same
    # judgement — a panel that says 'ok' beside an exit 3 is worse than either
    # alone. The precedence it encodes is unchanged: a failure outranks
    # staleness, because a source that is down is the more urgent of the two
    # and a run can be both.
    return _exit_code(report, stale_after_days=args.stale_after_days)


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())

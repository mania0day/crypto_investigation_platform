"""Reading one investigation out of storage in the shape a report needs.

The split is deliberate: everything the document *says* is decided in
``model.py`` without a database, and this module does nothing but fetch. That is
what lets the hard cases — no answers, one answer, zero findings, a partial run —
be tested exactly, instead of being approximated by whatever a live trace happens
to produce on the day.

Two things are fetched that the findings alone cannot supply:

- **Hops.** "Nearest" is measured in hops, and hop belongs to the traversal
  rather than to a conclusion, so it is joined in from the node record
  (``vasp_findings_with_hops``).
- **Coverage counters.** Truncated histories, depth-horizon stops and an
  undrained frontier are read from the traversal record as NUMBERS. The engine
  also states them in prose, which the caveats reproduce verbatim, but a report
  that could only quote prose could not tell a reader how many addresses were
  never read.

The answers themselves are not re-derived here. ``select_answers`` is the single
place that decides what "nearest" and "nearest named" mean, and a report that
computed its own would eventually disagree with the API about the same run.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from cipherchain.core.errors import CipherChainError
from cipherchain.investigation.answers import RankedFinding, claim_entity, select_answers
from cipherchain.investigation.budgets import BudgetExtension
from cipherchain.investigation.engine import TERMINAL_MIXER, TERMINAL_MIXER_CROSSED
from cipherchain.investigation.objectives import Objective
from cipherchain.reporting.model import (
    InvestigationReport,
    ReportHeader,
    TraversalCoverage,
    build_report,
)
from cipherchain.reporting.vasp import VaspLookup, resolve_profiles
from cipherchain.storage.repositories import FactRepository, InvestigationRepository
from cipherchain.storage.tables import InvestigationRow

DEPTH_HORIZON_REASON = "depth_horizon"
#: Imported rather than re-spelled: these are written by the engine and read
#: here, and a typo in either copy would silently report zero mixer contacts on
#: a run that made several.
MIXER_STOP_REASON = TERMINAL_MIXER
MIXER_CROSSED_REASON = TERMINAL_MIXER_CROSSED


class ReportNotFound(CipherChainError):
    """No such investigation — there is nothing to report on."""

    def __init__(self, investigation_id: uuid.UUID) -> None:
        super().__init__(f"unknown investigation {investigation_id}")
        self.investigation_id = investigation_id


def _int_or_none(raw: Any) -> int | None:
    """Spend and budget dicts are JSONB; a bad value must not break a report."""
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _extension_statements(raw: Any) -> tuple[str, ...]:
    """The run's self-granted allowances, read back as the engine stated them.

    Read from the record rather than from the terminal findings, because the
    successful case files no terminal: a run that extended three times, named
    the exchange and drained its frontier closes every objective and has nothing
    left to hang the disclosure on. The number would then appear only in reports
    where the pursuit FAILED — which is the one place it flatters the tool.

    Rendered through ``BudgetExtension.statement()`` so the document and the
    engine's own evidence say it the same way. Malformed JSONB is skipped rather
    than raised on: a coverage figure nobody can parse must not be the reason an
    investigator cannot open the report.
    """
    if not isinstance(raw, list):
        return ()
    statements: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            statements.append(BudgetExtension.from_dict(item).statement())
        except (TypeError, ValueError):
            continue
    return tuple(statements)


async def collect_coverage(
    investigations: InvestigationRepository, row: InvestigationRow
) -> TraversalCoverage:
    """Every limit that closed a branch without reading it, as numbers.

    Extracted from ``collect_report`` so the API can answer "was this run
    complete?" with the SAME counters the document prints. While it was inlined
    here the report had a coverage record and the API had none, so the only
    honest answer on the wire was a status string — and ``completed`` is the
    status of a run that hit a page limit, a depth horizon and a supernode cap
    on the way to draining its frontier.

    Read by query rather than from ``spent`` (except the two figures that only
    exist there) so a RESUMED run reports the gaps the first run recorded.
    """
    capped_nodes, capped_drops = await investigations.count_capped_expansions(row.id)
    missing_feed_nodes, missing_feeds = await investigations.count_nodes_missing_feeds(row.id)
    return TraversalCoverage(
        addresses_reached=await investigations.count_graph_nodes(row.id),
        truncated_histories=await investigations.count_truncated_histories(row.id),
        depth_horizon_stops=await investigations.count_nodes_terminated_for(
            row.id, DEPTH_HORIZON_REASON
        ),
        mixer_stops=await investigations.count_nodes_terminated_for(row.id, MIXER_STOP_REASON),
        mixer_crossings=await investigations.count_nodes_terminated_for(
            row.id, MIXER_CROSSED_REASON
        ),
        unexplored_frontier=await investigations.count_frontier(row.id),
        capped_expansions=capped_nodes,
        counterparties_dropped=capped_drops,
        addresses_missing_feeds=missing_feed_nodes,
        feeds_unavailable=missing_feeds,
        transactions_examined=_int_or_none(row.spent.get("txs_normalized")),
        max_depth=_int_or_none(row.budgets.get("max_depth")),
        budget_extensions=_extension_statements(row.spent.get("budget_extensions")),
    )


async def collect_report(
    session: AsyncSession,
    investigation_id: uuid.UUID,
    *,
    vasp_lookup: VaspLookup | None = None,
    generated_at: datetime | None = None,
) -> InvestigationReport:
    """Assemble the full report for one investigation.

    ``vasp_lookup`` is injected rather than imported: the metadata table is owned
    by the intel package, may be absent from a build, and its absence must cost
    the report a few reference rows and nothing else
    (``reporting.vasp.default_vasp_lookup`` finds one when there is one).

    Raises ``ReportNotFound`` for an unknown id, which the API edge maps to 404.
    A run that is still going is reported on quite happily — the header and the
    caveats both say it was unfinished.
    """
    investigations = InvestigationRepository(session)
    row = await investigations.get(investigation_id)
    if row is None:
        raise ReportNotFound(investigation_id)
    subject = await FactRepository(session).get_address(row.root_address_id)
    if subject is None:  # pragma: no cover — the root address is written before the row
        raise ReportNotFound(investigation_id)

    findings = await investigations.list_findings(investigation_id)
    ranked = await investigations.vasp_findings_with_hops(investigation_id)
    answers = select_answers(
        [
            RankedFinding(
                finding=r.finding,
                hop=r.hop,
                speculative=r.speculative,
                speculative_basis=r.speculative_basis,
            )
            for r in ranked
        ],
        [Objective(o).direction for o in row.objectives],
    )

    coverage = await collect_coverage(investigations, row)

    # Only endpoints offered as answers are looked up: metadata is reference
    # data about an operator a reader may contact, and a lookup per finding
    # would be a table read per row of a report nobody acts on. Of those, only
    # the ones a claim NAMED are actually asked about — ``resolve_profiles``
    # drops the rest, since metadata may describe an operator but never supply
    # one to an address that no claim attributed.
    endpoints = [
        (r.finding.subject.chain, r.finding.subject.value, claim_entity(r.finding))
        for answer in answers
        for r in (answer.nearest, answer.nearest_named, answer.best_effort)
        if r is not None
    ]
    profiles = await resolve_profiles(vasp_lookup, endpoints)

    header = ReportHeader(
        investigation_id=str(row.id),
        subject=subject,
        status=row.status,
        generated_at=generated_at or datetime.now(UTC),
        engine_version=row.engine_version,
        ruleset_version=row.ruleset_version,
        objectives=tuple(row.objectives),
        started_at=row.created_at,
        updated_at=row.updated_at,
        budgets=dict(row.budgets),
        spent=dict(row.spent),
        error=row.error,
    )
    return build_report(
        header=header,
        findings=findings,
        answers=answers,
        coverage=coverage,
        profiles=profiles,
    )

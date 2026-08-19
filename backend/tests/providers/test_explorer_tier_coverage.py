"""What the fetch tier's short read becomes by the time a reader sees it.

The tier's own tests stop at the provider and at the adapter: the manifest
carries ``truncated_at_page_limit``, the envelope carries the short-read mark,
the page comes back ``truncated``. None of that is the property that matters.
The property that matters is a row in the database — ``nodes.history_truncated``
— because that column is what the report's "Coverage and caveats" section and
the address-level "history truncated" marker are rendered from, and it is the
only thing standing between "we did not look here" and "there was nothing here".

That distinction is the whole product. On a sanctions trace it is the difference
between an unexplored branch and an exonerated one.

So this drives the REAL engine over the REAL ``TronAdapter`` over a REAL
``ProviderPool`` with this tier as the only registered provider, against a real
database, and asserts the column. Every layer in between used to agree the read
was complete while the provider one layer down was logging a warning that it had
stopped early:

    transactions returned : 6
    next_cursor           : None
    gaps                  : ()
    engine marks it truncated? -> False

``next_cursor`` is None here and always will be — this tier cannot mint a
TronGrid fingerprint — and ``gaps`` is empty because both feeds answered. Those
were the only two things the engine tested, which is exactly why a busy Tron
address served by this tier arrived in the report as one whose history simply
ends.

Real Postgres rather than a fake repository, for the reason
``tests/reporting/test_collect.py`` gives: the coverage counters ARE the
assertion, and a fake that records the call proves the engine called something,
not that a reader is told.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cipherchain.chains.base import ChainRegistry
from cipherchain.chains.tron import TronAdapter
from cipherchain.investigation import Budgets, InvestigationEngine, NullAttributor, Objective
from cipherchain.providers.pool import ProviderLimits, ProviderPool
from cipherchain.reporting.collect import collect_report
from cipherchain.reporting.html import render_html
from cipherchain.storage.repositories import InvestigationRepository
from tests.providers.test_explorer_fetch import Clock, build
from tests.providers.test_explorer_fetch_tron import (
    ADDRESS,
    COUNTERPARTY,
    address_page,
    module_path,
    router,
    trx_transfer_page,
)

#: Five explorer pages of history against a three-page bound, so the read stops
#: with the site still holding more. Two ids per page: the bound is what has to
#: stop the read, and a page shorter than the one before it would stop it first
#: (``_enumerate`` reads a short page as the last page) — which would prove the
#: honest end-of-history path instead of the truncation.
PAGES = 5
PER_PAGE = 2
PAGES_PER_CALL = 3

#: Distinct 64-hex Tron ids. Same shape as the real ones, and distinct because
#: `_enumerate` de-duplicates: repeats would collapse into a single page's worth
#: of rows and the page-size test would read the second page as short.
HASHES = [f"{index:064x}" for index in range(1, PAGES * PER_PAGE + 1)]


def busy_address_pages() -> dict[str, str]:
    """One Tron address with more native history than the bound will read."""
    pages: dict[str, str] = {}
    for index in range(PAGES):
        block = HASHES[index * PER_PAGE : (index + 1) * PER_PAGE]
        pages[module_path("tron-main", page=index)] = address_page(block)
    for tx_hash in HASHES:
        # Every transaction pays the same counterparty, so the trace gains one
        # address at hop 1 whose own history the router answers as empty. That
        # node is the control: read in full, and it must NOT be marked.
        pages[f"/tron/transaction/{tx_hash}"] = trx_transfer_page()
    return pages


def engine_over_the_fetch_tier(
    sessions: async_sessionmaker[AsyncSession], pages: dict[str, str]
) -> InvestigationEngine:
    """The real engine, reading Tron through this tier and nothing else.

    Which is the state the tier exists for: every keyed provider spent or
    throttled, and the floor answering.
    """
    clock = Clock()
    provider = build(router(pages), clock=clock, site_pages_per_call=PAGES_PER_CALL)
    pool = ProviderPool(clock=clock, sleep=clock.sleep)
    pool.register(provider, limits=ProviderLimits(rate_per_sec=100, burst=100), priority=95)
    registry = ChainRegistry()
    registry.register(TronAdapter(pool))
    return InvestigationEngine(registry, sessions, NullAttributor())


async def run_trace(sessions: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    engine = engine_over_the_fetch_tier(sessions, busy_address_pages())
    investigation_id = await engine.start(
        "tron", ADDRESS, [Objective.FIND_NEXT_VASP], Budgets(max_depth=2)
    )
    assert await engine.run(investigation_id) == "completed"
    return investigation_id


async def test_a_read_the_page_bound_cut_short_reaches_the_report_as_one(
    session: AsyncSession, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """The engine-visible outcome, which is the only one a reader ever sees.

    The provider knew it had stopped early — it wrote ``truncated_at_page_limit``
    into its manifest and logged a warning — and until the short read travelled
    in the ENVELOPE that knowledge reached nothing above ``explorer_fetch.py``.
    ``count_truncated_histories`` is the counter the report's coverage section
    prints from, so this is the sentence "N address(es) had more history than was
    read" being true.
    """
    investigation_id = await run_trace(sessions)

    investigations = InvestigationRepository(session)
    assert await investigations.count_truncated_histories(investigation_id) == 1

    nodes = {node.address: node for node in await investigations.graph_nodes(investigation_id)}
    assert nodes[ADDRESS].history_truncated is True
    # The rows that WERE read are still there: this is a partial read reported
    # as partial, not a read abandoned. An answer that marked coverage honestly
    # by returning nothing would be no better than the silent truncation.
    assert nodes[ADDRESS].hop_distance == 0


async def test_an_address_read_to_the_end_is_not_marked(
    session: AsyncSession, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """The counter has to stay worth something.

    A tier that marked every address it touched would make "N address(es) had
    more history than was read" a constant, and a caveat that is always printed
    is one no reader weighs. The counterparty at hop 1 has one page of history
    and the site says so, so it is read in full and must come back unmarked —
    from the same run, the same provider and the same bound as the root above.
    """
    investigation_id = await run_trace(sessions)

    investigations = InvestigationRepository(session)
    nodes = {node.address: node for node in await investigations.graph_nodes(investigation_id)}
    assert COUNTERPARTY in nodes, "the trace never reached the address this test is about"
    assert nodes[COUNTERPARTY].history_truncated is False


async def test_the_short_read_is_printed_in_the_report_a_reader_is_handed(
    session: AsyncSession, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """The column is the mechanism; the sentence is the product.

    The test above stops at ``nodes.history_truncated`` on the strength of this
    module's own claim that the column is "what the report's Coverage and
    caveats section ... [is] rendered from". That claim was true of a chain of
    four modules nothing exercised end to end — ``collect_coverage`` counts the
    rows, ``TraversalCoverage.complete`` decides whether the run may call itself
    whole, ``_coverage_caveats`` writes the caveat, ``render_html`` prints the
    figure — and each of them is owned somewhere else. Assert the rendered
    document, so a break anywhere along it fails here rather than downgrading a
    caveat to silence in a document a reader is about to act on.
    """
    investigation_id = await run_trace(sessions)
    report = await collect_report(session, investigation_id)

    assert report.coverage.truncated_histories == 1
    # The guard that stops any summary calling this run whole.
    assert report.coverage.complete is False
    assert "truncated_history" in {caveat.code for caveat in report.caveats}
    # The figure itself, in the document — not the model that feeds it.
    assert "<dt>Histories read only in part</dt><dd>1</dd>" in render_html(report)

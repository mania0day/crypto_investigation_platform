"""Engine end-to-end on the synthetic known-answer chain, real Postgres.

Ground truth:  exchange_in → funder → ROOT → cashout → exchange_out
"""

import pytest
from sqlalchemy import update

from cipherchain.chains.base import ChainRegistry
from cipherchain.core.models import EvidenceKind, FindingKind
from cipherchain.investigation import Budgets, InvestigationEngine, NullAttributor, Objective
from cipherchain.reporting import collect_coverage
from cipherchain.storage.repositories import InvestigationRepository
from cipherchain.storage.tables import InvestigationRow
from tests.investigation.conftest import (
    CHAIN,
    EXCHANGE_IN,
    EXCHANGE_OUT,
    FUNDER,
    ROOT,
    FakeAdapter,
    FakeClock,
    Hop,
    MapAttributor,
)

BOTH = (Objective.FIND_PREV_VASP, Objective.FIND_NEXT_VASP)
VASP_LABELS = {
    EXCHANGE_IN: ("TestExchange In", "vasp"),
    EXCHANGE_OUT: ("TestExchange Out", "vasp"),
}


def make_engine(registry, sessions, attributor, **kwargs) -> InvestigationEngine:
    return InvestigationEngine(registry, sessions, attributor, **kwargs)


async def test_answers_the_core_query_in_both_directions(registry, sessions) -> None:
    reg, adapter = registry
    engine = make_engine(reg, sessions, MapAttributor(VASP_LABELS))
    investigation_id = await engine.start(CHAIN, ROOT, BOTH)

    assert await engine.run(investigation_id) == "completed"

    async with sessions() as session:
        findings = await InvestigationRepository(session).list_findings(investigation_id)
    vasp = {f.direction: f for f in findings if f.kind is FindingKind.VASP_ENDPOINT}
    assert set(vasp) == {"backward", "forward"}  # both objectives answered

    backward = vasp["backward"]
    assert backward.subject.value == EXCHANGE_IN
    assert "previous VASP" in backward.summary
    fact = next(e for e in backward.evidence if e.kind.value == "onchain_fact")
    assert set(fact.refs) == {"tx_ex_in", "tx_fund"}  # the funding path, evidenced

    forward = vasp["forward"]
    assert forward.subject.value == EXCHANGE_OUT
    assert set(next(e for e in forward.evidence if e.kind.value == "onchain_fact").refs) == {
        "tx_out",
        "tx_ex_out",
    }

    claim = next(e for e in backward.evidence if e.kind.value == "third_party_claim")
    assert claim.source == "test-labels@2026-08-07"  # attribution is a sourced claim
    # attribute-first: the VASP endpoints were never expanded (zero API cost)
    assert EXCHANGE_IN not in adapter.history_calls
    assert EXCHANGE_OUT not in adapter.history_calls


async def test_no_labels_yields_honest_terminals(registry, sessions) -> None:
    reg, _ = registry
    engine = make_engine(reg, sessions, NullAttributor())
    investigation_id = await engine.start(CHAIN, ROOT, BOTH)

    assert await engine.run(investigation_id) == "completed"

    async with sessions() as session:
        findings = await InvestigationRepository(session).list_findings(investigation_id)
    terminals = [f for f in findings if f.kind is FindingKind.TERMINAL]
    assert {f.direction for f in terminals} == {"backward", "forward"}
    assert all("without an attributed endpoint" in f.summary for f in terminals)
    assert not [f for f in findings if f.kind is FindingKind.VASP_ENDPOINT]


async def test_a_labelled_address_is_attributed_the_moment_it_is_discovered(
    sessions,
) -> None:
    """A label CipherChain already holds must never be discarded with the budget.

    Attribution is a dict lookup with no provider call and no budget, but it
    used to be reachable only from ``_process_node`` — after a node was claimed
    and expanded at a cost of three provider calls. So every labelled address
    still sitting on the frontier when the budget died was thrown away unread
    along with the genuinely unexplored work. Measured on live traces, runs
    reported 58-62% behavioural guesses as their answer while holding unread
    0.9-confidence Binance, Coinbase, KuCoin and OKX labels for addresses they
    had already found (docs/research/ATTRIBUTION_AT_DISCOVERY.md).

    The ledger here reproduces that exactly: the exchange funds the root
    directly, but with the SMALLEST amount, so nearest-first ranks it last and
    a one-call budget can never claim it.
    """
    ledger = (
        Hop("decoy_a", ROOT, 9_000, 1, "tx_a"),
        Hop("decoy_b", ROOT, 8_000, 1, "tx_b"),
        Hop("decoy_c", ROOT, 7_000, 1, "tx_c"),
        Hop(EXCHANGE_IN, ROOT, 1, 1, "tx_exchange"),  # ranks LAST
    )
    adapter = FakeAdapter(ledger=ledger)
    reg = ChainRegistry()
    reg.register(adapter)
    engine = make_engine(reg, sessions, MapAttributor(VASP_LABELS))
    investigation_id = await engine.start(
        CHAIN,
        ROOT,
        (Objective.FIND_PREV_VASP,),
        # One call: enough to expand the root and nothing else.
        Budgets(api_calls=1, seconds=300, max_depth=6, max_nodes=500),
    )

    assert await engine.run(investigation_id) == "partial"

    async with sessions() as session:
        findings = await InvestigationRepository(session).list_findings(investigation_id)

    vasp = [f for f in findings if f.kind is FindingKind.VASP_ENDPOINT]
    assert len(vasp) == 1, "the labelled funder must be named despite never being claimed"
    assert vasp[0].subject.value == EXCHANGE_IN
    assert vasp[0].direction == "backward"
    assert vasp[0].confidence == 0.9, "a sourced label, not a behavioural guess"
    # Same evidence shape as an attribution reached the old way — the taxonomy
    # does not change just because the answer arrived earlier.
    kinds = {e.kind for e in vasp[0].evidence}
    assert EvidenceKind.THIRD_PARTY_CLAIM in kinds
    assert EvidenceKind.ONCHAIN_FACT in kinds
    path = next(e for e in vasp[0].evidence if e.kind is EvidenceKind.ONCHAIN_FACT)
    assert "tx_exchange" in path.refs, "the value path must survive discovery-time attribution"

    # The point of the whole fix: this cost nothing.
    assert adapter.history_calls == [ROOT], "the exchange's history was never fetched"

    # And because the objective is genuinely answered, the run must NOT also
    # claim it exhausted itself without finding an endpoint.
    exhausted = [
        f
        for f in findings
        if f.kind is FindingKind.TERMINAL and "without an attributed endpoint" in f.summary
    ]
    assert not exhausted


async def test_discovery_attribution_does_not_double_record(sessions) -> None:
    """Attributing at discovery must not file the same claim twice.

    A labelled node is closed the moment it is named, so it can never be
    claimed and re-attributed by ``_process_node``. With budget to spare, the
    default ledger's two exchanges must yield exactly one finding each.
    """
    adapter = FakeAdapter()
    reg = ChainRegistry()
    reg.register(adapter)
    engine = make_engine(reg, sessions, MapAttributor(VASP_LABELS))
    investigation_id = await engine.start(CHAIN, ROOT, BOTH, Budgets(api_calls=50))

    assert await engine.run(investigation_id) == "completed"

    async with sessions() as session:
        findings = await InvestigationRepository(session).list_findings(investigation_id)
    subjects = [f.subject.value for f in findings if f.kind is FindingKind.VASP_ENDPOINT]
    assert sorted(subjects) == [EXCHANGE_IN, EXCHANGE_OUT]
    # Never expanded either exchange: naming one closes its branch.
    assert EXCHANGE_IN not in adapter.history_calls
    assert EXCHANGE_OUT not in adapter.history_calls


async def test_budget_exhaustion_is_partial_then_resumable(registry, sessions) -> None:
    reg, adapter = registry
    engine = make_engine(reg, sessions, MapAttributor(VASP_LABELS))
    investigation_id = await engine.start(
        CHAIN, ROOT, BOTH, Budgets(api_calls=1, seconds=300, max_depth=6, max_nodes=500)
    )

    assert await engine.run(investigation_id) == "partial"
    async with sessions() as session:
        repo = InvestigationRepository(session)
        row = await repo.get(investigation_id)
        assert row is not None and row.status == "partial"
        findings = await repo.list_findings(investigation_id)
        budget_terminal = next(f for f in findings if f.kind is FindingKind.TERMINAL)
        assert "budget 'api_calls' exhausted" in budget_terminal.summary
        assert "unexplored" in budget_terminal.summary  # gaps are explicit

        # raise the budget and resume the SAME investigation
        await session.execute(
            update(InvestigationRow)
            .where(InvestigationRow.id == investigation_id)
            .values(budgets=Budgets(api_calls=50).to_dict())
        )
        await session.commit()

    calls_before_resume = list(adapter.history_calls)
    assert await engine.run(investigation_id) == "completed"

    async with sessions() as session:
        findings = await InvestigationRepository(session).list_findings(investigation_id)
    assert {f.direction for f in findings if f.kind is FindingKind.VASP_ENDPOINT} == {
        "backward",
        "forward",
    }
    # resume never refetched already-expanded nodes
    resumed_calls = adapter.history_calls[len(calls_before_resume) :]
    assert not (set(resumed_calls) & set(calls_before_resume))


async def test_a_service_endpoint_marks_the_node_and_keeps_going(sessions) -> None:
    """An unnamed inference is a signpost, not a destination.

    Stopping at a suspected service endpoint ended the branch on a guess — and
    the NAMED exchange one hop past it was never reached, so a report that
    could have said "Binance" said "custodial infrastructure, operator
    unnamed" instead. Ruling 2026-08-13: mark it, keep tracing.
    """
    from cipherchain.analysis.heuristics import detect_service_endpoint

    hub = "hub"
    ledger = tuple(
        [Hop(f"payer_{i}", hub, 100 + i, 1, f"tx_in_{i}") for i in range(30)]
        + [Hop(hub, f"payee_{i}", 50 + i, 2, f"tx_out_{i}") for i in range(29)]
        + [Hop(hub, EXCHANGE_OUT, 900, 2, "tx_to_exchange")]
        + [Hop(ROOT, hub, 500, 1, "tx_root")]
    )
    adapter = FakeAdapter(ledger=ledger)
    reg = ChainRegistry()
    reg.register(adapter)
    engine = make_engine(
        reg,
        sessions,
        MapAttributor(VASP_LABELS),
        service_detector=detect_service_endpoint,
        supernode_threshold=50,
    )
    investigation_id = await engine.start(CHAIN, ROOT, (Objective.FIND_NEXT_VASP,))
    await engine.run(investigation_id)

    async with sessions() as session:
        findings = await InvestigationRepository(session).list_findings(investigation_id)

    inferred = [
        f
        for f in findings
        if f.kind is FindingKind.VASP_ENDPOINT
        and not any(e.kind is EvidenceKind.THIRD_PARTY_CLAIM for e in f.evidence)
    ]
    assert inferred, "the hub should still be flagged as a suspected service endpoint"

    # The point of the ruling: the trace went THROUGH it and named someone.
    named = [
        f
        for f in findings
        if f.kind is FindingKind.VASP_ENDPOINT
        and any(e.kind is EvidenceKind.THIRD_PARTY_CLAIM for e in f.evidence)
    ]
    assert named, "the named exchange beyond the hub must still be reached"
    assert any(f.subject.value == EXCHANGE_OUT for f in named)


async def test_supernode_expansion_is_capped_not_refused(registry, sessions) -> None:
    """A hub follows its largest flows and states what it left.

    Refusing a high-degree address outright also refused the largest flow out
    of it, which is usually the one worth following (ruling 2026-08-13). The
    cap keeps the frontier bounded; the finding keeps the omission visible.
    """
    fan_ledger = tuple(
        [Hop(FUNDER, ROOT, 900, 2, "tx_fund")]
        + [Hop(f"payer_{i}", FUNDER, 10 + i, 1, f"tx_fan_{i}") for i in range(60)]
    )
    adapter = FakeAdapter(ledger=fan_ledger)
    from cipherchain.chains.base import ChainRegistry

    reg = ChainRegistry()
    reg.register(adapter)
    engine = make_engine(
        reg, sessions, NullAttributor(), supernode_threshold=50, supernode_follow=20
    )
    investigation_id = await engine.start(CHAIN, ROOT, (Objective.FIND_PREV_VASP,))

    assert await engine.run(investigation_id) == "completed"
    async with sessions() as session:
        findings = await InvestigationRepository(session).list_findings(investigation_id)

    supernode = next(f for f in findings if "high-degree address" in f.summary)
    assert supernode.subject.value == FUNDER
    assert "60 counterparties" in supernode.summary
    assert "followed the 20 largest by value" in supernode.summary
    assert "40 branch(es) not followed" in supernode.summary

    # What it skipped is a statement about this run, not about the chain.
    assert any(e.kind is EvidenceKind.ENGINE_OBSERVATION for e in supernode.evidence)

    expanded = [c for c in adapter.history_calls if c.startswith("payer_")]
    assert len(expanded) == 20, "the cap must bound the frontier"
    # Ranked by value, so the cap keeps the MONEY: payers 40..59 pay the most.
    assert set(expanded) == {f"payer_{i}" for i in range(40, 60)}


async def test_depth_horizon_prevents_deep_expansion(registry, sessions) -> None:
    reg, adapter = registry
    engine = make_engine(reg, sessions, MapAttributor(VASP_LABELS))
    investigation_id = await engine.start(
        CHAIN, ROOT, (Objective.FIND_PREV_VASP,), Budgets(max_depth=1)
    )

    assert await engine.run(investigation_id) == "completed"
    async with sessions() as session:
        findings = await InvestigationRepository(session).list_findings(investigation_id)
    # exchange_in sits at hop 2 — beyond the horizon, so no VASP finding
    assert not [f for f in findings if f.kind is FindingKind.VASP_ENDPOINT]
    terminal = next(f for f in findings if f.kind is FindingKind.TERMINAL)
    # The coverage gap is stated as a fact about the run, and the summary must
    # not claim the trace saw everything it could reach (Ruling 4).
    assert "did not read everything it could reach" in terminal.summary
    coverage = next(
        e
        for e in terminal.evidence
        if e.kind is EvidenceKind.ENGINE_OBSERVATION and "beyond" in e.summary
    )
    assert "beyond the depth horizon" in coverage.summary
    assert coverage.confidence is None  # the engine does not guess about its own run
    assert EXCHANGE_IN not in adapter.history_calls


async def test_terminal_findings_never_wear_the_onchain_fact_stamp(registry, sessions) -> None:
    """Statements about CipherChain's own run are not verifiable against the chain.

    Before Ruling 4 the "frontier ran dry" evidence was filed as ONCHAIN_FACT
    with an ADDRESS as its ref — a tool observation wearing the stamp reserved
    for things anyone can check on-chain.
    """
    reg, _ = registry
    engine = make_engine(reg, sessions, MapAttributor({}))  # no labels at all
    investigation_id = await engine.start(CHAIN, ROOT, (Objective.FIND_PREV_VASP,))
    await engine.run(investigation_id)

    async with sessions() as session:
        findings = await InvestigationRepository(session).list_findings(investigation_id)

    terminals = [f for f in findings if f.kind is FindingKind.TERMINAL]
    assert terminals, "an exhausted trace must still produce an explicit answer"
    for finding in terminals:
        for item in finding.evidence:
            assert item.kind is not EvidenceKind.ONCHAIN_FACT, (
                f"tool-state evidence must not claim to be an on-chain fact: {item.summary}"
            )
        assert any(e.kind is EvidenceKind.ENGINE_OBSERVATION for e in finding.evidence)


async def test_sanctioned_address_recorded_and_trace_continues(registry, sessions) -> None:
    reg, _ = registry
    labels = dict(VASP_LABELS) | {FUNDER: ("OFAC Listed Entity", "sanctioned")}
    engine = make_engine(reg, sessions, MapAttributor(labels))
    investigation_id = await engine.start(CHAIN, ROOT, (Objective.FIND_PREV_VASP,))

    assert await engine.run(investigation_id) == "completed"
    async with sessions() as session:
        findings = await InvestigationRepository(session).list_findings(investigation_id)
    sanctioned = next(f for f in findings if f.kind is FindingKind.SANCTIONED_ADDRESS)
    assert sanctioned.subject.value == FUNDER
    assert "trace continued" in sanctioned.summary
    # ruling R2: the trace pushed THROUGH the sanctioned hop to the VASP
    vasp = next(f for f in findings if f.kind is FindingKind.VASP_ENDPOINT)
    assert vasp.subject.value == EXCHANGE_IN


async def test_seconds_budget_stops_a_long_running_trace(registry, sessions) -> None:
    reg, adapter = registry
    clock = FakeClock(step=6.0)  # each clock reading burns 6 simulated seconds
    engine = make_engine(reg, sessions, NullAttributor(), clock=clock)
    investigation_id = await engine.start(CHAIN, ROOT, BOTH, Budgets(seconds=10))

    assert await engine.run(investigation_id) == "partial"
    async with sessions() as session:
        findings = await InvestigationRepository(session).list_findings(investigation_id)
    assert "budget 'seconds' exhausted" in findings[0].summary
    assert adapter.history_calls == [ROOT]  # stopped after the first expansion


async def test_adapter_failure_marks_investigation_failed(registry, sessions) -> None:
    class ExplodingAdapter(FakeAdapter):
        async def address_history(self, address, **kwargs):  # type: ignore[override]
            raise RuntimeError("provider meltdown")

    from cipherchain.chains.base import ChainRegistry

    reg = ChainRegistry()
    reg.register(ExplodingAdapter())
    engine = make_engine(reg, sessions, NullAttributor())
    investigation_id = await engine.start(CHAIN, ROOT, BOTH)

    with pytest.raises(RuntimeError, match="meltdown"):
        await engine.run(investigation_id)
    async with sessions() as session:
        row = await InvestigationRepository(session).get(investigation_id)
    assert row is not None and row.status == "failed"
    assert row.error is not None and "meltdown" in row.error


async def test_a_feed_no_provider_could_serve_is_a_coverage_gap(sessions) -> None:
    """Losing an acquisition feed must cost coverage, not pass unnoticed.

    ``HistoryPage.gaps`` exists because a missing transfer cannot be inferred
    from the rows that did arrive — nothing downstream can see what was never
    fetched. The engine dropped it: a run that lost ``tokentx`` on every
    address reported "no address was left partially read", which is the exact
    sentence an address funded entirely in USDT would be misread by.

    Recorded as a partially-read address, because that is what it is — the
    same durable counter a cut page sets, so the API's ``complete`` flag and
    the report's coverage section both state it without either having to learn
    a new concept.
    """
    from cipherchain.chains.base import Capability, FeedGap, HistoryPage

    class HalfBlindAdapter(FakeAdapter):
        async def address_history(self, address, **kwargs):  # type: ignore[override]
            page = await super().address_history(address, **kwargs)
            return HistoryPage(
                items=page.items,
                next_cursor=page.next_cursor,
                gaps=(
                    FeedGap(
                        chain=self.chain,
                        capability=Capability.TOKEN_TRANSFERS,
                        detail="all providers failed: quota exhausted",
                    ),
                ),
            )

    reg = ChainRegistry()
    reg.register(HalfBlindAdapter())
    engine = make_engine(reg, sessions, NullAttributor())
    investigation_id = await engine.start(CHAIN, ROOT, (Objective.FIND_NEXT_VASP,))

    assert await engine.run(investigation_id) == "completed"
    async with sessions() as session:
        investigations = InvestigationRepository(session)
        row = await investigations.get(investigation_id)
        assert row is not None
        coverage = await collect_coverage(investigations, row)

    assert coverage.truncated_histories == coverage.addresses_reached
    assert coverage.complete is False, "a run with an unread feed claimed complete coverage"


async def test_a_short_read_with_no_cursor_to_offer_is_still_a_cut_history(sessions) -> None:
    """The third way a page can be short, and the only one with nothing to show
    for it.

    A cursor says "there is more, resume here"; a gap says "a feed never
    answered". A provider that reads a fixed number of numbered pages and stops
    can offer neither — the keyless explorer tier on Tron, whose caller pages by
    TronGrid fingerprint. The engine tested only ``next_cursor`` and ``gaps``,
    so such an address arrived in the report as one whose history simply ends,
    and "N address(es) had more history than was read" left it out. The same
    address served by TronGrid WAS counted, which made the document's honesty a
    function of which provider happened to be up.
    """
    from cipherchain.chains.base import HistoryPage

    class ShortReadAdapter(FakeAdapter):
        async def address_history(self, address, **kwargs):  # type: ignore[override]
            page = await super().address_history(address, **kwargs)
            return HistoryPage(items=page.items, next_cursor=None, gaps=(), truncated=True)

    reg = ChainRegistry()
    reg.register(ShortReadAdapter())
    engine = make_engine(reg, sessions, NullAttributor())
    investigation_id = await engine.start(CHAIN, ROOT, (Objective.FIND_NEXT_VASP,))

    assert await engine.run(investigation_id) == "completed"
    async with sessions() as session:
        investigations = InvestigationRepository(session)
        row = await investigations.get(investigation_id)
        assert row is not None
        coverage = await collect_coverage(investigations, row)

    assert coverage.truncated_histories == coverage.addresses_reached
    assert coverage.complete is False, "a cut read with no cursor claimed complete coverage"


async def test_a_page_that_says_nothing_is_wrong_is_taken_at_its_word(sessions) -> None:
    """The other direction of the same flag: ``truncated`` defaults False, and
    every adapter that never sets it must still report a whole read. A default
    of True would mark every address on every chain as partly read, which is the
    same lie pointing the other way."""
    reg = ChainRegistry()
    reg.register(FakeAdapter())
    engine = make_engine(reg, sessions, NullAttributor())
    investigation_id = await engine.start(CHAIN, ROOT, (Objective.FIND_NEXT_VASP,))

    assert await engine.run(investigation_id) == "completed"
    async with sessions() as session:
        investigations = InvestigationRepository(session)
        row = await investigations.get(investigation_id)
        assert row is not None
        coverage = await collect_coverage(investigations, row)

    assert coverage.truncated_histories == 0


async def test_a_full_expansion_query_is_a_coverage_gap(sessions) -> None:
    """The 500-row expansion query cuts counterparties, and must say so.

    Movements accumulate in the fact store across pages and across runs, so an
    address can hold more of them than one expansion query returns. Anything
    past the cut is not ranked and not dropped — it is absent, and the
    supernode guard cannot count it either, because it never becomes a
    counterparty. Here twenty payers sit entirely in the tail: without the
    flag the run reports complete coverage over twenty addresses it never saw.
    """
    recent = [
        Hop(f"recent{p:03d}", ROOT, 1_000 + n, 50 + n, f"tx_r_{p:03d}_{n:02d}")
        for p in range(20)
        for n in range(25)
    ]
    older = [Hop(f"old{u:03d}", ROOT, 9_000_000, 1, f"tx_o_{u:03d}") for u in range(20)]

    reg = ChainRegistry()
    reg.register(FakeAdapter(ledger=tuple(recent + older)))
    engine = make_engine(reg, sessions, NullAttributor(), page_limit=2000)
    investigation_id = await engine.start(
        CHAIN, ROOT, (Objective.FIND_PREV_VASP,), Budgets(api_calls=100, max_nodes=500)
    )

    assert await engine.run(investigation_id) == "completed"
    async with sessions() as session:
        investigations = InvestigationRepository(session)
        row = await investigations.get(investigation_id)
        assert row is not None
        coverage = await collect_coverage(investigations, row)
        reached = {n.address for n in await investigations.graph_nodes(investigation_id, limit=999)}

    assert not (reached & {f"old{u:03d}" for u in range(20)}), (
        "the probe needs the tail to be genuinely cut"
    )
    assert coverage.truncated_histories >= 1
    assert coverage.complete is False, "counterparties were cut and coverage said complete"

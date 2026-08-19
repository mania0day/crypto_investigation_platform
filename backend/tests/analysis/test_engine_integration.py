"""Analysis wired into the engine: real OFAC data + sweep detection drive
findings on the synthetic chain, against real Postgres."""

from datetime import UTC, datetime

from cipherchain.analysis.attribution.labels import LabelPack, LabelRecord
from cipherchain.analysis.attribution.store import LabelStoreAttributor
from cipherchain.analysis.heuristics import detect_sweeps
from cipherchain.analysis.sanctions import OfacSanctionsSource
from cipherchain.chains.base import ChainRegistry
from cipherchain.core.models import FindingKind
from cipherchain.investigation import Budgets, InvestigationEngine, Objective
from cipherchain.storage.repositories import InvestigationRepository
from tests.investigation.conftest import CHAIN, EXCHANGE_IN, ROOT, FakeAdapter, Hop

GENESIS = datetime(2026, 1, 1, tzinfo=UTC)


def labelpack(*records: LabelRecord) -> LabelPack:
    return LabelPack(name="test-vasps", labels=records)


async def test_real_ofac_source_flags_sanctioned_hop(sessions) -> None:
    """A funder address that is actually on the OFAC list is flagged, and
    the trace still reaches the VASP beyond it (ruling R2)."""
    ofac = OfacSanctionsSource()
    # Build a ledger whose funding hop is a genuinely-listed OFAC address.
    listed = next(r for r in ofac.records() if r.chain == "ethereum").address.lower()

    ledger = (
        Hop(EXCHANGE_IN, listed, 1_000, 1, "tx_in"),
        Hop(listed, ROOT, 900, 2, "tx_fund"),
    )
    adapter = FakeAdapter(chain="ethereum", ledger=ledger)
    reg = ChainRegistry()
    reg.register(adapter)

    attributor = LabelStoreAttributor(
        [
            ofac,
            labelpack(
                LabelRecord(
                    chain="ethereum",
                    address=EXCHANGE_IN,
                    entity="Test Exchange",
                    category="vasp",
                    source="test-vasps@2026-08-07",
                    confidence=0.9,
                )
            ),
        ]
    )
    engine = InvestigationEngine(reg, sessions, attributor)
    investigation_id = await engine.start("ethereum", ROOT, (Objective.FIND_PREV_VASP,))
    assert await engine.run(investigation_id) == "completed"

    async with sessions() as session:
        findings = await InvestigationRepository(session).list_findings(investigation_id)
    sanctioned = next(f for f in findings if f.kind is FindingKind.SANCTIONED_ADDRESS)
    assert sanctioned.subject.value == listed
    assert sanctioned.evidence[0].source.startswith("ofac-sdn")
    # trace continued through to the VASP
    assert any(f.kind is FindingKind.VASP_ENDPOINT for f in findings)


async def test_sweep_detector_fires_during_investigation(sessions) -> None:
    """ROOT receives 1000 and forwards 950 next day → a sweep finding, with
    the VASP still found downstream."""
    attributor = LabelStoreAttributor(
        [
            labelpack(
                LabelRecord(
                    chain=CHAIN,
                    address="cashout_vasp",
                    entity="Exit Exchange",
                    category="vasp",
                    source="test-vasps@2026-08-07",
                    confidence=0.9,
                )
            )
        ]
    )
    ledger = (
        Hop("upstream", ROOT, 1_000, 1, "tx_recv"),
        Hop(ROOT, "cashout_vasp", 950, 2, "tx_fwd"),  # 95% next day = sweep
    )
    reg = ChainRegistry()
    reg.register(FakeAdapter(ledger=ledger))
    engine = InvestigationEngine(reg, sessions, attributor, detectors=(detect_sweeps,))
    investigation_id = await engine.start(CHAIN, ROOT, (Objective.FIND_NEXT_VASP,))
    assert await engine.run(investigation_id) == "completed"

    async with sessions() as session:
        findings = await InvestigationRepository(session).list_findings(investigation_id)
    sweep = next((f for f in findings if f.kind is FindingKind.SWEEP_PATTERN), None)
    assert sweep is not None
    assert sweep.subject.value == ROOT
    assert "pass-through" in sweep.summary
    inference = next(e for e in sweep.evidence if e.kind.value == "heuristic_inference")
    assert inference.heuristic == "sweep@1"  # versioned, per taxonomy
    assert any(f.kind is FindingKind.VASP_ENDPOINT for f in findings)


async def test_no_detectors_means_no_sweep_findings(sessions) -> None:
    from cipherchain.investigation import NullAttributor

    ledger = (
        Hop("upstream", ROOT, 1_000, 1, "tx_recv"),
        Hop(ROOT, "downstream", 950, 2, "tx_fwd"),
    )
    reg = ChainRegistry()
    reg.register(FakeAdapter(ledger=ledger))
    engine = InvestigationEngine(reg, sessions, NullAttributor(), detectors=())
    investigation_id = await engine.start(
        CHAIN, ROOT, (Objective.FIND_NEXT_VASP,), Budgets(max_depth=2)
    )
    assert await engine.run(investigation_id) == "completed"
    async with sessions() as session:
        findings = await InvestigationRepository(session).list_findings(investigation_id)
    assert not [f for f in findings if f.kind is FindingKind.SWEEP_PATTERN]


async def test_mixer_contact_is_recorded_and_the_branch_continues_marked(sessions) -> None:
    """Mixer contact is recorded with its source, and the branch goes on — marked.

    This test used to assert the opposite, and the reversal is a ruling rather
    than a refactor: *"i dont want to stop at mixer … go forward until VASP"*,
    with the cost accepted out loud — *"say weak decision like because of mixer
    and stuff but i need VASP"*. Stopping produced a report whose entire answer
    was "the trail ends at Tornado Cash", which the body reading it cannot act
    on.

    What did NOT change is the part that made stopping defensible: nothing past
    the pool may look traced. The address beyond it is followed as a
    ``speculative`` node naming the heuristic that guessed it, so the graph
    still distinguishes the two kinds of branch — it just no longer throws the
    second one away.
    """
    from cipherchain.chains.base import ChainRegistry
    from cipherchain.investigation import CATEGORY_MIXER

    ledger = (
        Hop("upstream", ROOT, 1_000, 1, "tx_in"),
        Hop(ROOT, "mixer_pool", 950, 2, "tx_mix"),
        Hop("mixer_pool", "beyond", 900, 3, "tx_out"),  # followed, as a marked guess
    )
    adapter = FakeAdapter(ledger=ledger)
    reg = ChainRegistry()
    reg.register(adapter)
    attributor = LabelStoreAttributor(
        [
            labelpack(
                LabelRecord(
                    chain=CHAIN,
                    address="mixer_pool",
                    entity="Tornado Cash — 1 ETH pool",
                    category=CATEGORY_MIXER,
                    source="ofac+official@2026-08-07",
                    confidence=0.9,
                )
            )
        ]
    )
    engine = InvestigationEngine(reg, sessions, attributor)
    investigation_id = await engine.start(CHAIN, ROOT, (Objective.FIND_NEXT_VASP,))
    assert await engine.run(investigation_id) == "completed"

    async with sessions() as session:
        repo = InvestigationRepository(session)
        findings = await repo.list_findings(investigation_id)
        nodes = {n.address: n for n in await repo.graph_nodes(investigation_id)}
    mixer = next(f for f in findings if f.kind is FindingKind.MIXER_INTERACTION)
    assert mixer.subject.value == "mixer_pool"
    assert "severed by design" in mixer.summary
    assert mixer.evidence[0].source == "ofac+official@2026-08-07"
    # The branch continued — and everything it reached says it is a guess.
    assert "beyond" in adapter.history_calls
    assert nodes["beyond"].speculative is True
    assert nodes["beyond"].speculative_basis is not None
    assert nodes["mixer_pool"].speculative is False


async def test_one_exchange_answers_both_objectives(sessions) -> None:
    """An address reached BOTH backward and forward must answer both objectives.

    Withdraw-then-redeposit is an everyday shape: the same exchange funded the
    subject and later received from it. Before direction entered node identity
    the exchange held a single node, so whichever objective claimed it first got
    the answer and the other was told "trace exhausted" — about an exchange the
    engine had already stored, attributed, and printed (REVIEW_FINDINGS.md #4).
    """
    from cipherchain.core.models import Direction
    from cipherchain.investigation import CATEGORY_VASP

    exchange = "shared_exchange"
    ledger = (
        Hop(exchange, ROOT, 1_000, 1, "tx_withdraw"),  # funded BY the exchange
        Hop(ROOT, exchange, 900, 2, "tx_redeposit"),  # cashed out TO the exchange
    )
    adapter = FakeAdapter(ledger=ledger)
    reg = ChainRegistry()
    reg.register(adapter)
    attributor = LabelStoreAttributor(
        [
            labelpack(
                LabelRecord(
                    chain=CHAIN,
                    address=exchange,
                    entity="Shared Exchange",
                    category=CATEGORY_VASP,
                    source="test-pack@2026-08-09",
                    confidence=0.9,
                )
            )
        ]
    )
    engine = InvestigationEngine(reg, sessions, attributor)
    investigation_id = await engine.start(
        CHAIN, ROOT, (Objective.FIND_PREV_VASP, Objective.FIND_NEXT_VASP)
    )
    assert await engine.run(investigation_id) == "completed"

    async with sessions() as session:
        findings = await InvestigationRepository(session).list_findings(investigation_id)

    vasp = [f for f in findings if f.kind is FindingKind.VASP_ENDPOINT]
    directions = {f.direction for f in vasp}
    assert directions == {Direction.BACKWARD, Direction.FORWARD}, (
        f"both objectives must be answered; got {sorted(str(d) for d in directions)}"
    )
    assert all(f.subject.value == exchange for f in vasp)

    # And neither objective may ALSO claim the trail ran dry.
    terminal_directions = {
        f.direction for f in findings if f.kind is FindingKind.TERMINAL and f.direction
    }
    assert not (terminal_directions & directions), (
        "an objective that found its VASP must not also report 'trace exhausted'"
    )


async def test_behavioural_findings_are_not_filed_twice_for_one_address(sessions) -> None:
    """Two nodes for one address must not double-file its movement patterns.

    Direction is part of node identity, so a both-ways address is processed
    once per objective. Its sweep/obfuscation patterns are the same both times;
    filing them twice would read as independent corroboration.
    """
    hub = "hub"
    ledger = (
        Hop("origin", ROOT, 1_000, 1, "tx_a"),
        Hop(ROOT, hub, 900, 2, "tx_b"),
        Hop(hub, ROOT, 100, 3, "tx_c"),  # hub is reachable both ways
        Hop(hub, "onward", 800, 4, "tx_d"),
    )
    adapter = FakeAdapter(ledger=ledger)
    reg = ChainRegistry()
    reg.register(adapter)
    engine = InvestigationEngine(
        reg, sessions, LabelStoreAttributor([]), detectors=(detect_sweeps,)
    )
    investigation_id = await engine.start(
        CHAIN, ROOT, (Objective.FIND_PREV_VASP, Objective.FIND_NEXT_VASP)
    )
    await engine.run(investigation_id)

    async with sessions() as session:
        findings = await InvestigationRepository(session).list_findings(investigation_id)

    behavioural = [
        (f.kind, f.subject.value, f.summary)
        for f in findings
        if f.kind in (FindingKind.SWEEP_PATTERN, FindingKind.OBFUSCATION_PATTERN)
    ]
    assert len(behavioural) == len(set(behavioural)), (
        f"duplicate behavioural findings: {behavioural}"
    )

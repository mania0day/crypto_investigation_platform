"""An exhausted budget is a decision, not an answer.

The regression these lock down shipped: run 1 stopped at 400 nodes with the
FORWARD objective unanswered and 382 addresses still queued, and OKX was found
only after a human issued ``POST /investigations/{id}/resume`` with a bigger
number. The instruction was given twice more — *"i dont want to stop at mixer i
want it to go foward until VASP"*, then *"report has no VASP and i told you dont
stop until VASP??"* — so the loop now buys its own allowance.

Every test here is about one of the four ways that decision can go, because the
dangerous version of this feature is the one that keeps spending: after the
questions are answered, when nothing is left to explore, past the ceiling, or on
``max_depth`` — which is not a cost at all and would change what the run MEANS.
"""

from __future__ import annotations

import pytest

from cipherchain.analysis.heuristics.service import detect_service_endpoint
from cipherchain.chains.base import ChainRegistry
from cipherchain.core.models import EvidenceKind, FindingKind
from cipherchain.investigation import Budgets, InvestigationEngine, NullAttributor, Objective
from cipherchain.investigation.budgets import BudgetExtension, BudgetTracker
from cipherchain.reporting import collect_coverage
from cipherchain.reporting.collect import collect_report
from cipherchain.storage.repositories import InvestigationRepository
from tests.investigation.conftest import (
    CASHOUT,
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
DECOY = "decoy"


def engine_for(
    sessions,
    labels: dict[str, tuple[str, str]] | None = None,
    ledger=None,
    *,
    clock=None,
    service: bool = False,
):
    adapter = FakeAdapter(ledger=ledger) if ledger is not None else FakeAdapter()
    registry = ChainRegistry()
    registry.register(adapter)
    attributor = MapAttributor(labels) if labels is not None else NullAttributor()
    kwargs: dict[str, object] = {}
    if clock is not None:
        kwargs["clock"] = clock
    if service:
        kwargs["service_detector"] = detect_service_endpoint
    return InvestigationEngine(registry, sessions, attributor, **kwargs), adapter


def fanout_ledger(levels: int = 5, fan: int = 4) -> tuple[Hop, ...]:
    """A frontier that keeps regenerating: every address spends to ``fan`` new
    addresses, outward forever, and nothing is ever labelled.

    Pursuit exists to buy more frontier, and a mixer crossing ADDS nodes — so
    the loop can feed itself. This ledger is the shape that would never stop if
    the ceiling were not honoured on every path.
    """
    hops: list[Hop] = []
    frontier = [ROOT]
    day = 1
    for level in range(levels):
        nxt: list[str] = []
        for parent in frontier:
            for i in range(fan):
                child = f"a{level}_{parent}_{i}"
                hops.append(Hop(parent, child, 1000 - i, day, f"tx_{level}_{parent}_{i}"))
                hops.append(
                    Hop(f"b{level}_{parent}_{i}", parent, 900 - i, day, f"ty_{level}_{parent}_{i}")
                )
                nxt.append(child)
                day += 1
        frontier = nxt
    return tuple(hops)


async def record(sessions, investigation_id):
    async with sessions() as session:
        return await InvestigationRepository(session).get(investigation_id)


async def findings_of(sessions, investigation_id):
    async with sessions() as session:
        return await InvestigationRepository(session).list_findings(investigation_id)


def budget_terminal(findings):
    """The terminal that names the budget — the one a reader checks for 'why did
    it stop', and the only one filed when every objective is answered."""
    return next(f for f in findings if f.kind is FindingKind.TERMINAL and "budget '" in f.summary)


#: The stop this engine filed before pursuit existed. Every pursuit-off test
#: asserts it in full and from here: the escape hatch promises this exact
#: sentence, and four hand-typed copies of it would drift apart one edit later.
OLD_STOP = (
    "budget 'api_calls' exhausted with {n} frontier address(es) unexplored — "
    "partial result, gaps explicit"
)


# ── the four decisions ───────────────────────────────────────────────────


async def test_an_unanswered_objective_with_work_left_buys_another_allowance(sessions) -> None:
    """The shipped failure, inverted: one API call, and both exchanges named.

    A single call buys exactly one expansion — the root — which reaches neither
    exchange. Before pursuit this run ended `partial` with two addresses queued
    and no endpoint, and answering it needed a human with a bigger number. The
    frontier and the objectives were both in the record the whole time.
    """
    engine, adapter = engine_for(sessions, VASP_LABELS)
    investigation_id = await engine.start(CHAIN, ROOT, BOTH, Budgets(api_calls=1))

    await engine.run(investigation_id)

    findings = await findings_of(sessions, investigation_id)
    endpoints = {
        f.direction: f.subject.value for f in findings if f.kind is FindingKind.VASP_ENDPOINT
    }
    assert endpoints == {"backward": EXCHANGE_IN, "forward": EXCHANGE_OUT}
    assert adapter.history_calls == [ROOT, FUNDER, CASHOUT], (
        "each extension bought exactly one more expansion, in frontier order"
    )
    row = await record(sessions, investigation_id)
    assert [e["budget"] for e in row.spent["budget_extensions"]] == ["api_calls", "api_calls"]
    assert [e["previous"] for e in row.spent["budget_extensions"]] == [1.0, 2.0]
    assert row.budgets["api_calls"] == 1, (
        "the record must keep saying what the OPERATOR authorised — the extensions "
        "are recorded beside the spend, not folded into the request"
    )


async def test_a_run_whose_questions_are_all_answered_stops_at_the_budget(sessions) -> None:
    """Pursuit is for open questions. Nothing else may keep it spending.

    The exchange funds the root directly and is named the moment it is
    discovered, so the objective closes on the first expansion — while a decoy
    counterparty sits unread on the frontier. That decoy is the point: there IS
    work left and there IS budget pressure, and the run must still stop, because
    the only thing that justifies more spend is an unanswered question.
    """
    ledger = (
        Hop(EXCHANGE_IN, ROOT, 1_000, 1, "tx_exchange"),
        Hop(DECOY, ROOT, 900, 1, "tx_decoy"),
    )
    engine, adapter = engine_for(sessions, VASP_LABELS, ledger)
    investigation_id = await engine.start(
        CHAIN, ROOT, (Objective.FIND_PREV_VASP,), Budgets(api_calls=1)
    )

    assert await engine.run(investigation_id) == "partial"

    assert adapter.history_calls == [ROOT], "not one extra expansion was bought"
    row = await record(sessions, investigation_id)
    assert row.spent["budget_extensions"] == []
    terminal = budget_terminal(await findings_of(sessions, investigation_id))
    assert "every objective already answered so nothing further was spent" in terminal.summary
    assert "1 frontier address(es) unexplored" in terminal.summary, (
        "the decoy was still queued: the run stopped on the answer, not on empty work"
    )


async def test_an_empty_frontier_is_not_something_a_bigger_budget_can_fix(sessions) -> None:
    """No queued work means the trail ran out, not the allowance.

    Extending here would spend a second lap of nothing and then file the same
    terminal — and the summary would say a larger budget might reach further,
    which is the one thing this state proves false.
    """
    engine, _ = engine_for(sessions, ledger=(Hop(FUNDER, ROOT, 900, 2, "tx_fund"),))
    investigation_id = await engine.start(
        CHAIN, ROOT, (Objective.FIND_PREV_VASP,), Budgets(api_calls=2)
    )

    assert await engine.run(investigation_id) == "partial"

    row = await record(sessions, investigation_id)
    assert row.spent["budget_extensions"] == []
    terminal = budget_terminal(await findings_of(sessions, investigation_id))
    assert "nothing left on the frontier for a larger budget to reach" in terminal.summary


async def test_the_ceiling_stop_says_it_tried_and_the_first_budget_stop_does_not(
    sessions,
) -> None:
    """"We tried three times and named nobody" is a finding about the chain.

    "We ran out of allowance" is a finding about the tool. They read the same to
    a caseworker unless the report is made to say which one it holds, and only
    one of them means another lap is worth paying for.
    """
    engine, _ = engine_for(sessions)  # no labels: nothing can ever be named
    pursued = await engine.start(CHAIN, ROOT, BOTH, Budgets(api_calls=1, max_extensions=2))
    assert await engine.run(pursued) == "partial"

    stopped = await engine.start(
        CHAIN, ROOT, BOTH, Budgets(api_calls=1, pursue_until_answered=False)
    )
    assert await engine.run(stopped) == "partial"

    ceiling = budget_terminal(await findings_of(sessions, pursued))
    assert "after 2 budget extension(s) chasing the unanswered objective(s)" in ceiling.summary
    assert "(api_calls 1 → 3) and no extension left" in ceiling.summary

    first = budget_terminal(await findings_of(sessions, stopped))
    assert "extension" not in first.summary, (
        "a run that never pursued must not borrow the stronger statement"
    )

    # Per objective, the same distinction — this is the sentence that reaches an
    # investigator deciding whether the money is findable at all.
    directed = [
        f
        for f in await findings_of(sessions, pursued)
        if f.kind is FindingKind.TERMINAL and f.direction is not None
    ]
    assert len(directed) == 2
    for finding in directed:
        assert "extended its budgets 2 time(s) chasing this objective and still named nobody" in (
            finding.summary
        )
        observation = next(
            e for e in finding.evidence if e.kind is EvidenceKind.ENGINE_OBSERVATION
        )
        assert "after exhausting all 2 permitted budget extension(s)" in observation.summary


# ── the boundary pursuit may not cross ───────────────────────────────────


def test_depth_is_not_a_cost_and_the_tracker_refuses_to_sell_it() -> None:
    """``max_depth`` decides what the trace MEANS — how far from the subject a
    conclusion may be drawn. Raising it mid-run would rewrite the question the
    record says was asked, and the README already tells operators that going
    deeper is a NEW investigation. Refused by name, not merely absent."""
    tracker = BudgetTracker(Budgets(api_calls=4, max_depth=2))

    with pytest.raises(ValueError, match="max_depth"):
        tracker.extend("max_depth", ["find_next_vasp"])

    assert tracker.budgets.max_depth == 2


async def test_pursuit_buys_expansions_and_never_buys_depth(sessions) -> None:
    """A run pursuing an answer still stops at the depth horizon it was given.

    With ``max_depth=1`` the exchanges are two hops out. Pursuit here extends
    ``api_calls`` until the frontier is dry, and the exchanges must STILL be
    unexplored horizon stops — the alternative is a run that quietly answers a
    question about four hops when the record says one.
    """
    engine, adapter = engine_for(sessions, VASP_LABELS)
    investigation_id = await engine.start(
        CHAIN, ROOT, BOTH, Budgets(api_calls=1, max_depth=1, max_extensions=8)
    )

    await engine.run(investigation_id)

    row = await record(sessions, investigation_id)
    assert row.spent["budget_extensions"], "the run did pursue"
    assert {e["budget"] for e in row.spent["budget_extensions"]} == {"api_calls"}
    assert row.budgets["max_depth"] == 1
    assert EXCHANGE_IN not in adapter.history_calls
    assert EXCHANGE_OUT not in adapter.history_calls
    async with sessions() as session:
        coverage = await collect_coverage(InvestigationRepository(session), row)
    assert coverage.depth_horizon_stops > 0, "the horizon still bit while the budget grew"


async def test_pursuit_off_leaves_the_old_stop_word_for_word(sessions) -> None:
    """The escape hatch has to be a true escape hatch.

    A caller who needs a predictable spend gets exactly the run this engine made
    before pursuit existed: one expansion, one terminal, that sentence.
    """
    engine, adapter = engine_for(sessions, VASP_LABELS)
    investigation_id = await engine.start(
        CHAIN, ROOT, BOTH, Budgets(api_calls=1, pursue_until_answered=False)
    )

    assert await engine.run(investigation_id) == "partial"

    assert adapter.history_calls == [ROOT]
    row = await record(sessions, investigation_id)
    assert row.spent["api_calls"] == 1
    assert row.spent["budget_extensions"] == []
    terminal = budget_terminal(await findings_of(sessions, investigation_id))
    assert terminal.summary == OLD_STOP.format(n=2)


async def test_pursuit_off_does_not_borrow_the_answered_stop(sessions) -> None:
    """Word-for-word when the last objective closes on the same lap the budget
    runs out — not only in the case the escape hatch was written against.

    ``_pursue`` used to run its three tests before consulting the flag, so a
    pursuit-off run came back holding ``objectives_answered``, and
    ``_finish_partial`` prints a distinct terminal for that. A sentence invented
    by machinery the caller had switched off then went into the document that
    goes to law enforcement.
    """
    engine, _ = engine_for(
        sessions,
        VASP_LABELS,
        (Hop(EXCHANGE_IN, ROOT, 1_000, 1, "tx_exchange"), Hop(DECOY, ROOT, 900, 1, "tx_decoy")),
    )
    investigation_id = await engine.start(
        CHAIN, ROOT, (Objective.FIND_PREV_VASP,), Budgets(api_calls=1, pursue_until_answered=False)
    )

    assert await engine.run(investigation_id) == "partial"

    terminal = budget_terminal(await findings_of(sessions, investigation_id))
    assert terminal.summary == OLD_STOP.format(n=1)


async def test_pursuit_off_does_not_borrow_the_empty_frontier_stop(sessions) -> None:
    """The other half of the same regression: a budget that runs out on the lap
    that empties the frontier reported ``frontier_empty`` — "nothing left on the
    frontier for a larger budget to reach" — for a run that had declined to buy
    a larger budget in the first place."""
    engine, _ = engine_for(sessions, ledger=(Hop(FUNDER, ROOT, 900, 2, "tx_fund"),))
    investigation_id = await engine.start(
        CHAIN, ROOT, (Objective.FIND_PREV_VASP,), Budgets(api_calls=2, pursue_until_answered=False)
    )

    assert await engine.run(investigation_id) == "partial"

    terminal = budget_terminal(await findings_of(sessions, investigation_id))
    assert terminal.summary == OLD_STOP.format(n=0)


async def test_pursuit_off_still_owns_an_earlier_run_s_extensions(sessions) -> None:
    """Switching pursuit off does not un-spend what the investigation already
    spent, and the sentence that says so has to be reachable.

    Run 1 pursues to its ceiling; the operator resumes with pursuit off and a
    bigger number. Those two grants are still this investigation's cost. While
    ``_pursue`` ignored the flag this branch could not be reached by any input
    in the suite — the resumed run came back as ``extensions_exhausted`` and
    wore the ceiling's "chasing the unanswered objective(s)" wording instead,
    claiming a pursuit for a run that had declined to pursue.
    """
    engine, _ = engine_for(sessions)  # no labels: nothing can ever be named
    investigation_id = await engine.start(CHAIN, ROOT, BOTH, Budgets(api_calls=1, max_extensions=2))
    assert await engine.run(investigation_id) == "partial"

    async with sessions() as session:
        repository = InvestigationRepository(session)
        assert await repository.claim_for_resume(
            investigation_id,
            budgets=Budgets(api_calls=4, pursue_until_answered=False).to_dict(),
        )
        await session.commit()
    assert await engine.run(investigation_id) == "partial"

    # The LAST budget terminal: run 1 filed one too, and run 1 is the one that
    # pursued. Reading the first would assert about the wrong run.
    terminal = [
        f
        for f in await findings_of(sessions, investigation_id)
        if f.kind is FindingKind.TERMINAL and f.summary.startswith("budget '")
    ][-1]
    assert ", after 2 earlier budget extension(s)," in terminal.summary
    assert "chasing the unanswered objective(s)" not in terminal.summary, (
        "this run declined to pursue; only the earlier one did"
    )


# ── the record ───────────────────────────────────────────────────────────


async def test_every_extension_reaches_the_coverage_summary_and_the_caveats(sessions) -> None:
    """An investigator must be able to see the run cost several times its budget.

    Read from the investigation record rather than from a terminal finding,
    because the SUCCESSFUL pursuit files no terminal for the objective it
    closed: if the disclosure hung off the terminals it would appear only in
    reports where the pursuit failed, which is the one place it flatters us.
    """
    engine, _ = engine_for(sessions, VASP_LABELS)
    investigation_id = await engine.start(CHAIN, ROOT, BOTH, Budgets(api_calls=1))

    await engine.run(investigation_id)

    row = await record(sessions, investigation_id)
    async with sessions() as session:
        coverage = await collect_coverage(InvestigationRepository(session), row)
        report = await collect_report(session, investigation_id)

    assert len(coverage.budget_extensions) == 2
    assert coverage.budget_extensions[0] == (
        "budget 'api_calls' extended from 1 to 2 to keep pursuing an unanswered objective "
        "(find_prev_vasp, find_next_vasp still had no named endpoint)"
    )
    assert "find_next_vasp still had no named endpoint" in coverage.budget_extensions[1], (
        "the second grant names only the objective that was still open"
    )
    assert "find_prev_vasp" not in coverage.budget_extensions[1]

    caveat = report.caveat("budget_extended")
    assert caveat is not None
    assert "extended its own budget 2 time(s)" in caveat.headline
    assert "The search DEPTH was never extended" in caveat.detail

    # And the engine says it in its own coverage sentence, which rides on every
    # finding the run files rather than only on the report.
    observations = [
        e.summary
        for f in await findings_of(sessions, investigation_id)
        for e in f.evidence
        if e.kind is EvidenceKind.ENGINE_OBSERVATION
    ]
    assert any(
        "2 budget extension(s) were granted" in o and "api_calls 1 → 3" in o for o in observations
    )


# ── the ceiling itself ───────────────────────────────────────────────────


def test_the_pursuit_settings_survive_a_round_trip() -> None:
    """Budgets are stored as JSONB and rebuilt on every run and every resume. A
    field that did not round-trip would silently restore the DEFAULT — so a
    caller who switched pursuit off would get it back on at the first resume."""
    original = Budgets(api_calls=7, pursue_until_answered=False, max_extensions=3)

    assert Budgets.from_dict(original.to_dict()) == original
    # A row written before pursuit existed has neither key and must read as the
    # current default rather than as "pursuit off".
    legacy = Budgets.from_dict({"api_calls": 7, "seconds": 300.0, "max_depth": 6, "max_nodes": 500})
    assert legacy.pursue_until_answered is True
    assert legacy.max_extensions == 8


def test_a_resume_does_not_hand_back_a_fresh_ceiling() -> None:
    """The ceiling counts the INVESTIGATION's extensions, not the run's.

    Counted per run, eight resumes would buy sixty-four automatic extensions and
    the cap would bound nothing at all — the runaway it exists to prevent, just
    slower. The operator resuming still gets the budget they named; what they do
    not get is a fresh allowance of self-granted ones.
    """
    spent = {
        "api_calls": 4,
        "txs_normalized": 9,
        "budget_extensions": [
            BudgetExtension("api_calls", 1.0, 2.0, ("find_next_vasp",)).to_dict(),
            BudgetExtension("api_calls", 2.0, 3.0, ("find_next_vasp",)).to_dict(),
        ],
    }
    tracker = BudgetTracker(Budgets(api_calls=50, max_extensions=2))
    tracker.seed_spent(spent)

    assert len(tracker.extensions()) == 2
    assert tracker.may_extend() is False
    assert tracker.spent_snapshot()["budget_extensions"] == spent["budget_extensions"], (
        "an earlier run's grants stay in the record the resumed run rewrites"
    )


def test_a_ceiling_of_zero_means_no_pursuit_at_all() -> None:
    """``max_extensions=0`` is the caller who wants the pursuit machinery present
    and switched off by arithmetic rather than by flag. It must hold even with
    ``pursue_until_answered`` left at its default."""
    tracker = BudgetTracker(Budgets(api_calls=2, max_extensions=0))

    assert tracker.budgets.pursue_until_answered is True
    assert tracker.may_extend() is False
    with pytest.raises(ValueError, match="no extension left"):
        tracker.extend("api_calls", ["find_next_vasp"])


# ── a ceiling nobody spent is not a pursuit ──────────────────────────────


async def test_a_ceiling_that_granted_nothing_never_claims_the_run_tried(sessions) -> None:
    """``max_extensions=0`` stops on the FIRST budget, and must say only that.

    The ceiling wording is the strong statement — "we bought more allowance
    several times and still named nobody" — and it was printed by reason code
    alone, so a run that had granted itself nothing reached it too. The document
    then read "after 0 budget extension(s) chasing the unanswered objective(s)
    () and no extension left" and, per objective, "the run extended its budgets
    0 time(s) chasing this objective and still named nobody": the weakest stop
    wearing the strongest sentence, in the one direction that overstates what
    the trace did.
    """
    engine, adapter = engine_for(sessions)
    zero = await engine.start(CHAIN, ROOT, BOTH, Budgets(api_calls=1, max_extensions=0))
    assert await engine.run(zero) == "partial"

    assert adapter.history_calls == [ROOT], "a ceiling of zero buys nothing"
    findings = await findings_of(sessions, zero)
    terminal = budget_terminal(findings)
    assert terminal.summary == OLD_STOP.format(n=2), (
        "identical to the pursuit-off stop, because that is what happened"
    )
    for finding in findings:
        assert "extension" not in finding.summary
        for evidence in finding.evidence:
            assert "permitted budget extension" not in evidence.summary


async def test_a_resume_past_the_ceiling_credits_the_investigation_not_the_run(sessions) -> None:
    """The grants reported at a ceiling may belong to an EARLIER run.

    The ceiling counts the investigation, so a resume can meet it having granted
    itself nothing at all. "The run extended its budgets 2 time(s)" was then
    false about the run holding the sentence, in a document whose whole value is
    that its statements are checkable against the record.
    """
    engine, _ = engine_for(sessions)
    investigation_id = await engine.start(CHAIN, ROOT, BOTH, Budgets(api_calls=1, max_extensions=2))
    assert await engine.run(investigation_id) == "partial"

    async with sessions() as session:
        repository = InvestigationRepository(session)
        assert await repository.claim_for_resume(
            investigation_id, budgets=Budgets(api_calls=4, max_extensions=2).to_dict()
        )
        await session.commit()
    assert await engine.run(investigation_id) == "partial"

    directed = [
        f
        for f in await findings_of(sessions, investigation_id)
        if f.kind is FindingKind.TERMINAL and f.direction is not None
    ]
    assert directed, "the resumed run filed its own per-objective terminals"
    for finding in directed[-2:]:
        assert "this investigation extended its budgets 2 time(s)" in finding.summary
        assert "the run extended its budgets" not in finding.summary


async def test_a_grant_is_durable_before_the_run_spends_against_it(sessions) -> None:
    """The record has to show the raised limit even if the run dies using it.

    The grant is written and committed in the loop, BEFORE control returns to
    claim the next node. Deferred to the ordinary end-of-node write instead, a
    run that died on the very expansion its extension paid for would leave a row
    whose budgets say 1 and whose spend says 1 — with no trace of the allowance
    it had granted itself. An overspend nobody can account for is exactly the
    disclosure the extension record exists to make, and the crash is the case
    where it matters most.
    """
    engine, adapter = engine_for(sessions, VASP_LABELS)
    original = adapter.address_history
    calls = {"n": 0}

    async def dies_on_the_second_expansion(address, **kwargs):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("provider died")
        return await original(address, **kwargs)

    adapter.address_history = dies_on_the_second_expansion  # type: ignore[method-assign]
    iid = await engine.start(CHAIN, ROOT, BOTH, Budgets(api_calls=1))

    with pytest.raises(RuntimeError, match="provider died"):
        await engine.run(iid)

    row = await record(sessions, iid)
    assert row.status == "failed"
    assert [e["budget"] for e in row.spent["budget_extensions"]] == ["api_calls"], (
        "the grant was made and then lost with the session it was never committed in"
    )


# ── termination and the "answered" gate, under pursuit ───────────────────


async def test_a_frontier_that_keeps_growing_still_hits_the_ceiling(sessions) -> None:
    """Pursuit buys more frontier, and a mixer crossing ADDS nodes, so the loop
    can feed itself. Without the ceiling honoured on every exit path this run
    never returns — the failure mode is a paid quota burned overnight with
    nobody watching, which is worse than stopping early."""
    engine, adapter = engine_for(sessions, ledger=fanout_ledger(4, 4))
    iid = await engine.start(CHAIN, ROOT, BOTH, Budgets(api_calls=1, max_depth=10))

    assert await engine.run(iid) == "partial"

    row = await record(sessions, iid)
    assert len(row.spent["budget_extensions"]) == 8, "the ceiling was not honoured"
    assert len(adapter.history_calls) == 9, adapter.history_calls


async def test_pursuit_stops_buying_seconds_at_the_ceiling_too(sessions) -> None:
    """Every cost budget has to respect the ceiling, not just the node count —
    a wall-clock budget that kept extending is the same runaway wearing a
    different name."""
    clock = FakeClock(step=1.0)
    engine, _ = engine_for(sessions, ledger=fanout_ledger(4, 4), clock=clock)
    iid = await engine.start(
        CHAIN, ROOT, BOTH, Budgets(api_calls=10_000, seconds=3.0, max_depth=10)
    )

    assert await engine.run(iid) == "partial"

    extensions = (await record(sessions, iid)).spent["budget_extensions"]
    assert {e["budget"] for e in extensions} == {"seconds"}
    assert len(extensions) == 8


async def test_an_unnamed_service_endpoint_never_satisfies_pursuit(sessions) -> None:
    """The gate stays a third_party_claim, or pursuit becomes a machine for
    buying budget until it accepts a guess.

    `service-endpoint@1` files a VASP_ENDPOINT from behaviour alone and says so
    itself — "operator unnamed". If that closed the objective, the run would
    stop early AND report an endpoint nobody can be served with.
    """
    ledger = [Hop(ROOT, "hub", 1_000, 1, "tx_root_hub")]
    for i in range(30):
        ledger.append(Hop(f"s{i}", "hub", 10, 2, f"tx_in_{i}"))
        ledger.append(Hop("hub", f"r{i}", 10, 3, f"tx_out_{i}"))
    engine, _ = engine_for(sessions, ledger=tuple(ledger), service=True)
    iid = await engine.start(
        CHAIN, ROOT, (Objective.FIND_NEXT_VASP,), Budgets(api_calls=2, max_extensions=3)
    )

    assert await engine.run(iid) == "partial"

    findings = await findings_of(sessions, iid)
    assert [f for f in findings if f.kind is FindingKind.VASP_ENDPOINT], (
        "the service heuristic did not fire, so this test proves nothing"
    )
    extensions = (await record(sessions, iid)).spent["budget_extensions"]
    assert len(extensions) == 3, (
        "an unnamed endpoint satisfied the objective — pursuit must spend the whole ceiling"
    )


async def test_a_named_endpoint_past_a_mixer_never_satisfies_pursuit(sessions) -> None:
    """The label is sourced; the LINK to it is a guess out of a crowd.

    Letting it close the objective would delete the only sentence in the report
    that says so, and put a speculative endpoint in the slot a reader treats as
    traced.
    """
    mixer = "mixer_pool"
    engine, _ = engine_for(
        sessions,
        {mixer: ("Test Mixer", "mixer"), "source_exchange": ("Prev VASP", "vasp")},
        (
            Hop("source_exchange", mixer, 500, 1, "tx_deposit"),
            Hop(mixer, ROOT, 500, 5, "tx_anchor"),
        ),
    )
    iid = await engine.start(
        CHAIN, ROOT, (Objective.FIND_PREV_VASP,), Budgets(api_calls=2, max_extensions=2)
    )
    await engine.run(iid)

    async with sessions() as session:
        report = await collect_report(session, iid)
    assert all(s.nearest is None and s.nearest_named is None for s in report.answers), (
        "a speculative endpoint reached a slot a reader treats as traced"
    )

"""Crossing a mixer: the engine follows past it, and everything past it is marked.

The behaviour these lock down was a deliberate reversal. A mixer contact used
to file its finding and stop the branch; the instruction that overrode it was
given three times — *"i dont want to stop at mixer … go forward until VASP"* —
with the cost accepted out loud: *"say weak decision like because of mixer and
stuff but i need VASP"*.

Reversing it opens exactly one catastrophic failure mode, and most of this
file exists to close it: a candidate on the far side of a pool belongs to a
stranger most of the time, so the run must never be able to present one the
way it presents a traced hop. The direction tests are the sharpest edge of
that — trace the wrong way and the answer is confident, well-formed, and about
somebody else's money.

Ledgers here are read literally, so each one is written so that a
correctly-directed trace and a swapped one name DIFFERENT addresses.
"""

from __future__ import annotations

import pytest

from cipherchain.analysis.mixers import (
    ANONYMITY_SET_HEURISTIC,
    LINKED_ADDRESS_HEURISTIC,
    MAX_FOLLOW,
)
from cipherchain.chains.base import ChainRegistry
from cipherchain.core.models import EvidenceKind, FindingKind
from cipherchain.investigation import Budgets, InvestigationEngine, Objective
from cipherchain.investigation.engine import (
    TERMINAL_MIXER,
    TERMINAL_MIXER_CROSSED,
    is_speculative_finding,
    speculative_basis_of,
)
from cipherchain.storage.repositories import GraphNode, InvestigationRepository
from tests.investigation.conftest import CHAIN, ROOT, FakeAdapter, Hop, MapAttributor

MIXER = "mixer_pool"
MIXER_LABEL = {MIXER: ("Test Mixer", "mixer")}

# Named for what each one is FOR: `strander` is the address a
# wrong-direction trace would name, and no test may ever see it in the graph.
SOURCE_EXCHANGE = "source_exchange"
DEST_EXCHANGE = "dest_exchange"
STRANDED = "stranded"


def engine_for(ledger: tuple[Hop, ...], labels: dict[str, tuple[str, str]], sessions):
    adapter = FakeAdapter(ledger=ledger)
    registry = ChainRegistry()
    registry.register(adapter)
    return InvestigationEngine(registry, sessions, MapAttributor(labels)), adapter


async def graph(sessions, investigation_id) -> dict[str, GraphNode]:
    async with sessions() as session:
        nodes = await InvestigationRepository(session).graph_nodes(investigation_id)
    return {node.address: node for node in nodes}


async def findings_of(sessions, investigation_id):
    async with sessions() as session:
        return await InvestigationRepository(session).list_findings(investigation_id)


async def test_a_mixer_no_longer_ends_the_branch_and_the_exit_is_marked(sessions) -> None:
    """The whole reversal in one assertion pair: named, and named as a guess.

    Backward through a pool. The trace arrives on a WITHDRAWAL the pool paid to
    the root, so the candidates are DEPOSITS that went in before it — that is
    what `trace_back_from_withdrawal` enumerates. The labelled exchange sits on
    that side and must be reached; it must also arrive carrying, in its own
    summary and evidence, the fact that no value path connects it to the root.
    """
    ledger = (
        Hop(SOURCE_EXCHANGE, MIXER, 500, 1, "tx_deposit"),  # the far side, before the anchor
        Hop(MIXER, ROOT, 500, 5, "tx_anchor"),  # how the trace reached the pool
        Hop(MIXER, STRANDED, 500, 8, "tx_later"),  # only a FORWARD trace would want this
    )
    engine, adapter = engine_for(ledger, MIXER_LABEL | {SOURCE_EXCHANGE: ("Prev VASP", "vasp")},
                                sessions)
    investigation_id = await engine.start(CHAIN, ROOT, (Objective.FIND_PREV_VASP,))

    assert await engine.run(investigation_id) == "completed"

    findings = await findings_of(sessions, investigation_id)
    vasp = [f for f in findings if f.kind is FindingKind.VASP_ENDPOINT]
    assert [f.subject.value for f in vasp] == [SOURCE_EXCHANGE], (
        "the deposit side of the pool holds the labelled exchange and must be reached"
    )
    assert "possible previous VASP beyond a mixer" in vasp[0].summary
    assert "speculative branch" in vasp[0].summary
    assert is_speculative_finding(vasp[0])
    # The answer layer bars a speculative endpoint from "nearest VASP" and
    # words its weakness from the basis. Both travel on the finding itself,
    # because a Finding is all that crosses into that layer today.
    assert speculative_basis_of(vasp[0]) == ANONYMITY_SET_HEURISTIC
    path = next(e for e in vasp[0].evidence if e.kind is EvidenceKind.ONCHAIN_FACT)
    assert "do not form a connected value path" in path.summary, (
        "the hashes are real; calling the chain of them a value path would smuggle the "
        "guess back in one taxonomy layer down"
    )

    # The pool's own history is what makes the crowd countable — it costs one
    # expansion and the test says so rather than leaving it implicit.
    assert MIXER in adapter.history_calls

    nodes = await graph(sessions, investigation_id)
    assert nodes[SOURCE_EXCHANGE].speculative is True
    assert nodes[SOURCE_EXCHANGE].speculative_basis == ANONYMITY_SET_HEURISTIC
    assert nodes[ROOT].speculative is False


async def test_backward_takes_deposits_and_never_the_far_side_of_a_forward_trace(
    sessions,
) -> None:
    """Swap the two ladder entry points and this ledger names a stranger.

    ``trace_back_from_withdrawal`` and ``trace_forward_from_deposit`` hold
    identical fields and return identical shapes, so a swap produces a
    well-formed answer about an unrelated party with no symptom anywhere. Here
    the deposit before the anchor and the withdrawal after it are two different
    addresses: a backward objective must reach the depositor and must never
    admit the later withdrawal, whose funds cannot be the ones that arrived.
    """
    ledger = (
        Hop(SOURCE_EXCHANGE, MIXER, 500, 1, "tx_deposit"),
        Hop(MIXER, ROOT, 500, 5, "tx_anchor"),
        Hop(MIXER, STRANDED, 500, 8, "tx_later"),
    )
    engine, _ = engine_for(ledger, MIXER_LABEL, sessions)
    investigation_id = await engine.start(CHAIN, ROOT, (Objective.FIND_PREV_VASP,))
    await engine.run(investigation_id)

    nodes = await graph(sessions, investigation_id)
    assert SOURCE_EXCHANGE in nodes, "the deposit before the withdrawal is the candidate"
    assert STRANDED not in nodes, (
        "a withdrawal made AFTER the anchor cannot have funded it — following it would "
        "mean the forward entry point was called for a backward objective"
    )


async def test_forward_takes_withdrawals_and_never_the_far_side_of_a_backward_trace(
    sessions,
) -> None:
    """The mirror of the test above, and it fails on the same swap.

    Forward, the trace arrives on a DEPOSIT it made into the pool, and the
    candidates are withdrawals that came out after it. The deposit somebody
    else made beforehand is the wrong-direction answer.
    """
    ledger = (
        Hop(STRANDED, MIXER, 500, 1, "tx_other_deposit"),
        Hop(ROOT, MIXER, 500, 5, "tx_anchor"),
        Hop(MIXER, DEST_EXCHANGE, 500, 8, "tx_withdrawal"),
    )
    engine, _ = engine_for(ledger, MIXER_LABEL | {DEST_EXCHANGE: ("Next VASP", "vasp")}, sessions)
    investigation_id = await engine.start(CHAIN, ROOT, (Objective.FIND_NEXT_VASP,))
    await engine.run(investigation_id)

    nodes = await graph(sessions, investigation_id)
    assert DEST_EXCHANGE in nodes, "the withdrawal after the deposit is the candidate"
    assert STRANDED not in nodes, (
        "a deposit made BEFORE the anchor cannot have been funded by it — following it "
        "would mean the backward entry point was called for a forward objective"
    )
    findings = await findings_of(sessions, investigation_id)
    vasp = [f for f in findings if f.kind is FindingKind.VASP_ENDPOINT]
    assert [f.subject.value for f in vasp] == [DEST_EXCHANGE]
    assert "possible next VASP beyond a mixer" in vasp[0].summary


async def test_speculation_is_inherited_two_hops_past_the_crossing(sessions) -> None:
    """A guess does not become a fact by being one hop further along.

    Every node below a mixer candidate was reached by following a real
    movement, which is exactly why this needs its own rule: the movements are
    real and the branch they hang off is still a guess. The basis travels down
    so a report can name the specific heuristic a node three hops out depends
    on, rather than only saying that some guess happened somewhere.
    """
    ledger = (
        Hop(ROOT, MIXER, 500, 5, "tx_anchor"),
        Hop(MIXER, "exit", 500, 8, "tx_exit"),
        Hop("exit", "deeper", 400, 9, "tx_deep"),
        Hop("deeper", "deepest", 300, 10, "tx_deeper"),
    )
    engine, _ = engine_for(ledger, MIXER_LABEL, sessions)
    investigation_id = await engine.start(CHAIN, ROOT, (Objective.FIND_NEXT_VASP,))
    await engine.run(investigation_id)

    nodes = await graph(sessions, investigation_id)
    for address, hop in (("exit", 2), ("deeper", 3), ("deepest", 4)):
        assert nodes[address].speculative is True, f"{address} rests on the mixer guess"
        assert nodes[address].speculative_basis == ANONYMITY_SET_HEURISTIC
        assert nodes[address].hop_distance == hop
    assert nodes["deepest"].hop_distance - nodes[MIXER].hop_distance >= 2, (
        "inheritance has to hold beyond the first hop, which is where it would be "
        "easiest to lose"
    )


async def test_a_crossed_mixer_is_still_visibly_a_mixer(sessions) -> None:
    """Following past a pool must not erase that the money went through one.

    Two records have to survive the crossing: the sourced claim that this
    address is a mixer, and a statement of what the engine then did about it.
    The node keeps a mixer-prefixed terminal reason because the TRACED trail
    genuinely ended there — what continues hangs off it as a guess.
    """
    ledger = (
        Hop(ROOT, MIXER, 500, 5, "tx_anchor"),
        Hop(MIXER, DEST_EXCHANGE, 500, 8, "tx_withdrawal"),
    )
    engine, _ = engine_for(ledger, MIXER_LABEL, sessions)
    investigation_id = await engine.start(CHAIN, ROOT, (Objective.FIND_NEXT_VASP,))
    await engine.run(investigation_id)

    nodes = await graph(sessions, investigation_id)
    assert nodes[MIXER].terminal_reason == TERMINAL_MIXER_CROSSED
    assert nodes[MIXER].speculative is False, "the pool itself was reached by a real movement"

    findings = [
        f
        for f in await findings_of(sessions, investigation_id)
        if f.kind is FindingKind.MIXER_INTERACTION
    ]
    contact = next(f for f in findings if "funds reached a known mixer" in f.summary)
    assert any(e.kind is EvidenceKind.THIRD_PARTY_CLAIM for e in contact.evidence)
    assert "cannot be followed through it" not in contact.summary, (
        "the old wording contradicted the crossing it now sits next to"
    )

    crossing = next(f for f in findings if "crossed this mixer" in f.summary)
    assert "1 candidate branch(es) followed as SPECULATIVE" in crossing.summary
    observation = next(e for e in crossing.evidence if e.kind is EvidenceKind.ENGINE_OBSERVATION)
    assert "not from the pool's full history" in observation.summary, (
        "an anonymity set counted from stored movements is a floor, and the confidence "
        "derived from it is optimistic — the reader has to be told"
    )
    weakness = next(e for e in crossing.evidence if e.kind is EvidenceKind.HEURISTIC_INFERENCE)
    assert weakness.heuristic == ANONYMITY_SET_HEURISTIC
    assert "lead, not an attribution" in weakness.summary


async def test_a_speculative_endpoint_does_not_answer_the_objective(sessions) -> None:
    """Naming an exchange across a pool is not the same as reaching one.

    The label is sourced and the finding stands; the LINK is a pick out of a
    crowd. If that closed the objective, the run would delete the only sentence
    in the report saying the trail was never followed cleanly — and the answer
    layer bars speculative endpoints from "nearest VASP", so the reader would
    be left with no headline and no explanation.
    """
    ledger = (
        Hop(ROOT, MIXER, 500, 5, "tx_anchor"),
        Hop(MIXER, DEST_EXCHANGE, 500, 8, "tx_withdrawal"),
    )
    engine, _ = engine_for(ledger, MIXER_LABEL | {DEST_EXCHANGE: ("Next VASP", "vasp")}, sessions)
    investigation_id = await engine.start(CHAIN, ROOT, (Objective.FIND_NEXT_VASP,))
    await engine.run(investigation_id)

    findings = await findings_of(sessions, investigation_id)
    assert [f.subject.value for f in findings if f.kind is FindingKind.VASP_ENDPOINT] == [
        DEST_EXCHANGE
    ]
    terminal = next(
        f
        for f in findings
        if f.kind is FindingKind.TERMINAL and f.direction == "forward"
    )
    assert "no endpoint reached without crossing a mixer" in terminal.summary
    coverage = next(e for e in terminal.evidence if "mixer(s) were crossed" in e.summary)
    assert coverage.kind is EvidenceKind.ENGINE_OBSERVATION
    assert coverage.confidence is None


async def test_a_mixer_with_nothing_on_the_far_side_still_stops(sessions) -> None:
    """The honest no survives. Following is a ruling, not an obligation to invent.

    Nothing was stored on the other side of this pool, so there is no crowd and
    no candidate. The branch closes exactly as it did before the crossing
    existed, and the run says which of the two happened.
    """
    ledger = (Hop(ROOT, MIXER, 500, 5, "tx_anchor"),)
    engine, _ = engine_for(ledger, MIXER_LABEL, sessions)
    investigation_id = await engine.start(CHAIN, ROOT, (Objective.FIND_NEXT_VASP,))

    assert await engine.run(investigation_id) == "completed"

    nodes = await graph(sessions, investigation_id)
    assert nodes[MIXER].terminal_reason == TERMINAL_MIXER
    findings = await findings_of(sessions, investigation_id)
    stopped = next(f for f in findings if "the trail stopped at this mixer" in f.summary)
    assert "not evidence that none exists" in stopped.summary
    terminal = next(f for f in findings if f.kind is FindingKind.TERMINAL)
    assert "trace exhausted without an attributed endpoint" in terminal.summary


async def test_a_candidate_already_on_a_traced_path_keeps_its_provenance(sessions) -> None:
    """A guess may not attach itself to an address the trace really reached.

    The pool's only exit here is an address the root paid directly, so it is
    already in the graph as a traced hop. Hanging a mixer-exit edge onto it
    would let a path search route that node's evidence through the pool, and
    the finding would then cite a value path that does not exist.
    """
    ledger = (
        Hop(ROOT, MIXER, 500, 5, "tx_anchor"),
        Hop(ROOT, "payee", 100, 5, "tx_direct"),
        Hop(MIXER, "payee", 500, 8, "tx_withdrawal"),
    )
    engine, _ = engine_for(ledger, MIXER_LABEL, sessions)
    investigation_id = await engine.start(CHAIN, ROOT, (Objective.FIND_NEXT_VASP,))
    await engine.run(investigation_id)

    nodes = await graph(sessions, investigation_id)
    assert nodes["payee"].speculative is False
    assert nodes["payee"].speculative_basis is None
    assert nodes[MIXER].terminal_reason == TERMINAL_MIXER

    stopped = next(
        f
        for f in await findings_of(sessions, investigation_id)
        if "the trail stopped at this mixer" in f.summary
    )
    observation = next(e for e in stopped.evidence if e.kind is EvidenceKind.ENGINE_OBSERVATION)
    assert "already reached by a traced path" in observation.summary


async def test_the_budget_cap_on_a_crossing_is_reported_not_silent(sessions) -> None:
    """A pool multiplies the frontier, so what it dropped has to be visible.

    An investigator reading a shortlist of exits needs to know it is a
    shortlist. Silently truncating it would present the followed branches as
    the only ones there were, when the subject's own exit may be among the
    dropped.
    """
    ledger = (
        Hop(ROOT, MIXER, 900, 5, "tx_anchor"),
        Hop(MIXER, "exit_a", 300, 8, "tx_a"),
        Hop(MIXER, "exit_b", 200, 8, "tx_b"),
        Hop(MIXER, "exit_c", 100, 8, "tx_c"),
    )
    engine, _ = engine_for(ledger, MIXER_LABEL, sessions)
    investigation_id = await engine.start(
        CHAIN,
        ROOT,
        (Objective.FIND_NEXT_VASP,),
        # Room for the root, the pool, and exactly one candidate. Pursuit off:
        # this test is about what the CAP records when it bites, and pursuit
        # would raise the cap until it stopped biting.
        Budgets(max_nodes=3, pursue_until_answered=False),
    )

    assert await engine.run(investigation_id) == "partial"

    nodes = await graph(sessions, investigation_id)
    followed = [a for a in ("exit_a", "exit_b", "exit_c") if a in nodes]
    assert followed == ["exit_a"], "the cap keeps the money: candidates are ranked by value"
    assert nodes["exit_a"].speculative is True

    crossing = next(
        f
        for f in await findings_of(sessions, investigation_id)
        if "crossed this mixer" in f.summary
    )
    assert "2 not followed" in crossing.summary
    observation = next(e for e in crossing.evidence if e.kind is EvidenceKind.ENGINE_OBSERVATION)
    assert "2 no node budget left" in observation.summary


async def test_the_mixer_contact_is_recorded_even_if_the_run_dies_first(sessions) -> None:
    """The contact is filed at discovery; only the CROSSING waits for the claim.

    The crossing needs the pool's own transactions, so it cannot be decided
    until the node is claimed — but "the money touched a mixer" is established
    by the label alone. Deferring both would mean a run that ran out of budget
    reported a clean trail through a pool it had already identified.
    """
    ledger = (
        Hop(ROOT, MIXER, 500, 5, "tx_anchor"),
        Hop(MIXER, DEST_EXCHANGE, 500, 8, "tx_withdrawal"),
    )
    engine, adapter = engine_for(ledger, MIXER_LABEL, sessions)
    investigation_id = await engine.start(
        CHAIN,
        ROOT,
        (Objective.FIND_NEXT_VASP,),
        # Pursuit off: the run must genuinely die on its first budget for this
        # to be a test about what a dead run had already recorded.
        Budgets(api_calls=1, pursue_until_answered=False),
    )

    assert await engine.run(investigation_id) == "partial"
    assert adapter.history_calls == [ROOT], "the pool was never expanded"

    findings = await findings_of(sessions, investigation_id)
    contact = next(f for f in findings if f.kind is FindingKind.MIXER_INTERACTION)
    assert contact.subject.value == MIXER
    assert "funds reached a known mixer" in contact.summary
    assert not [f for f in findings if "crossed this mixer" in f.summary]


async def test_an_identity_rung_wider_than_the_follow_cap_says_what_it_left(sessions) -> None:
    """The cap the ladder does not apply to itself, and the sentence that shows it.

    Rung 5 truncates its own crowd, but the identity rungs return every match
    they find — "how many is too many" is a budget question and the ladder
    holds no budget. Here the linked-address rung names 25 exits at once; 20
    are followed and the finding has to say the other 5 exist, because the
    subject's own exit may be one of them.
    """
    linked = tuple(
        [
            Hop(ROOT, "mid", 900, 1, "tx_root"),
            Hop("mid", MIXER, 800, 2, "tx_anchor"),
        ]
        # Paid INTO `mid`, so a forward trace never follows them directly and
        # they can only enter the graph through the pool.
        + [Hop(f"w{i}", "mid", 10 + i, 1, f"tx_pay_{i}") for i in range(25)]
        + [Hop(MIXER, f"w{i}", 100 + i, 4, f"tx_out_{i}") for i in range(25)]
    )
    engine, _ = engine_for(linked, MIXER_LABEL, sessions)
    investigation_id = await engine.start(CHAIN, ROOT, (Objective.FIND_NEXT_VASP,))
    await engine.run(investigation_id)

    crossing = next(
        f
        for f in await findings_of(sessions, investigation_id)
        if "crossed this mixer" in f.summary
    )
    assert f"{MAX_FOLLOW} candidate branch(es) followed as SPECULATIVE" in crossing.summary
    assert "5 not followed" in crossing.summary
    observation = next(e for e in crossing.evidence if e.kind is EvidenceKind.ENGINE_OBSERVATION)
    assert "5 beyond the follow cap" in observation.summary
    weakness = next(e for e in crossing.evidence if e.kind is EvidenceKind.HEURISTIC_INFERENCE)
    assert weakness.heuristic == LINKED_ADDRESS_HEURISTIC

    nodes = await graph(sessions, investigation_id)
    # Ranked by value, so the cap keeps the largest exits — the same rule the
    # supernode guard follows, for the same reason.
    assert "w24" in nodes and "w0" not in nodes


async def test_every_finding_past_a_mixer_is_marked_not_only_the_endpoint(sessions) -> None:
    """The rule says *anywhere*, so a sanctions hit on a guess is marked too.

    A sanctioned-address finding filed against a mixer candidate reads exactly
    like one filed against a traced hop, and it is the kind of line that gets
    quoted on its own. If only VASP findings carried the caveat, every other
    conclusion on the branch would leave here looking traced.
    """
    ledger = (
        Hop(ROOT, MIXER, 500, 5, "tx_anchor"),
        Hop(MIXER, "flagged", 500, 8, "tx_withdrawal"),
    )
    engine, _ = engine_for(ledger, MIXER_LABEL | {"flagged": ("OFAC Listed", "sanctioned")},
                           sessions)
    investigation_id = await engine.start(CHAIN, ROOT, (Objective.FIND_NEXT_VASP,))
    await engine.run(investigation_id)

    sanctioned = next(
        f
        for f in await findings_of(sessions, investigation_id)
        if f.kind is FindingKind.SANCTIONED_ADDRESS
    )
    assert sanctioned.subject.value == "flagged"
    assert "seen on a branch past a mixer" in sanctioned.summary
    assert is_speculative_finding(sanctioned)
    assert speculative_basis_of(sanctioned) == ANONYMITY_SET_HEURISTIC


@pytest.mark.parametrize("objective", [Objective.FIND_PREV_VASP, Objective.FIND_NEXT_VASP])
async def test_the_mixer_itself_is_never_offered_as_the_exit(sessions, objective) -> None:
    """A pool appears on both sides of itself and would win every rung.

    Left in the candidate set, the mixer resolves its own crossing by naming
    itself as the source or destination of the money — for every user, every
    time. The activity handed to the ladder therefore declares its own address,
    and this asserts the engine actually does that rather than trusting it.
    """
    ledger = (
        Hop(MIXER, MIXER, 700, 1, "tx_self"),
        Hop(SOURCE_EXCHANGE, MIXER, 500, 2, "tx_deposit"),
        Hop(MIXER, ROOT, 500, 5, "tx_back"),
        Hop(ROOT, MIXER, 400, 5, "tx_fwd"),
        Hop(MIXER, DEST_EXCHANGE, 400, 8, "tx_withdrawal"),
    )
    engine, _ = engine_for(ledger, MIXER_LABEL, sessions)
    investigation_id = await engine.start(CHAIN, ROOT, (objective,))
    await engine.run(investigation_id)

    nodes = await graph(sessions, investigation_id)
    assert nodes[MIXER].speculative is False
    assert nodes[MIXER].hop_distance == 1, "the pool is reached once, by a real movement"
    expected = SOURCE_EXCHANGE if objective is Objective.FIND_PREV_VASP else DEST_EXCHANGE
    assert nodes[expected].speculative is True


async def test_the_activity_handed_to_the_ladder_declares_the_pools_own_address(
    sessions,
) -> None:
    """The exclusion is DECLARED, not left to the shape of the query.

    ``_mixer_facts`` also happens to skip a movement whose counterparty is the
    pool itself, so blanking ``mixer_address_ids`` changes no engine outcome
    today and the test above stays green — the property it claims to assert is
    held up by a different mechanism. That is only true while ONE address is
    involved: the ladder's exclusion set is what a router or relayer id would
    be added to, and it must be a live channel when that happens rather than a
    dead argument nobody noticed had stopped being read.
    """
    from cipherchain.core.models import Address
    from cipherchain.storage.repositories import FactRepository

    ledger = (
        Hop(MIXER, MIXER, 700, 1, "tx_self"),
        Hop(ROOT, MIXER, 500, 5, "tx_anchor"),
        Hop(MIXER, DEST_EXCHANGE, 500, 8, "tx_withdrawal"),
    )
    engine, _ = engine_for(ledger, MIXER_LABEL, sessions)
    investigation_id = await engine.start(CHAIN, ROOT, (Objective.FIND_NEXT_VASP,))
    await engine.run(investigation_id)

    async with sessions() as session:
        facts = FactRepository(session)
        mixer_id = await facts.get_or_create_address(Address(CHAIN, MIXER))
        root_id = await facts.get_or_create_address(Address(CHAIN, ROOT))
        incoming, outgoing = await facts.movements_around_address(mixer_id, limit=500)
        assets = await facts.asset_facts({m.asset_id for m in (*incoming, *outgoing)})
        mixer_facts = await InvestigationEngine._mixer_facts(
            facts, Address(CHAIN, MIXER), mixer_id, root_id, incoming, outgoing, assets
        )
    assert mixer_facts.activity.mixer_address_ids == frozenset({mixer_id}), (
        "the pool must be named to the ladder as not-a-party, whatever else filters it"
    )


async def test_a_crossing_this_trace_did_not_make_is_reported_not_silent(sessions) -> None:
    """A pool is used repeatedly by design, and only one crossing is run.

    Ten deposits of a fixed denomination is how a Tornado-style pool is used,
    and they collapse into ONE graph edge carrying the largest — so the run
    anchors on that one and never enumerates the other nine crowds. The count
    of dropped CANDIDATES cannot say so: it is about one anchor's far side, and
    printed alone it reads as the whole of what the trace did at this pool.
    """
    ledger = (
        Hop(ROOT, MIXER, 900, 2, "tx_big"),
        Hop(ROOT, MIXER, 100, 20, "tx_small"),
        Hop(MIXER, "near_big", 900, 3, "tx_out_near_big"),
        Hop(MIXER, "near_small", 100, 21, "tx_out_near_small"),
    )
    engine, _ = engine_for(ledger, MIXER_LABEL, sessions)
    investigation_id = await engine.start(CHAIN, ROOT, (Objective.FIND_NEXT_VASP,))
    await engine.run(investigation_id)

    nodes = await graph(sessions, investigation_id)
    assert "near_big" in nodes, "the largest deposit is the one anchored"
    assert "near_small" not in nodes, (
        "the second deposit is a whole second crossing and this run does not make it"
    )

    crossing = next(
        f
        for f in await findings_of(sessions, investigation_id)
        if "crossed this mixer" in f.summary
    )
    observation = next(e for e in crossing.evidence if e.kind is EvidenceKind.ENGINE_OBSERVATION)
    assert "1 further transaction(s) between this address and the pool" in observation.summary
    assert "never enumerated" in observation.summary


async def test_the_unmade_crossing_is_counted_on_the_backward_side_too(sessions) -> None:
    """Backward the un-anchored movements are WITHDRAWALS, not deposits.

    The side a further crossing would sit on flips with the objective, exactly
    as the anchor's does. Counted on the wrong side the number is always zero
    here — the pool is the counterparty of every movement on the other list —
    and a caveat that never prints is indistinguishable from one that was never
    written.
    """
    ledger = (
        Hop("dep_small", MIXER, 100, 1, "tx_in_small"),
        Hop(MIXER, ROOT, 100, 2, "tx_small_out"),
        Hop("dep_big", MIXER, 900, 9, "tx_in_big"),
        Hop(MIXER, ROOT, 900, 10, "tx_big_out"),
    )
    engine, _ = engine_for(ledger, MIXER_LABEL, sessions)
    investigation_id = await engine.start(CHAIN, ROOT, (Objective.FIND_PREV_VASP,))
    await engine.run(investigation_id)

    nodes = await graph(sessions, investigation_id)
    assert "dep_big" in nodes, "the largest withdrawal is the one anchored"
    assert "dep_small" not in nodes

    crossing = next(
        f
        for f in await findings_of(sessions, investigation_id)
        if "crossed this mixer" in f.summary
    )
    observation = next(e for e in crossing.evidence if e.kind is EvidenceKind.ENGINE_OBSERVATION)
    assert "1 further transaction(s) between this address and the pool" in observation.summary


async def test_a_single_crossing_does_not_invent_a_second_one(sessions) -> None:
    """The caveat above is a fact, not a template — one deposit means no sentence."""
    ledger = (
        Hop(ROOT, MIXER, 900, 2, "tx_anchor"),
        Hop(MIXER, "exit", 900, 3, "tx_out"),
    )
    engine, _ = engine_for(ledger, MIXER_LABEL, sessions)
    investigation_id = await engine.start(CHAIN, ROOT, (Objective.FIND_NEXT_VASP,))
    await engine.run(investigation_id)

    crossing = next(
        f
        for f in await findings_of(sessions, investigation_id)
        if "crossed this mixer" in f.summary
    )
    observation = next(e for e in crossing.evidence if e.kind is EvidenceKind.ENGINE_OBSERVATION)
    assert "not used as an anchor" not in observation.summary

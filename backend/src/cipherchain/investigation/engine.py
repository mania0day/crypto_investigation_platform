"""The investigation loop: claim → attribute → guard → expand → re-plan.

Every iteration is one committed checkpoint — kill the process at any
point and ``run()`` resumes from the stored frontier without refetching
(the fact store and cache make repeats free). End-states are explicit
findings, never silence (rulings R2/R3, vision principle 9).

Mixers do not end a branch
--------------------------
They used to. A mixer contact filed its finding and the branch stopped, on
the reasoning that following value through a severed link is
de-anonymization. The instruction that overrides it was given three times —
*"i dont want to stop at mixer … go forward until VASP, VASP will be there,
there is no way no VASP"* — with the trade-off accepted explicitly: *"say
weak decision like because of mixer and stuff but i need VASP"*. The ruling
is **follow it, but marked**.

So a mixer node is now read like any other address and then handed to the
exit ladder in ``cipherchain.analysis.mixers``, and every branch that leaves it is
written into the graph as a SPECULATIVE node naming the heuristic that
proposed it. Three properties keep that from becoming the lie the old
behaviour was avoiding:

1. **Direction is chosen by which ladder function is called**, and the two
   take different types. Backward asks where money came FROM, so the anchor
   is a withdrawal; forward asks where it went, so the anchor is a deposit.
   Swapping them produces confident answers about strangers, and the output
   is indistinguishable from a working one — hence
   ``tests/investigation/test_engine_mixer.py`` asserts the anchor's side.
2. **Speculation is inherited.** Everything discovered downstream of a
   speculative node is speculative, with the ancestor's basis: a guess does
   not become a fact by being one hop further along.
3. **An endpoint reached this way says so in its own summary and evidence**,
   and does not satisfy the objective's "answered" gate — the run still files
   the terminal that says no endpoint was reached without crossing a mixer.

``cipherchain.analysis.mixers`` is imported rather than injected, unlike the
detectors and the asset policy. Injecting it would mean passing the two
entry points as one callable type, which erases exactly the distinction that
makes the direction bug impossible to write; and the package is pure value
code — no session, no provider — so importing it costs the engine nothing it
was protecting.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import cipherchain
from cipherchain.analysis.mixers import (
    MAX_FOLLOW,
    DirectInteraction,
    MixerActivity,
    MixerCandidate,
    MixerDeposit,
    MixerExitResult,
    MixerWithdrawal,
    trace_back_from_withdrawal,
    trace_forward_from_deposit,
)
from cipherchain.chains.base import BridgeDirection, BridgeHint, ChainAdapter, ChainRegistry
from cipherchain.core.logging import bind_investigation, unbind_investigation
from cipherchain.core.models import (
    Address,
    Direction,
    Evidence,
    EvidenceKind,
    Finding,
    FindingKind,
)
from cipherchain.graph.paths import path_tx_hashes
from cipherchain.investigation.attribution import (
    CATEGORY_INFRASTRUCTURE,
    CATEGORY_MIXER,
    CATEGORY_SANCTIONED,
    CATEGORY_VASP,
    AddressRole,
    AttributionResult,
    Attributor,
)
from cipherchain.investigation.budgets import (
    BudgetExtension,
    Budgets,
    BudgetTracker,
    extension_summary,
)
from cipherchain.investigation.frontier import derive_counterparties
from cipherchain.investigation.objectives import Objective
from cipherchain.storage.repositories import (
    AssetFacts,
    FactRepository,
    InvestigationRepository,
    StoredMovement,
)

if TYPE_CHECKING:
    from cipherchain.storage.tables import NodeRow

logger = logging.getLogger(__name__)

_MOVEMENT_QUERY_LIMIT = 500

#: ``terminal_reason`` on the mixer node itself. Both values start with
#: "mixer" so nothing that groups by prefix loses a mixer contact, and they
#: stay distinct because "the trail stopped here" and "the trail continued on
#: a guess from here" are different facts about the same address.
TERMINAL_MIXER = "mixer"
TERMINAL_MIXER_CROSSED = "mixer_crossed"

#: First words of the ENGINE_OBSERVATION stamped onto every finding recorded
#: against a node downstream of a mixer crossing.
#:
#: This marker is no longer how the ANSWER layer learns a branch is a guess:
#: ``vasp_findings_with_hops`` now returns ``nodes.speculative`` and its basis,
#: and ``app.py``/``collect.py`` pass both into ``RankedFinding``. That is the
#: durable route and it reads a column, not a sentence.
#:
#: What remains here is the marker's honest job. It is prose in a document —
#: the reason a reader of the finding alone can see that the conclusion sits on
#: a selected link — and it is what ``_Endpoints.of`` reads, since that runs
#: inside the engine on a bare ``Sequence[Finding]`` with no node to join
#: against. Written and read in this one module, so the two cannot drift.
SPECULATIVE_EVIDENCE_PREFIX = "reached across a mixer crossing"


def _speculative_evidence(finding: Finding) -> Evidence | None:
    return next(
        (
            e
            for e in finding.evidence
            if e.kind is EvidenceKind.ENGINE_OBSERVATION
            and e.summary.startswith(SPECULATIVE_EVIDENCE_PREFIX)
        ),
        None,
    )


def is_speculative_finding(finding: Finding) -> bool:
    """Was this conclusion drawn on a branch that crosses a mixer?

    The answer layer must be able to bar such an endpoint from "nearest VASP"
    (``answers.py``) without re-reading the graph, and ``_finish_completed``
    must not let one satisfy an objective. Both ask here so there is one
    definition rather than two spellings of the same string test.
    """
    return _speculative_evidence(finding) is not None


def speculative_basis_of(finding: Finding) -> str | None:
    """The heuristic id the branch under this finding rests on, if it names one.

    For a consumer holding only a ``Finding``. The wiring no longer needs it:
    ``vasp_findings_with_hops`` returns ``nodes.speculative_basis`` itself and
    the API and report layers read that column, which is the authority. Prefer
    it — this parses a sentence, and a sentence can be reworded.

    Kept because it is the only route available to code that has findings and
    no graph: ``_Endpoints.of`` above, and any caller reading a stored finding
    outside a traversal context.
    """
    evidence = _speculative_evidence(finding)
    if evidence is None:
        return None
    tail = evidence.summary[len(SPECULATIVE_EVIDENCE_PREFIX) :]
    if not tail.startswith(" (") or ")" not in tail:
        return None
    return tail[2 : tail.index(")")] or None


@dataclass(frozen=True, slots=True)
class _Endpoints:
    """Which directions ended with what KIND of endpoint.

    Two questions, asked independently, which is why there are four groups and
    not three: *was an operator named?* and *was the route to it traced or
    selected?* They are orthogonal, and a direction can land in more than one
    group at once — a run can reach unnamed custodial infrastructure by
    following value AND a named exchange past a mixer, and both facts are ones
    an investigator acts on.

    ``named``               a sourced label, reached by following value. The
                            only group that answers an objective.
    ``named_over_mixer``    a sourced label on a branch that crosses a mixer.
                            The exchange is real; the link to the subject is a
                            guess, so the objective is NOT closed.
    ``unnamed``             custodial infrastructure inferred from behaviour and
                            reached by following value, with no operator to
                            subpoena.
    ``unnamed_over_mixer``  the same inference drawn on a guessed branch —
                            neither the operator nor the route is established.

    ``unnamed`` used to hold both of the last two. A run whose only inference
    sat past a mixer then closed with "custodial infrastructure was inferred",
    a sentence that says nothing about the crossing and reads exactly like the
    same claim drawn from a traced path. Splitting them is what lets
    ``shortfall`` say which it is holding.
    """

    named: frozenset[Direction]
    named_over_mixer: frozenset[Direction]
    unnamed: frozenset[Direction]
    unnamed_over_mixer: frozenset[Direction]

    @classmethod
    def of(cls, findings: Sequence[Finding]) -> _Endpoints:
        endpoints = [
            f for f in findings if f.kind is FindingKind.VASP_ENDPOINT and f.direction is not None
        ]

        def directions(chosen: Callable[[Finding], bool]) -> frozenset[Direction]:
            return frozenset(f.direction for f in endpoints if f.direction and chosen(f))

        def named(finding: Finding) -> bool:
            return any(e.kind is EvidenceKind.THIRD_PARTY_CLAIM for e in finding.evidence)

        return cls(
            named=directions(lambda f: named(f) and not is_speculative_finding(f)),
            named_over_mixer=directions(lambda f: named(f) and is_speculative_finding(f)),
            unnamed=directions(lambda f: not named(f) and not is_speculative_finding(f)),
            unnamed_over_mixer=directions(lambda f: not named(f) and is_speculative_finding(f)),
        )

    def shortfall(self, direction: Direction, objective: object, *, exhausted: bool) -> str:
        """Why this direction is not answered — every true reason, not the first.

        Returns the opening statement of the TERMINAL finding that closes an
        unanswered objective; the caller appends why the run stopped and what it
        left unread. The objective is named right after the headline, where a
        reader looking for "which question is this about" finds it before the
        qualifiers.

        Composed rather than chosen. The previous if/elif chain returned on the
        first bucket that matched, and the buckets overlap, so a direction
        holding a traced-but-unnamed endpoint AND a named one past a mixer
        closed with "no endpoint reached without crossing a mixer" — false,
        because the unnamed one WAS reached without crossing one. Every clause
        that is true is said, in the order an investigator needs them: what was
        traced first, what was only guessed at second.

        ``exhausted`` distinguishes the two runs that call this. A completed run
        ran out of trail; a partial one ran out of budget, and "trace exhausted"
        printed over a run that stopped on an API-call cap is the single most
        misleading sentence this function could produce — it turns "we stopped
        early" into "there was nothing more". The mixer headline is shared,
        because it is true either way.
        """
        traced = direction in self.unnamed
        over_mixer = direction in self.named_over_mixer or direction in self.unnamed_over_mixer
        if traced:
            headline = (
                "trace exhausted without a NAMED endpoint"
                if exhausted
                else "no NAMED endpoint found"
            )
        elif over_mixer:
            headline = "no endpoint reached without crossing a mixer"
        else:
            headline = (
                "trace exhausted without an attributed endpoint"
                if exhausted
                else "no attributed endpoint found"
            )
        clauses: list[str] = []
        if traced:
            clauses.append(
                "custodial infrastructure was inferred but its operator is unnamed and "
                "needs a sourced label"
            )
        if direction in self.named_over_mixer:
            clauses.append(
                "a named endpoint was found only on a speculative branch, which may belong "
                "to an unrelated party"
            )
        if direction in self.unnamed_over_mixer:
            clauses.append(
                "custodial infrastructure was inferred only on a speculative branch, so "
                "neither the operator nor the route to it is established"
            )
        opening = f"{headline} ({objective})"
        return opening if not clauses else f"{opening} — {'; '.join(clauses)}"


#: Why the run stopped at an exhausted budget instead of buying more of it.
#: Constants rather than bare strings because ``_finish_partial`` writes a
#: different terminal for each, and a report that could not tell "we ran out of
#: allowance" from "we extended eight times and still could not name anyone" is
#: hiding the stronger of the two statements about the chain.
PURSUIT_EXTENDED = "extended"
PURSUIT_DISABLED = "pursuit_disabled"
PURSUIT_ANSWERED = "objectives_answered"
PURSUIT_FRONTIER_EMPTY = "frontier_empty"
PURSUIT_CEILING = "extensions_exhausted"


@dataclass(frozen=True, slots=True)
class _Pursuit:
    """The decision taken at an exhausted budget, and what it rests on.

    ``extension`` is set only when the run continued. Everything else is a stop,
    and ``reason`` is which one — carried into the terminal finding, because
    "the budget ran out" is the same sentence for a run that never tried and a
    run that tried eight times, and those two say opposite things about how much
    more looking is worth.
    """

    reason: str
    unanswered: tuple[Objective, ...] = ()
    extension: BudgetExtension | None = None


# A detector reads (address, incoming, outgoing) stored movements and
# returns findings. It never touches providers — Class F boundary.
Detector = Callable[
    [Address, Sequence["StoredMovement"], Sequence["StoredMovement"]], Sequence[Finding]
]

# Decides whether an asset's provenance is established well enough for a
# heuristic to point at movements denominated in it. Injected rather than
# imported so the engine keeps no dependency on the analysis package.
AssetPredicate = Callable[[AssetFacts], bool]


def _native_only(asset: AssetFacts) -> bool:
    """Fail-closed default: an unwired engine still cannot be fed a forged token."""
    return asset.kind == "native"


def _pool_id(address: Address, asset_id: int, assets: Mapping[int, AssetFacts]) -> str:
    """The pool identity the exit ladder groups a mixer's events by.

    A Tornado-style mixer is a set of fixed-denomination pools, and the ladder
    is written for that: candidates are drawn from the anchor's own pool. The
    fact store does not know a mixer's denomination structure, so inventing
    one would be a guess dressed as data — but it does know the ASSET, and an
    ETH deposit cannot leave as a token withdrawal without a swap that would
    be its own movement. So a pool here is (mixer contract, asset), which is
    the coarsest split we can make out of facts we actually hold.

    The asset id is part of the string and the symbol alone is not, because
    symbols are attacker-chosen: two worthless contracts both calling
    themselves USDC would otherwise merge into one pool and the crowd count —
    which rung 5 turns directly into a confidence — would be wrong in the
    direction that overstates it.
    """
    asset = assets.get(asset_id)
    symbol = asset.symbol if asset is not None else "unknown"
    return f"{address.value}:{symbol}#{asset_id}"


@dataclass(frozen=True, slots=True)
class _MixerFacts:
    """What is stored about one mixer, shaped for the exit ladder.

    The two movement maps exist because a candidate names a ``(tx_hash,
    address_id)`` pair and the graph edge needs the movement row behind it.
    They are kept apart per side: one transaction can carry both a deposit and
    a withdrawal for the same address, and a single map would hand the edge
    the wrong half of it.
    """

    activity: MixerActivity
    deposit_movements: dict[tuple[str, int], int]
    withdrawal_movements: dict[tuple[str, int], int]

    def movement_id(self, direction: Direction, candidate: MixerCandidate) -> int | None:
        """The stored movement a candidate stands for, on the far side."""
        side = (
            self.deposit_movements if direction is Direction.BACKWARD else self.withdrawal_movements
        )
        return side.get((candidate.tx_hash, candidate.address_id))


class InvestigationEngine:
    def __init__(
        self,
        registry: ChainRegistry,
        session_factory: async_sessionmaker[AsyncSession],
        attributor: Attributor,
        *,
        detectors: Sequence[Detector] = (),
        service_detector: Detector | None = None,
        evidence_assets: AssetPredicate | None = None,
        clock: Callable[[], float] = time.monotonic,
        supernode_threshold: int = 50,
        supernode_follow: int = 20,
        page_limit: int = 100,
        engine_version: str = cipherchain.__version__,
        ruleset_version: str = "baseline-2026-08-07",
    ) -> None:
        self._registry = registry
        self._sessions = session_factory
        self._attributor = attributor
        self._detectors = tuple(detectors)
        # Inferring a VASP from behaviour when no label exists.
        self._service_detector = service_detector
        self._evidence_assets = evidence_assets if evidence_assets is not None else _native_only
        self._clock = clock
        self._supernode_threshold = supernode_threshold
        self._supernode_follow = supernode_follow
        self._page_limit = page_limit
        self._engine_version = engine_version
        self._ruleset_version = ruleset_version

    @property
    def registry(self) -> ChainRegistry:
        """The chains this engine can investigate (read-only accessor)."""
        return self._registry

    # ── intake ───────────────────────────────────────────────────────────

    async def start(
        self,
        chain: str,
        address_value: str,
        objectives: Sequence[Objective],
        budgets: Budgets | None = None,
    ) -> uuid.UUID:
        adapter = self._registry.get(chain)  # unknown chain fails before any row exists
        if not objectives:
            raise ValueError("an investigation needs at least one objective")
        # Canonical form is per-chain knowledge and belongs to the adapter;
        # the engine must not know that "0x means lowercase"
        # (REVIEW_FINDINGS.md, chain-agnosticism leak).
        value = adapter.canonical_address(address_value)
        async with self._sessions() as session:
            facts = FactRepository(session)
            investigations = InvestigationRepository(session)
            root_address_id = await facts.get_or_create_address(Address(chain, value))
            row = await investigations.create(
                root_address_id=root_address_id,
                objectives=[str(o) for o in objectives],
                budgets=(budgets or Budgets()).to_dict(),
                engine_version=self._engine_version,
                ruleset_version=self._ruleset_version,
            )
            await investigations.add_address_node(
                row.id,
                root_address_id,
                direction=None,  # the root serves every objective
                hop_distance=0,
                value_share=None,
                discovered_reason="root",
            )
            await session.commit()
            return row.id

    # ── the loop ─────────────────────────────────────────────────────────

    async def run(self, investigation_id: uuid.UUID) -> str:
        token = bind_investigation(str(investigation_id))
        try:
            return await self._run(investigation_id)
        except Exception as exc:
            async with self._sessions() as session:
                await InvestigationRepository(session).set_status(
                    investigation_id, "failed", error=repr(exc)
                )
                await session.commit()
            raise
        finally:
            unbind_investigation(token)

    async def _run(self, investigation_id: uuid.UUID) -> str:
        async with self._sessions() as session:
            investigations = InvestigationRepository(session)
            row = await investigations.get(investigation_id)
            if row is None:
                raise ValueError(f"unknown investigation {investigation_id}")
            objectives = [Objective(o) for o in row.objectives]
            budgets = Budgets.from_dict(row.budgets)
            root_address_id = row.root_address_id
            facts = FactRepository(session)
            root_address = await facts.get_address(root_address_id)
            assert root_address is not None
            root_node = await investigations.get_address_node(investigation_id, root_address_id)
            assert root_node is not None
            root_node_id = root_node.id
            existing_nodes = await investigations.count_nodes(investigation_id)
            prior_spent = dict(row.spent)
            await investigations.set_status(investigation_id, "running")
            await session.commit()

        adapter = self._registry.get(root_address.chain)
        tracker = BudgetTracker(budgets, clock=self._clock)
        tracker.seed_nodes(existing_nodes)
        # Resume carries forward everything already spent (except the per-run
        # wall clock) so a crash/resume cycle cannot grant a fresh api_calls
        # budget and blow past the configured cap (REVIEW_FINDINGS.md #2).
        tracker.seed_spent(prior_spent)
        logger.info(
            "run start: chain=%s root=%s objectives=%s",
            root_address.chain,
            root_address.value,
            [str(o) for o in objectives],
        )

        while True:
            exhausted = tracker.exhausted()
            async with self._sessions() as session:
                facts = FactRepository(session)
                investigations = InvestigationRepository(session)
                if exhausted is not None:
                    # An exhausted budget is a decision, not a terminal: while a
                    # question is open and the frontier still holds work, the
                    # run buys itself another allowance instead of handing back
                    # a partial result somebody has to resume by hand.
                    pursuit = await self._pursue(
                        investigations, investigation_id, exhausted, tracker, objectives
                    )
                    if pursuit.extension is not None:
                        # Committed before the loop spends against the new
                        # limit, so a run killed mid-pursuit still shows what it
                        # granted itself rather than an unexplained overspend.
                        await investigations.update_spent(
                            investigation_id, tracker.spent_snapshot()
                        )
                        await session.commit()
                        logger.info("pursuit: %s", pursuit.extension.statement())
                        continue
                    status = await self._finish_partial(
                        session,
                        investigations,
                        investigation_id,
                        root_address,
                        exhausted,
                        tracker,
                        objectives,
                        pursuit=pursuit,
                    )
                    return status
                claimed = await investigations.claim_frontier(investigation_id, 1)
                if not claimed:
                    return await self._finish_completed(
                        session,
                        facts,
                        investigations,
                        investigation_id,
                        root_address,
                        objectives,
                        tracker,
                    )
                await self._process_node(
                    session,
                    facts,
                    investigations,
                    adapter,
                    investigation_id,
                    root_node_id,
                    root_address_id,
                    claimed[0],
                    objectives,
                    tracker,
                )
                await investigations.update_spent(investigation_id, tracker.spent_snapshot())
                await session.commit()

    # ── one node ─────────────────────────────────────────────────────────

    async def _process_node(
        self,
        session: AsyncSession,
        facts: FactRepository,
        investigations: InvestigationRepository,
        adapter: ChainAdapter,
        investigation_id: uuid.UUID,
        root_node_id: int,
        root_address_id: int,
        node: NodeRow,
        objectives: Sequence[Objective],
        tracker: BudgetTracker,
    ) -> None:
        assert node.address_id is not None
        address = await facts.get_address(node.address_id)
        assert address is not None
        results = await self._attributor.attribute(address)

        for sanctioned in (r for r in results if r.category == CATEGORY_SANCTIONED):
            # Ruling R2: record and continue — the trace follows funds THROUGH.
            await investigations.add_finding(
                investigation_id,
                self._on_speculative_branch(
                    self._sanction_finding(address, node, sanctioned), node
                ),
                subject_address_id=node.address_id,
            )

        mixer = next((r for r in results if r.category == CATEGORY_MIXER), None)
        if mixer is not None and node.hop_distance == 0:
            # Every other node is attributed the moment it is discovered, and
            # that is where the mixer contact is recorded. The root is the one
            # node that never passes through discovery-time attribution — it is
            # inserted by `start()` — so it files its own contact here. Filing
            # unconditionally would record the same claim twice for every mixer
            # the trace reaches, and there is no uniqueness constraint on
            # findings to catch it.
            await investigations.add_finding(
                investigation_id,
                self._on_speculative_branch(self._mixer_finding(address, node, mixer), node),
                subject_address_id=node.address_id,
            )

        vasp = next((r for r in results if r.category == CATEGORY_VASP), None)
        # A mixer label and a VASP label on one address is a contradiction in
        # the label data, and the mixer reading is the cautious one: closing
        # the branch as an answered endpoint would publish "the funds reached
        # this exchange" about a pool. Mixer wins, as it did when this branch
        # returned first.
        if vasp is not None and mixer is None:
            refs = await path_tx_hashes(session, investigation_id, root_node_id, node.id)
            await investigations.add_finding(
                investigation_id,
                self._on_speculative_branch(
                    self._vasp_finding(address, node, vasp, refs),
                    node,
                    # `_vasp_finding` rewrites its own headline for a
                    # speculative node ("possible previous VASP beyond a
                    # mixer…"), so the generic clause would be said twice.
                    summary_states_it=True,
                ),
                subject_address_id=node.address_id,
            )
            # The ROOT is recorded and then expanded anyway. Terminating at hop 0
            # would answer "where did these funds come from and go?" with "this
            # address is Binance" and stop — true, and useless, since the
            # investigator supplied the address and wants its flows. Every other
            # terminator already guards on hop_distance > 0 for the same reason.
            if node.hop_distance > 0:
                await investigations.set_node_state(node.id, "terminal", reason="vasp")
                logger.info("VASP endpoint at %s (%s): branch closed", address.value, vasp.entity)
                return
            logger.info("root %s is attributed to %s; expanding anyway", address.value, vasp.entity)

        if node.hop_distance >= tracker.budgets.max_depth:
            tracker.note_depth_horizon()
            await investigations.set_node_state(node.id, "terminal", reason="depth_horizon")
            return

        page = await adapter.address_history(address, limit=self._page_limit)
        tracker.charge_api(1)
        if page.next_cursor is not None or page.truncated:
            # v1 reads one page per address by design (Ruling 2). The limit is
            # accepted; hiding it is not — so the shortfall becomes a durable
            # fact that every conclusion drawn from this run can cite.
            #
            # `truncated` is the same shortfall from a provider that cannot
            # offer a cursor at all, and testing only `next_cursor` missed it
            # entirely: the keyless explorer tier reads a fixed number of
            # numbered pages of a Tron address and stops, so a busy address came
            # back with no cursor, no gap, and the report counted it as read in
            # full — while the same address served by TronGrid was counted as
            # cut. Coverage that depends on which provider answered is not
            # coverage.
            await investigations.mark_history_truncated(node.id)
        for gap in page.gaps:
            # A feed no provider could serve is the SAME coverage fact as a cut
            # page — this address was read only in part — and it is the one the
            # keyless fallback tier makes routine, since an exhausted quota is
            # how a trace is meant to degrade rather than stop.
            #
            # The adapter carries the loss out as data precisely because it
            # cannot be inferred from the rows: nothing downstream can see a
            # transfer that was never fetched. Until this line the record
            # dropped it, so a run that lost ``tokentx`` on every address
            # reported "no address was left partially read" over an address
            # funded entirely in USDT — the exact sentence `FeedGap` was added
            # to make impossible.
            await investigations.mark_history_truncated(node.id)
            # AND which feed it was. The line above says this address was read
            # only in part, which is true but is also what a cut page says; it
            # cannot distinguish "there was more history" from "every ETH
            # transfer is here and the USDT ones are not". Only the second
            # sentence tells a reader whether the gap could have hidden the
            # counterparty they are looking for, and until this call the feed's
            # name reached nothing but the log below — which no report and no
            # API response has ever read.
            await investigations.mark_feed_unavailable(node.id, gap.code)
            logger.warning("coverage gap at %s: %s", address.value, gap.summary)
        seen_bridges: set[tuple[str, str]] = set()
        for item in page.items:
            normalized = await adapter.normalize(item)
            await facts.store_movements(
                normalized.tx,
                list(normalized.movements),
                raw_sha256=item.provenance.payload_sha256,
            )
            for hint in normalized.bridge_hints:
                key = (hint.bridge_id, str(hint.direction))
                if key in seen_bridges:
                    continue
                seen_bridges.add(key)
                await investigations.add_finding(
                    investigation_id,
                    self._on_speculative_branch(self._bridge_finding(address, node, hint), node),
                    subject_address_id=node.address_id,
                )
                logger.info(
                    "bridge %s (%s) at %s -> %s",
                    hint.bridge_id,
                    hint.direction,
                    address.value,
                    hint.counterpart_chain or "unknown chain",
                )
        tracker.charge_txs(len(page.items))

        if mixer is not None:
            # The mixer's own history was just read and stored, which is what
            # the exit ladder needs: it sizes an anonymity set out of the pool's
            # contemporaneous events, and with nothing stored it can only ever
            # return the honest nothing.
            #
            # Its counterparties are NOT expanded the ordinary way. Everyone who
            # ever used the pool would enter the graph as a traced hop, which is
            # the false link this whole path exists to avoid — only the ladder's
            # candidates leave here, and they leave marked.
            #
            # The behavioural detectors and the service-endpoint inference are
            # skipped for the same reason they are skipped for a labelled
            # exchange: a sourced claim already says what this address is.
            # "Sweep pattern at a mixer" and "custodial infrastructure, operator
            # unnamed" are both true of every pool ever deployed and neither
            # tells an investigator anything.
            await self._cross_mixer(
                session,
                facts,
                investigations,
                investigation_id,
                root_node_id,
                root_address_id,
                node,
                address,
                mixer,
                tracker,
            )
            return

        await self._run_detectors(facts, investigations, investigation_id, node, address)

        # A hub the trace reached is usually custodial infrastructure. Saying
        # "service endpoint (operator unnamed)" answers the investigator's
        # question — they can subpoena the address — where a bare
        # "high-degree address" tells them nothing. Only reached when no
        # LABEL identified it above: a sourced attribution always wins.
        #
        # It MARKS the node and keeps going (ruling 2026-08-13). Stopping here
        # ended the branch on a guess, when the objective is a NAMED endpoint
        # that may be one hop further out — an unnamed inference is a signpost,
        # not a destination. The finding still stands beside the terminal and
        # still cannot close the objective (`_finish_completed`).
        if node.hop_distance > 0:
            service = await self._assess_service(facts, node, address, results)
            if service is not None:
                await investigations.add_finding(
                    investigation_id,
                    self._on_speculative_branch(service, node),
                    subject_address_id=node.address_id,
                )
                logger.info(
                    "service endpoint inferred at %s: marked, expansion continues", address.value
                )

        directions = [node.direction] if node.direction else [str(o.direction) for o in objectives]
        objective_by_direction = {str(o.direction): o for o in objectives}
        for direction_value in directions:
            direction = Direction(direction_value)
            stored = (
                await facts.movements_to_address(node.address_id, limit=_MOVEMENT_QUERY_LIMIT)
                if direction is Direction.BACKWARD
                else await facts.movements_from_address(
                    node.address_id, limit=_MOVEMENT_QUERY_LIMIT
                )
            )
            if len(stored) >= _MOVEMENT_QUERY_LIMIT:
                # The expansion query is capped, and a full result means there
                # were probably more rows behind it. Counterparties are derived
                # from what this query returned, so anything past the cut is not
                # ranked, not dropped and not counted — it is simply absent, and
                # the supernode guard cannot see it either (a tail of 500 older
                # movements from twenty addresses collapses to twenty
                # counterparties nobody ever hears about).
                #
                # Recorded as a partially-read address, which is exactly what it
                # is. Flagging at ``>=`` treats "exactly ``limit`` rows" as a
                # possible cut: over-disclosing coverage is the only direction
                # this system is allowed to be wrong in.
                await investigations.mark_history_truncated(node.id)
                logger.warning(
                    "coverage gap at %s: the %s expansion query returned its full %d rows, so "
                    "older movements — and any counterparty reachable only through them — were "
                    "never read",
                    address.value,
                    direction,
                    _MOVEMENT_QUERY_LIMIT,
                )
            counterparties = await derive_counterparties(
                facts,
                stored,
                node.address_id,
                direction,
                ranking_assets=await self._ranking_assets(facts, stored),
            )
            if node.hop_distance > 0 and len(counterparties) > self._supernode_threshold:
                # Partial expansion, not refusal (ruling 2026-08-13). Refusing
                # outright also refused the largest flow out of a hub, which is
                # usually the one worth following; every service endpoint is a
                # supernode by construction (60 counterparties vs a threshold of
                # 50), so a hard stop here silently undid the change above.
                # Counterparties arrive ranked by value, so the head is the
                # money. What is NOT followed is stated in the finding.
                followed = counterparties[: self._supernode_follow]
                # Recorded against the NODE as well as stated in the finding.
                # The finding is prose; every counter that decides whether this
                # run was complete — the coverage sentence below, the report's
                # figures, the API — reads the record, and while this cap lived
                # only in prose those counters all read zero and said the trace
                # had explored everything it reached.
                await investigations.mark_expansion_capped(
                    node.id, len(counterparties) - len(followed)
                )
                await investigations.add_finding(
                    investigation_id,
                    self._on_speculative_branch(
                        self._supernode_finding(
                            address, node, len(counterparties), stored, followed=len(followed)
                        ),
                        node,
                    ),
                    subject_address_id=node.address_id,
                )
                logger.info(
                    "supernode at %s: degree %d, following the %d largest by value",
                    address.value,
                    len(counterparties),
                    len(followed),
                )
                counterparties = followed
            objective = objective_by_direction.get(str(direction))
            reason = str(objective) if objective else str(direction)
            for counterparty in counterparties:
                if node.hop_distance + 1 > tracker.budgets.max_depth:
                    tracker.note_depth_horizon()
                    continue
                if counterparty.address_id == root_address_id:
                    # A cycle back to the subject. The root was already expanded
                    # in every objective's direction at hop 0, so re-admitting it
                    # deeper would re-fetch its history and re-file its findings
                    # to reach conclusions already drawn. Record the edge only.
                    await investigations.add_edge(
                        investigation_id,
                        src_node_id=node.id,
                        dst_node_id=root_node_id,
                        movement_id=counterparty.movement_id,
                    )
                    continue
                created = await investigations.add_address_node(
                    investigation_id,
                    counterparty.address_id,
                    direction=direction,
                    hop_distance=node.hop_distance + 1,
                    value_share=counterparty.value,
                    discovered_reason=reason,
                    # Speculation is inherited, and this is the line that does
                    # it. This node was reached by following a real movement out
                    # of its parent — but if the PARENT was a mixer-exit guess,
                    # the whole branch still rests on that guess, and one clean
                    # hop does not launder it. The ancestor's basis travels down
                    # so a report can always name the specific heuristic the
                    # branch depends on, however far out the node sits
                    # (REACHING_THE_VASP.md §3; nodes.speculative_basis).
                    speculative_basis=node.speculative_basis if node.speculative else None,
                )
                if created is not None:
                    tracker.charge_nodes(1)
                    destination = created
                else:
                    existing = await investigations.get_address_node(
                        investigation_id, counterparty.address_id, direction
                    )
                    assert existing is not None
                    destination = existing.id
                await investigations.add_edge(
                    investigation_id,
                    src_node_id=node.id,
                    dst_node_id=destination,
                    movement_id=counterparty.movement_id,
                )
                # Read the label store NOW, not when this node is eventually
                # claimed — it may never be. Only for a node just created, so
                # each is attributed exactly once. AFTER add_edge: the finding's
                # value-path evidence is reconstructed from stored edges, and
                # attributing first would produce a finding with an empty path.
                if created is not None:
                    await self._attribute_on_discovery(
                        session,
                        facts,
                        investigations,
                        investigation_id,
                        root_node_id,
                        counterparty.address_id,
                        direction,
                    )
        await investigations.set_node_state(node.id, "expanded")

    async def _attribute_on_discovery(
        self,
        session: AsyncSession,
        facts: FactRepository,
        investigations: InvestigationRepository,
        investigation_id: uuid.UUID,
        root_node_id: int,
        address_id: int,
        direction: Direction,
    ) -> bool:
        """Draw the conclusions a LABEL alone supports, at discovery time.

        Attribution is a dict lookup over an in-memory index: no provider call,
        no budget (analysis/attribution/store.py). It used to be reachable only
        from ``_process_node``, which runs after a node is claimed and expanded
        at a cost of three provider calls — so a free question sat behind an
        expensive one, and every labelled address still on the frontier when the
        budget died was discarded unread along with the genuinely unexplored
        work. Measured on live traces, runs reported 58-62% behavioural guesses
        as their answer while holding unread 0.9-confidence Binance, Coinbase,
        KuCoin and OKX labels for addresses they had already found
        (docs/research/ATTRIBUTION_AT_DISCOVERY.md).

        Only what the attributor ALONE can decide is decided here. Anything
        resting on the address's own history — the service-endpoint inference,
        the obfuscation detectors, the supernode guard — stays in
        ``_process_node``, where that history is actually read. The finding is
        built by the same builders and carries the same evidence; nothing about
        the taxonomy, ``direction``, ``hop_distance`` or the "answered" gate
        changes. It is the same answer, reached earlier and for nothing.

        A mixer is the one label that records here without closing anything.
        The contact is established by the label alone, so it is filed; whether
        the branch continues past it is decided by the exit ladder, which
        cannot run until the pool's own transactions have been read.

        Returns True when the branch was closed here.
        """
        address = await facts.get_address(address_id)
        assert address is not None
        results = await self._attributor.attribute(address)
        if not results:
            return False
        mixer = next((r for r in results if r.category == CATEGORY_MIXER), None)
        vasp = next((r for r in results if r.category == CATEGORY_VASP), None)
        if mixer is None and vasp is None:
            # Sanctions are deliberately NOT recorded here. They are
            # record-and-continue, so the node stays on the frontier and
            # ``_process_node`` would file the same claim a second time;
            # de-duplicating findings is a separate change (00_PROJECT_STATE.md,
            # "add_finding has no uniqueness constraint").
            return False
        node = await investigations.get_address_node(investigation_id, address_id, direction)
        if node is None:  # pragma: no cover — just created in this transaction
            return False

        if mixer is not None:
            # Recorded here and NOT closed here. The contact is a fact the label
            # alone establishes, so it is filed at discovery for the same reason
            # every other label is: a run that dies on budget with this node
            # still on the frontier must not lose the fact that the money
            # touched a mixer.
            #
            # The CROSSING is a different question and cannot be answered yet.
            # The exit ladder needs the pool's own transactions to size an
            # anonymity set, and at discovery the fact store holds only the one
            # movement that arrived here. So the node stays on the frontier and
            # `_process_node` decides, after reading the mixer's history.
            await investigations.add_finding(
                investigation_id,
                self._on_speculative_branch(self._mixer_finding(address, node, mixer), node),
                subject_address_id=address_id,
            )
            logger.info(
                "mixer %s identified at discovery (%s): contact recorded, crossing decided when "
                "the node is claimed",
                address.value,
                mixer.entity,
            )
            return False

        assert vasp is not None
        refs = await path_tx_hashes(session, investigation_id, root_node_id, node.id)
        await investigations.add_finding(
            investigation_id,
            self._on_speculative_branch(
                self._vasp_finding(address, node, vasp, refs),
                node,
                summary_states_it=True,  # same reason as the `_process_node` site
            ),
            subject_address_id=address_id,
        )
        await investigations.set_node_state(node.id, "terminal", reason="vasp")
        logger.info(
            "VASP %s identified at discovery (%s) %d hop(s) out: answered without expanding",
            address.value,
            vasp.entity,
            node.hop_distance,
        )
        return True

    # ── crossing a mixer ─────────────────────────────────────────────────

    async def _cross_mixer(
        self,
        session: AsyncSession,
        facts: FactRepository,
        investigations: InvestigationRepository,
        investigation_id: uuid.UUID,
        root_node_id: int,
        root_address_id: int,
        node: NodeRow,
        address: Address,
        mixer: AttributionResult,
        tracker: BudgetTracker,
    ) -> None:
        """Run the exit ladder and put its candidates on the frontier, marked.

        The anchor is the movement by which THIS trace reached the pool, and
        which side of the pool it sits on is decided by the objective, not by
        the shape of the data:

            BACKWARD (where did the money come from) → the trace arrived on a
                WITHDRAWAL the pool paid out, and the candidates are deposits
                that went in before it.
            FORWARD (where did it go) → the trace arrived on a DEPOSIT into
                the pool, and the candidates are withdrawals that came out
                after it.

        Getting that backwards enumerates events that cannot possibly be
        related and returns them with the same confidences, the same weakness
        strings and the same shape as a working answer — silently. The ladder
        makes the swap a type error rather than a wrong answer, and the
        anchor's side is asserted in the tests for the same reason.

        What leaves here is never a traced hop. Every candidate becomes a node
        carrying ``speculative_basis`` — the id of the rung that proposed it —
        and the branch beyond inherits it. The mixer node itself stays terminal
        with a ``mixer``-prefixed reason, because the traced branch really did
        end at the pool; what continues is a guess attached to it.
        """
        assert node.address_id is not None
        direction = Direction(node.direction) if node.direction else None
        incoming, outgoing = await facts.movements_around_address(
            node.address_id, limit=_MOVEMENT_QUERY_LIMIT
        )
        anchor = (
            None
            if direction is None
            else self._mixer_anchor(
                await investigations.arriving_movement_ids(node.id),
                incoming if direction is Direction.FORWARD else outgoing,
            )
        )
        if direction is None or anchor is None:
            await investigations.add_finding(
                investigation_id,
                self._on_speculative_branch(
                    self._mixer_stopped_finding(
                        address,
                        node,
                        (
                            "the investigated address is the mixer itself, so there is no "
                            "arriving transaction to trace from"
                            if direction is None
                            else "the movement that reached this mixer is not among the "
                            "stored transactions for it, so no anchor could be identified"
                        ),
                        None,
                    ),
                    node,
                ),
                subject_address_id=node.address_id,
            )
            await investigations.set_node_state(node.id, "terminal", reason=TERMINAL_MIXER)
            logger.info("mixer at %s: no anchor to cross from, branch stops", address.value)
            return

        anchor_party = (
            anchor.to_address_id if direction is Direction.BACKWARD else anchor.from_address_id
        )
        assert anchor_party is not None
        # Every OTHER time this party used the pool on this side. One anchor is
        # crossed; the rest are a gap, and a gap this run knows about has to be
        # in the document rather than only in the ranking that produced it.
        unanchored = self._unanchored_crossings(
            anchor,
            anchor_party,
            incoming if direction is Direction.FORWARD else outgoing,
            direction,
        )
        assets = await facts.asset_facts({m.asset_id for m in (*incoming, *outgoing)})
        pool = _pool_id(address, anchor.asset_id, assets)
        mixer_facts = await self._mixer_facts(
            facts, address, node.address_id, anchor_party, incoming, outgoing, assets
        )
        if direction is Direction.BACKWARD:
            result = trace_back_from_withdrawal(
                MixerWithdrawal(
                    tx_hash=anchor.tx_hash,
                    address_id=anchor_party,
                    pool=pool,
                    denomination=anchor.amount,
                    timestamp=anchor.timestamp,
                    gas_price=anchor.gas_price,
                ),
                mixer_facts.activity,
            )
        else:
            result = trace_forward_from_deposit(
                MixerDeposit(
                    tx_hash=anchor.tx_hash,
                    address_id=anchor_party,
                    pool=pool,
                    denomination=anchor.amount,
                    timestamp=anchor.timestamp,
                    gas_price=anchor.gas_price,
                ),
                mixer_facts.activity,
            )

        if not result.candidates:
            await investigations.add_finding(
                investigation_id,
                self._on_speculative_branch(
                    self._mixer_stopped_finding(
                        address,
                        node,
                        "no exit candidate could be proposed",
                        result,
                        unanchored=unanchored,
                    ),
                    node,
                ),
                subject_address_id=node.address_id,
            )
            await investigations.set_node_state(node.id, "terminal", reason=TERMINAL_MIXER)
            logger.info(
                "mixer at %s (%s): ladder proposed nothing, branch stops",
                address.value,
                mixer.entity,
            )
            return

        followed, skipped = await self._enqueue_mixer_candidates(
            session,
            facts,
            investigations,
            investigation_id,
            root_node_id,
            root_address_id,
            node,
            direction,
            result,
            mixer_facts,
            tracker,
        )
        if not followed:
            # The ladder had something and the engine could not take it — a
            # different fact from "nothing was there", and the reader needs the
            # one that is true.
            await investigations.add_finding(
                investigation_id,
                self._on_speculative_branch(
                    self._mixer_stopped_finding(
                        address,
                        node,
                        f"all {sum(skipped.values())} proposed exit(s) were dropped "
                        "before expansion",
                        result,
                        skipped=skipped,
                        unanchored=unanchored,
                    ),
                    node,
                ),
                subject_address_id=node.address_id,
            )
            await investigations.set_node_state(node.id, "terminal", reason=TERMINAL_MIXER)
            return

        await investigations.add_finding(
            investigation_id,
            self._on_speculative_branch(
                self._mixer_crossed_finding(
                    address,
                    node,
                    result,
                    followed=followed,
                    skipped=skipped,
                    unanchored=unanchored,
                ),
                node,
            ),
            subject_address_id=node.address_id,
        )
        # Terminal, and deliberately so even though the branch continues: the
        # TRACED trail ends at the pool. Its children hang off a terminal node
        # because they were not reached by following value out of it.
        await investigations.set_node_state(node.id, "terminal", reason=TERMINAL_MIXER_CROSSED)
        logger.info(
            "mixer crossed at %s (%s) by %s: %d speculative branch(es) followed, %d dropped",
            address.value,
            mixer.entity,
            result.rung,
            len(followed),
            sum(skipped.values()),
        )

    @staticmethod
    def _mixer_anchor(
        arriving: Collection[int],
        side: Sequence[StoredMovement],
    ) -> StoredMovement | None:
        """The movement by which THIS trace reached the pool.

        Read off the graph edges rather than guessed from the pool's traffic:
        an edge into this node is precisely "the trace arrived here by that
        movement", and a mixer has thousands of other movements that look
        exactly like it. Picking one of those would anchor the ladder to a
        stranger's transaction and every candidate after it would be about
        somebody else's money.

        ``arriving`` is the movement ids on edges into the pool node
        (``arriving_movement_ids``). ``side`` is the pool's movements on the
        side the objective requires — outgoing (a withdrawal) when tracing
        backward, incoming (a deposit) when tracing forward — so an edge
        pointing the wrong way cannot become the anchor even if it exists.
        Direction is therefore already spent by the caller in choosing
        ``side``; taking it again here would be a second chance to get it
        wrong.
        """
        if not arriving:
            return None
        reached = [movement for movement in side if movement.id in arriving]
        if not reached:
            return None
        # Largest first, as everywhere else in this engine: when a trace
        # touched a pool several times, the biggest movement is the one the
        # investigation is about. tx_hash breaks the tie so two runs agree.
        return max(reached, key=lambda m: (m.amount, m.tx_hash))

    @staticmethod
    def _unanchored_crossings(
        anchor: StoredMovement,
        anchor_party_id: int,
        side: Sequence[StoredMovement],
        direction: Direction,
    ) -> int:
        """How many other times this party touched the pool on the anchor's side.

        One crossing is run per mixer NODE, anchored on a single movement —
        ``derive_counterparties`` collapses every movement between two
        addresses into one edge carrying the largest, and ``_mixer_anchor``
        takes the largest of those. That is a defensible ranking and a silent
        one: a subject who deposits ten times into a fixed-denomination pool
        gets one crossing, and the crossing finding's "0 not followed" then
        reads as "there was nothing else", when nine further deposits — each
        with its own window and its own crowd — were never enumerated.

        Counted in distinct transactions rather than movements, because that is
        the event identity the exit ladder uses; two movements of one
        transaction to the same party are one deposit, not two.

        This is a coverage fact, not a candidate cap, so it is reported on the
        crossing rather than folded into ``skipped``: nothing was proposed and
        dropped here — the proposal was never made.
        """
        # Forward the party deposited, so it is the SENDER of every movement on
        # this side; backward it withdrew, so it is the recipient. Same flip the
        # anchor itself takes, one line up in ``_cross_mixer``.
        forward = direction is Direction.FORWARD
        touched = {
            movement.tx_hash
            for movement in side
            if (movement.from_address_id if forward else movement.to_address_id) == anchor_party_id
        }
        return len(touched - {anchor.tx_hash})

    @staticmethod
    async def _mixer_facts(
        facts: FactRepository,
        address: Address,
        mixer_address_id: int,
        anchor_party_id: int,
        incoming: Sequence[StoredMovement],
        outgoing: Sequence[StoredMovement],
        assets: Mapping[int, AssetFacts],
    ) -> _MixerFacts:
        """Shape the pool's stored movements into the ladder's inputs.

        Incoming movements are deposits and outgoing ones are withdrawals —
        the mixer's side of each is fixed, so the party is always the OTHER
        endpoint. Halves with no counterparty (UTXO inputs and outputs) are
        dropped: the ladder matches parties, and a half with no party cannot
        be one.

        Rung 2 needs transfers between the anchor's party and a far-side party
        that did NOT run through the pool, so those are read from the anchor
        party's own stored movements with anything touching the mixer removed —
        otherwise the pool itself is the link, and it links everyone to
        everyone.

        Deduplicated per ``(tx_hash, party)`` keeping the largest amount: one
        transaction can carry several movements to the same address, and the
        ladder treats a tx hash as an event identity.
        """
        deposits: dict[tuple[str, int], MixerDeposit] = {}
        deposit_movements: dict[tuple[str, int], int] = {}
        for movement in incoming:
            party = movement.from_address_id
            if party is None or party == mixer_address_id:
                continue
            key = (movement.tx_hash, party)
            held = deposits.get(key)
            if held is not None and held.denomination >= movement.amount:
                continue
            deposits[key] = MixerDeposit(
                tx_hash=movement.tx_hash,
                address_id=party,
                pool=_pool_id(address, movement.asset_id, assets),
                denomination=movement.amount,
                timestamp=movement.timestamp,
                gas_price=movement.gas_price,
            )
            deposit_movements[key] = movement.id

        withdrawals: dict[tuple[str, int], MixerWithdrawal] = {}
        withdrawal_movements: dict[tuple[str, int], int] = {}
        for movement in outgoing:
            party = movement.to_address_id
            if party is None or party == mixer_address_id:
                continue
            key = (movement.tx_hash, party)
            kept = withdrawals.get(key)
            if kept is not None and kept.denomination >= movement.amount:
                continue
            withdrawals[key] = MixerWithdrawal(
                tx_hash=movement.tx_hash,
                address_id=party,
                pool=_pool_id(address, movement.asset_id, assets),
                denomination=movement.amount,
                timestamp=movement.timestamp,
                gas_price=movement.gas_price,
            )
            withdrawal_movements[key] = movement.id

        party_in, party_out = await facts.movements_around_address(
            anchor_party_id, limit=_MOVEMENT_QUERY_LIMIT
        )
        interactions: dict[tuple[str, int, int], DirectInteraction] = {}
        for movement in (*party_in, *party_out):
            source, target = movement.from_address_id, movement.to_address_id
            if source is None or target is None:
                continue
            if mixer_address_id in (source, target):
                continue
            interactions[(movement.tx_hash, source, target)] = DirectInteraction(
                tx_hash=movement.tx_hash, from_address_id=source, to_address_id=target
            )

        return _MixerFacts(
            activity=MixerActivity(
                deposits=tuple(deposits.values()),
                withdrawals=tuple(withdrawals.values()),
                interactions=tuple(interactions.values()),
                # The pool is not a party to its own crossing. Without this a
                # router appears on both sides and wins every identity rung
                # against every user it ever served.
                mixer_address_ids=frozenset({mixer_address_id}),
            ),
            deposit_movements=deposit_movements,
            withdrawal_movements=withdrawal_movements,
        )

    async def _enqueue_mixer_candidates(
        self,
        session: AsyncSession,
        facts: FactRepository,
        investigations: InvestigationRepository,
        investigation_id: uuid.UUID,
        root_node_id: int,
        root_address_id: int,
        node: NodeRow,
        direction: Direction,
        result: MixerExitResult,
        mixer_facts: _MixerFacts,
        tracker: BudgetTracker,
    ) -> tuple[list[MixerCandidate], dict[str, int]]:
        """Put the ladder's candidates on the frontier as speculative nodes.

        Returns what was followed and a count of what was not, per reason.
        Every cap here is reported rather than applied silently: a mixer
        multiplies the frontier by the size of a crowd, and an investigator who
        cannot see that the subject's own exit may be among the ones dropped
        has been handed a shortlist that looks exhaustive.
        """
        skipped: dict[str, int] = {}

        def drop(reason: str, count: int = 1) -> None:
            skipped[reason] = skipped.get(reason, 0) + count

        candidates = list(result.candidates)
        if len(candidates) > MAX_FOLLOW:
            # The ladder already caps its own fallback rung; the identity rungs
            # do not, because "how many is too many" is a budget question and
            # the ladder holds no budget.
            drop("beyond the follow cap", len(candidates) - MAX_FOLLOW)
            candidates = candidates[:MAX_FOLLOW]

        # No depth guard here, deliberately. `_process_node` returns before the
        # crossing whenever ``hop_distance >= max_depth``, so a candidate at
        # ``hop_distance + 1`` is always inside the horizon by the time control
        # arrives — a guard would be a branch no input can reach, and an
        # unreachable guard is a claim the tests cannot check.
        followed: list[MixerCandidate] = []
        for candidate in candidates:
            if tracker.nodes_created >= tracker.budgets.max_nodes:
                drop("no node budget left")
                continue
            if candidate.address_id == root_address_id:
                # The subject's own address on the far side of the pool. It is
                # already the root of this trace, and re-admitting it deeper
                # would restate conclusions the run has drawn — as a guess.
                drop("the candidate is the investigated address itself")
                continue
            created = await investigations.add_address_node(
                investigation_id,
                candidate.address_id,
                direction=direction,
                hop_distance=node.hop_distance + 1,
                value_share=candidate.value,
                discovered_reason=f"mixer-exit:{candidate.heuristic}",
                speculative_basis=candidate.heuristic,
            )
            if created is None:
                existing = await investigations.get_address_node(
                    investigation_id, candidate.address_id, direction
                )
                if existing is None or not existing.speculative:
                    # This address is already in the graph on a TRACED path.
                    # Hanging a mixer-exit edge onto it would let a path search
                    # route the clean node's evidence through the pool, and the
                    # resulting finding would cite a value path that does not
                    # exist. The traced node keeps its provenance; the guess is
                    # dropped and counted.
                    drop("already reached by a traced path")
                    continue
                destination = existing.id
            else:
                tracker.charge_nodes(1)
                destination = created
            await investigations.add_edge(
                investigation_id,
                src_node_id=node.id,
                dst_node_id=destination,
                movement_id=mixer_facts.movement_id(direction, candidate),
            )
            if created is not None:
                # Same reasoning as ordinary discovery: the label store is free
                # to read and these nodes are claimed LAST (the frontier orders
                # clean work ahead of guesses), so a budget death would
                # otherwise discard every one of them unread.
                await self._attribute_on_discovery(
                    session,
                    facts,
                    investigations,
                    investigation_id,
                    root_node_id,
                    candidate.address_id,
                    direction,
                )
            followed.append(candidate)
        return followed, skipped

    async def _assess_service(
        self,
        facts: FactRepository,
        node: NodeRow,
        address: Address,
        results: Sequence[AttributionResult] = (),
    ) -> Finding | None:
        """Infer 'this is custodial infrastructure' from behaviour alone.

        The detector is direction-agnostic (it only sees movements), so the
        engine stamps the objective's direction here — without it the finding
        would never surface as the answer to "nearest previous/next VASP".
        """
        if self._service_detector is None or node.address_id is None:
            return None
        # A sourced label saying "this is a DEX router" beats a behavioural guess
        # saying "this looks custodial". The detector reads counterparty degree,
        # and a busy settlement contract has an exchange's degree with none of
        # the custody — CoW Protocol's GPv2Settlement was inferred as "custodial
        # infrastructure such as an exchange" on a real theft trace.
        infrastructure = next((r for r in results if r.category == CATEGORY_INFRASTRUCTURE), None)
        if infrastructure is not None:
            logger.info(
                "service inference suppressed at %s: labelled %s",
                address.value,
                infrastructure.entity,
            )
            return None
        incoming, outgoing = await facts.movements_around_address(
            node.address_id, limit=_MOVEMENT_QUERY_LIMIT
        )
        # Counterparty degree is as forgeable as any other token-derived signal:
        # a worthless token can name thousands of "senders" that never signed.
        (incoming, outgoing), _ = await self._evidence_grade(facts, incoming, outgoing)
        found = self._service_detector(address, incoming, outgoing)
        if not found:
            return None
        finding = found[0]
        if node.direction is None:
            return finding
        return replace(finding, direction=Direction(node.direction))

    async def _ranking_assets(
        self, facts: FactRepository, movements: Sequence[StoredMovement]
    ) -> frozenset[int]:
        """Assets allowed to influence which branch is explored FIRST.

        Traversal order was steerable by anyone willing to pay gas: a token
        contract can emit transfers naming any amount, so spraying a victim with
        a worthless token at an astronomical nominal amount pushed the real
        trail below the spam until the budget ran out. Measured on a live Bybit
        trace, the five highest-ranked unexplored branches were all unverified
        tokens at ~1e26 while the largest genuine movement was 3.22e20 wei.

        This limits RANKING only. Unverified-asset counterparties are still
        discovered, still stored, still explored, and still appear in evidence —
        the provenance floor governs what may be CLAIMED, and hiding real events
        from the graph would be its own dishonesty.
        """
        known = await facts.asset_facts({m.asset_id for m in movements})
        return frozenset(
            asset_id for asset_id, asset in known.items() if self._evidence_assets(asset)
        )

    async def _evidence_grade(
        self, facts: FactRepository, *movement_sets: Sequence[StoredMovement]
    ) -> tuple[list[list[StoredMovement]], int]:
        """Keep only movements whose asset can support an inference.

        A token contract may emit transfer events between addresses that never
        signed anything, so a receive-and-forward pattern in an attacker-deployed
        token can be manufactured against any victim for the price of gas. Such
        movements are still stored and still expand the graph — they are real
        events — but they may not be the evidence a heuristic points at.
        """
        asset_ids = {m.asset_id for movements in movement_sets for m in movements}
        known = await facts.asset_facts(asset_ids)
        kept: list[list[StoredMovement]] = []
        excluded = 0
        for movements in movement_sets:
            allowed: list[StoredMovement] = []
            for movement in movements:
                asset = known.get(movement.asset_id)
                if asset is not None and self._evidence_assets(asset):
                    allowed.append(movement)
                else:
                    excluded += 1
            kept.append(allowed)
        return kept, excluded

    async def _run_detectors(
        self,
        facts: FactRepository,
        investigations: InvestigationRepository,
        investigation_id: uuid.UUID,
        node: NodeRow,
        address: Address,
    ) -> None:
        """Class F analysis on freshly stored data — no provider access."""
        if not self._detectors:
            return
        assert node.address_id is not None
        # Direction is part of node identity, so an address reached both backward
        # and forward is processed twice. Its movement pattern is the same both
        # times, and filing it twice would read as two independent observations.
        if await investigations.has_processed_sibling(
            investigation_id, node.address_id, exclude_node_id=node.id
        ):
            return
        incoming, outgoing = await facts.movements_around_address(
            node.address_id, limit=_MOVEMENT_QUERY_LIMIT
        )
        (incoming, outgoing), excluded = await self._evidence_grade(facts, incoming, outgoing)
        if excluded:
            logger.info(
                "asset floor: %d movement(s) at %s ignored as evidence (unverified asset)",
                excluded,
                address.value,
            )
        for detector in self._detectors:
            for finding in detector(address, incoming, outgoing):
                await investigations.add_finding(
                    investigation_id,
                    self._on_speculative_branch(finding, node),
                    subject_address_id=node.address_id,
                )

    # ── pursuit ──────────────────────────────────────────────────────────

    @staticmethod
    async def _pursue(
        investigations: InvestigationRepository,
        investigation_id: uuid.UUID,
        exhausted_budget: str,
        tracker: BudgetTracker,
        objectives: Sequence[Objective],
    ) -> _Pursuit:
        """At an exhausted budget: buy more of it, or say why not.

        The run that prompted this stopped at 400 nodes with the forward
        objective unanswered and 382 addresses queued, and a human had to issue
        a resume with a bigger number to reach OKX. Nothing about that decision
        needed a human — the three facts it rests on are all in the record.

        Extension needs ALL of:

        * an objective with no NAMED endpoint. Answered is
          ``_Endpoints.of(findings).named`` — the same gate ``_finish_completed``
          closes an objective with, called rather than restated, because two
          definitions of "answered" drift and the one that drifts is the one
          that lets a behavioural guess or a branch past a mixer end the run;
        * frontier work left. With nothing queued there is nothing an allowance
          could buy: the trail ran out, not the budget;
        * an extension left under the ceiling.

        Checked in that order, so a run whose questions are all answered stops
        as "answered" even when it also happens to be out of extensions —
        pursuit exists for open questions, and it must never keep spending after
        the last one closes.

        ``pursue_until_answered`` is read FIRST and short-circuits every query
        below, and that line is load-bearing rather than an optimisation. Without
        it a pursuit-off run still fell through the three tests and came back
        holding ``objectives_answered`` or ``frontier_empty``, and
        ``_finish_partial`` prints a different terminal for each — so the escape
        hatch that promises "exactly the run this engine made before pursuit
        existed" silently rewrote two of its four sentences, and the branch that
        exists to say "an EARLIER run pursued, this one did not" was unreachable
        in the whole suite. A caller turns pursuit off to get a predictable spend
        AND a document that reads as it always did; a stop reason invented by
        machinery that was switched off is neither.
        """
        if not tracker.budgets.pursue_until_answered:
            return _Pursuit(reason=PURSUIT_DISABLED)
        findings = await investigations.list_findings(investigation_id)
        named = _Endpoints.of(findings).named
        unanswered = tuple(o for o in objectives if o.direction not in named)
        if not unanswered:
            return _Pursuit(reason=PURSUIT_ANSWERED)
        if not await investigations.count_frontier(investigation_id):
            return _Pursuit(reason=PURSUIT_FRONTIER_EMPTY, unanswered=unanswered)
        if not tracker.may_extend():
            return _Pursuit(reason=PURSUIT_CEILING, unanswered=unanswered)
        return _Pursuit(
            reason=PURSUIT_EXTENDED,
            unanswered=unanswered,
            extension=tracker.extend(exhausted_budget, [str(o) for o in unanswered]),
        )

    # ── end states ───────────────────────────────────────────────────────

    @staticmethod
    async def _coverage(
        investigations: InvestigationRepository,
        investigation_id: uuid.UUID,
        tracker: BudgetTracker,
    ) -> tuple[Evidence, bool]:
        """The single place this engine states what it did and did not examine.

        Every limit that closed a branch without exploring it reports here —
        truncated histories, the depth horizon, an undrained frontier — so a
        conclusion carries one coverage statement instead of a sentence per
        limit. It is an ``ENGINE_OBSERVATION``: verifiable against the
        investigation record, not against the chain (Ruling 4).

        Returns the evidence and whether coverage was complete, so that no
        summary can claim the trace read everything while this says otherwise.
        """
        truncated = await investigations.count_truncated_histories(investigation_id)
        horizon = await investigations.count_nodes_terminated_for(investigation_id, "depth_horizon")
        unexplored = await investigations.count_frontier(investigation_id)
        crossed = await investigations.count_nodes_terminated_for(
            investigation_id, TERMINAL_MIXER_CROSSED
        )
        stopped_at_mixer = await investigations.count_nodes_terminated_for(
            investigation_id, TERMINAL_MIXER
        )
        capped_nodes, capped_drops = await investigations.count_capped_expansions(investigation_id)
        parts = [f"{tracker.txs_normalized} transaction(s) examined"]
        # Said here and not only in the terminal, because this sentence travels
        # with EVERY conclusion the run files. A run that granted itself eight
        # allowances to reach an exchange is a different run from one that found
        # it inside the budget the operator authorised, and the reader deciding
        # how much weight the answer carries is entitled to know which they hold.
        extensions = tracker.extensions()
        if extensions:
            parts.append(
                f"{len(extensions)} budget extension(s) were granted to keep pursuing an "
                f"unanswered objective ({extension_summary(extensions)})"
            )
        # Mixer crossings belong in the coverage sentence and not only in their
        # own findings, because this is the paragraph a reader checks to decide
        # how much of the answer to trust. A run whose only route to an endpoint
        # ran through a pool must not be able to present the same coverage
        # statement as one that never touched one.
        if crossed:
            parts.append(
                f"{crossed} mixer(s) were crossed on a heuristic — every branch past them is "
                "speculative and may belong to an unrelated party"
            )
        if stopped_at_mixer:
            parts.append(
                f"{stopped_at_mixer} branch(es) stopped at a mixer with no exit candidate to follow"
            )
        if truncated:
            # Cause-neutral on purpose: three different limits set this flag —
            # a page with more behind it, an acquisition feed no provider could
            # serve, and an expansion query that came back full — and a
            # sentence naming only the first would be false for the other two.
            parts.append(
                f"{truncated} address(es) were read only in part — a cut page, a feed no "
                "provider could serve, or more stored movements than one query returns; "
                "the unread part was never examined"
            )
        if horizon:
            parts.append(
                f"{horizon} address(es) reached but never expanded, beyond the depth horizon"
            )
        if capped_nodes:
            parts.append(
                f"{capped_nodes} high-degree address(es) were expanded only in part — "
                f"{capped_drops} counterparty branch(es) were reached and never followed"
            )
        if unexplored:
            parts.append(f"{unexplored} frontier address(es) never expanded")
        # A mixer counts against completeness whichever way it went. Crossed,
        # the pool's own counterparties were never explored as traced hops;
        # stopped at, the branch ended without being followed. Either way "no
        # address was left partially read" would be false, and that sentence is
        # what a reader leans on when deciding the trail really does end here.
        #
        # A capped expansion counts for the same reason and used not to count at
        # all: the supernode guard follows the largest counterparties by value
        # and abandons the rest, so a run that dropped forty branches printed
        # "no address was left partially read" — in the same section as the
        # finding that said forty were reached and never explored.
        complete = not (
            truncated or horizon or unexplored or crossed or stopped_at_mixer or capped_nodes
        )
        if complete:
            parts.append("no address was left partially read")
        if unexplored:
            # A reader seeing a large unverified movement low in the report
            # should know it was DEPRIORITISED, not overlooked. Ranking on
            # unverified assets would let anyone willing to pay gas choose what
            # this trace examined first.
            parts.append(
                "exploration order ranks by value in verified assets only, so a large "
                "movement in an unverified token is explored late rather than first"
            )
        return Evidence(kind=EvidenceKind.ENGINE_OBSERVATION, summary="; ".join(parts)), complete

    async def _finish_completed(
        self,
        session: AsyncSession,
        facts: FactRepository,
        investigations: InvestigationRepository,
        investigation_id: uuid.UUID,
        root_address: Address,
        objectives: Sequence[Objective],
        tracker: BudgetTracker,
    ) -> str:
        findings = await investigations.list_findings(investigation_id)
        # Only a SOURCED endpoint answers the objective. `service-endpoint@1`
        # also files a VASP_ENDPOINT, but it infers a ROLE from behaviour and
        # says so itself — "operator unnamed". Letting that close the objective
        # suppressed the honest "trace exhausted" terminal, so an investigator
        # reading the report saw a confident endpoint where the tool had in fact
        # failed to name anyone. A finding earns "answered" by carrying a
        # third-party claim; a behavioural inference stands beside the terminal,
        # not instead of it.
        #
        # An endpoint on the far side of a mixer does not close it either, and
        # for the same reason one layer along: the label is sourced, but the
        # link from the subject to the labelled address is a heuristic pick out
        # of a crowd. Letting it satisfy the objective would delete the only
        # sentence in the report that says so.
        endpoints = _Endpoints.of(findings)
        root_id = await facts.get_or_create_address(root_address)
        coverage, complete = await self._coverage(investigations, investigation_id, tracker)
        # "The trail ends here" and "the trail ends here as far as I looked" are
        # different answers, and only one of them is safe to act on.
        reach = (
            "every address the trace reached was explored within budget"
            if complete
            else "but the trace did not read everything it could reach — see coverage"
        )
        for objective in objectives:
            if objective.direction in endpoints.named:
                continue
            summary = (
                f"{endpoints.shortfall(objective.direction, objective, exhausted=True)}; {reach}"
            )
            await investigations.add_finding(
                investigation_id,
                Finding(
                    kind=FindingKind.TERMINAL,
                    subject=root_address,
                    summary=summary,
                    confidence=1.0,
                    direction=objective.direction,
                    evidence=(
                        Evidence(
                            kind=EvidenceKind.ENGINE_OBSERVATION,
                            summary="the explored frontier ran dry within budgets",
                        ),
                        coverage,
                    ),
                ),
                subject_address_id=root_id,
            )
        await investigations.set_status(investigation_id, "completed")
        await investigations.update_spent(investigation_id, tracker.spent_snapshot())
        await session.commit()
        logger.info("completed: %s finding(s)", len(findings))
        return "completed"

    async def _finish_partial(
        self,
        session: AsyncSession,
        investigations: InvestigationRepository,
        investigation_id: uuid.UUID,
        root_address: Address,
        exhausted_budget: str,
        tracker: BudgetTracker,
        objectives: Sequence[Objective] = (),
        *,
        pursuit: _Pursuit | None = None,
    ) -> str:
        """The run stopped on a budget. ``pursuit`` says whether it tried not to.

        Four stops wear the same exhausted budget and mean four different things,
        so each gets its own sentence:

        * pursuit off (or never consulted) — the historical statement, unchanged;
        * every objective answered — the run stopped because there was nothing
          left to ask, not because it ran short;
        * nothing on the frontier — an allowance would have bought nothing;
        * the extension ceiling — the run kept buying budget and still could not
          name anyone. That is a far stronger statement about the chain than "we
          ran out of allowance", and a report that printed the same sentence for
          both would file the weaker one over the case that earned the stronger.
        """
        facts = FactRepository(session)
        root_id = await facts.get_or_create_address(root_address)
        unexplored = await investigations.count_frontier(investigation_id)
        extensions = tracker.extensions()
        reason = pursuit.reason if pursuit is not None else None
        # A ceiling reached with NOTHING ever granted is the weak stop wearing
        # the strong stop's sentence, which is the confusion these four
        # terminals exist to prevent — inverted, and so in the more dangerous
        # direction. Two settings produce it: ``max_extensions=0``, and a resume
        # whose earlier runs already spent the whole ceiling. Both stopped on
        # the first budget they met, and "after 0 budget extension(s) chasing
        # the unanswered objective(s) () and no extension left" told a reader
        # the trail had been chased when nothing had been bought at all.
        pursued_to_ceiling = reason == PURSUIT_CEILING and bool(extensions)
        if pursued_to_ceiling:
            pursued = (
                f", after {len(extensions)} budget extension(s) chasing the unanswered "
                f"objective(s) ({extension_summary(extensions)}) and no extension left,"
            )
        elif reason == PURSUIT_ANSWERED:
            pursued = ", every objective already answered so nothing further was spent,"
        elif reason == PURSUIT_FRONTIER_EMPTY:
            pursued = ", nothing left on the frontier for a larger budget to reach,"
        elif extensions:
            # Pursuit is off NOW — an earlier run's extensions are still this
            # investigation's spend and are not dropped from the account.
            pursued = f", after {len(extensions)} earlier budget extension(s),"
        else:
            pursued = ""
        sample = await investigations.claim_frontier(investigation_id, 10)
        sample_values: list[str] = []
        for frontier_node in sample:
            if frontier_node.address_id is not None:
                frontier_address = await facts.get_address(frontier_node.address_id)
                if frontier_address is not None:
                    sample_values.append(frontier_address.value)
        await investigations.add_finding(
            investigation_id,
            Finding(
                kind=FindingKind.TERMINAL,
                subject=root_address,
                summary=(
                    f"budget '{exhausted_budget}' exhausted{pursued} with {unexplored} frontier "
                    f"address(es) unexplored — partial result, gaps explicit"
                ),
                confidence=1.0,
                evidence=(
                    Evidence(
                        kind=EvidenceKind.ENGINE_OBSERVATION,
                        summary=(
                            "unexplored frontier sample: "
                            + (", ".join(sample_values) if sample_values else "none recorded")
                        ),
                    ),
                    (await self._coverage(investigations, investigation_id, tracker))[0],
                ),
            ),
            subject_address_id=root_id,
        )
        # A partial run failed to answer its objectives too, and must say so
        # PER DIRECTION. Naming only the budget answers "why did you stop"
        # while leaving "did you find the exchange" unstated — and a reader
        # cannot tell an unanswered direction from one nobody asked about.
        # This became load-bearing when service endpoints stopped terminating
        # branches: runs that used to drain the frontier and complete now more
        # often end on a budget, and the per-objective statement went with them.
        findings = await investigations.list_findings(investigation_id)
        # The same distinctions the completed path draws, from the same place:
        # "we found nothing", "we found custodial infrastructure but could not
        # name its operator", "we named someone but only past a mixer", and the
        # inference that is itself past one. Each tells an investigator to do
        # something different next, and duplicating the sentences here is how
        # the two paths drifted — the completed path learned to distinguish a
        # speculative inference before this one did.
        endpoints = _Endpoints.of(findings)
        for objective in objectives:
            if objective.direction in endpoints.named:
                continue
            # The pursued case earns its own tail. "Not evidence that none
            # exists" is true of both stops, but a run that bought eight extra
            # allowances and still named nobody is telling an investigator
            # something about the chain — a report that left that out would read
            # as a tooling limit where it was a finding about the money.
            # "This investigation", not "the run": the ceiling counts across
            # resumes, so the grants being reported here may belong to an
            # earlier run of the same investigation. Said as "the run" it was
            # false on exactly the resume the ceiling exists to bound.
            chased = (
                f"; this investigation extended its budgets {len(extensions)} time(s) chasing "
                "this objective and still named nobody"
                if pursued_to_ceiling
                else ""
            )
            summary = (
                f"{endpoints.shortfall(objective.direction, objective, exhausted=False)} "
                f"before budget '{exhausted_budget}' was exhausted; {unexplored} address(es) "
                f"remain unexplored, so this is not evidence that none exists{chased}"
            )
            stopped_on = (
                f"the run stopped on budget '{exhausted_budget}' after exhausting all "
                f"{len(extensions)} permitted budget extension(s), not on an exhausted frontier"
                if pursued_to_ceiling
                else f"the run stopped on budget '{exhausted_budget}', not on an exhausted frontier"
            )
            await investigations.add_finding(
                investigation_id,
                Finding(
                    kind=FindingKind.TERMINAL,
                    subject=root_address,
                    summary=summary,
                    confidence=1.0,
                    direction=objective.direction,
                    evidence=(Evidence(kind=EvidenceKind.ENGINE_OBSERVATION, summary=stopped_on),),
                ),
                subject_address_id=root_id,
            )
        await investigations.set_status(investigation_id, "partial")
        await investigations.update_spent(investigation_id, tracker.spent_snapshot())
        await session.commit()
        logger.info(
            "partial: budget %s exhausted after %d extension(s) (%s), %d unexplored",
            exhausted_budget,
            len(extensions),
            reason or "pursuit not consulted",
            unexplored,
        )
        return "partial"

    # ── finding builders ─────────────────────────────────────────────────

    @staticmethod
    def _on_speculative_branch(
        finding: Finding, node: NodeRow, *, summary_states_it: bool = False
    ) -> Finding:
        """Stamp every finding filed against a node past a mixer crossing.

        Applied at the call sites rather than inside each builder, because the
        rule is about the NODE and not about the kind of conclusion: a sweep
        pattern, a bridge crossing, a sanctions hit and an endpoint are all
        equally about an address that may have nothing to do with the case.
        Marking only the endpoint would leave every other finding on the branch
        reading exactly like one drawn from a traced path — which is the hard
        rule this system has, and it says *anywhere*.

        ``summary_states_it`` is for the one builder that has already done this
        work itself: ``_vasp_finding`` rebuilds its whole headline and its path
        evidence for a speculative node, and a second clause bolted onto that
        sentence would only make it worse. It is passed by the CALLER, at the
        two sites that call that builder.

        It used to be inferred from ``finding.kind is VASP_ENDPOINT``, which was
        the same statement about a different thing and was wrong for a reason
        that is easy to miss: ``_vasp_finding`` is not the only source of a
        VASP_ENDPOINT finding. ``service-endpoint@1`` files one too, from
        behaviour, and it knows nothing about mixers — so a "behaves as
        custodial infrastructure such as an exchange" inference drawn on a
        guessed branch reached the report with a summary identical to one drawn
        from a traced path, marked only by an engine observation folded away in
        the evidence list. Keying on the builder rather than on the kind is what
        makes the rule hold for every finding, which is what it says.
        """
        if not node.speculative:
            return finding
        basis = node.speculative_basis or "a mixer-exit heuristic"
        marker = Evidence(
            kind=EvidenceKind.ENGINE_OBSERVATION,
            summary=(
                # The basis sits in brackets immediately after the prefix so a
                # consumer holding only the Finding can recover it
                # (`speculative_basis_of`) until storage carries the column
                # across that boundary.
                f"{SPECULATIVE_EVIDENCE_PREFIX} ({basis}): the node this was filed against "
                f"was proposed by that heuristic and is recorded speculative in the "
                f"investigation graph; it is a lead to check, not a path that was traced"
            ),
        )
        if summary_states_it:
            return replace(finding, evidence=(*finding.evidence, marker))
        return replace(
            finding,
            summary=(
                f"{finding.summary} — seen on a branch past a mixer, which may belong to "
                "an unrelated party"
            ),
            evidence=(*finding.evidence, marker),
        )

    @staticmethod
    def _vasp_finding(
        address: Address, node: NodeRow, result: AttributionResult, refs: tuple[str, ...]
    ) -> Finding:
        # A node downstream of a mixer crossing was not reached by following
        # value; it was reached by a heuristic choosing one member of a crowd.
        # The LABEL is as sourced as any other — this really is that exchange —
        # but the sentence "the funds reached it" is the part that is a guess,
        # so it is the sentence that has to change. Everything below that reads
        # `speculative` exists to stop this finding from being usable as a
        # traced answer while still being usable as a lead.
        speculative = bool(node.speculative)
        basis = node.speculative_basis or "a mixer-exit heuristic"
        evidence = [
            Evidence(
                kind=EvidenceKind.THIRD_PARTY_CLAIM,
                summary=f"{result.entity} labeled '{result.category}'",
                source=result.source,
                source_date=result.source_date,
                confidence=result.confidence,
            )
        ]
        if refs:
            evidence.append(
                Evidence(
                    kind=EvidenceKind.ONCHAIN_FACT,
                    summary=(
                        # The hashes are real; the CHAIN of them is not. Calling
                        # this a value path would be the exact failure the
                        # speculative flag exists to prevent, one taxonomy layer
                        # down where nobody would look for it.
                        f"{len(refs)} transaction(s) on the branch that reached this "
                        "endpoint — the branch is broken at a mixer, so they do not "
                        "form a connected value path"
                        if speculative
                        else f"value path over {len(refs)} transaction(s) links root to endpoint"
                    ),
                    refs=refs,
                )
            )
        direction = Direction(node.direction) if node.direction else None
        hops = f" {node.hop_distance} hop(s) away" if node.hop_distance else " (root itself)"
        # A customer intake address and the operator's own collector are both
        # "the exchange", and they answer different questions: the first names an
        # ACCOUNT the operator can identify, the second names only the operator.
        # Carrying that in the entity string alone would make a report parse
        # prose to tell them apart.
        if result.role is AddressRole.DEPOSIT:
            reached = (
                f"{result.entity} — a customer deposit address, so the operator can "
                "identify the account these funds were credited to"
            )
        elif result.role is AddressRole.OPERATIONAL:
            reached = f"{result.entity} — an operator-controlled wallet"
        else:
            reached = result.entity
        side = "previous" if direction is Direction.BACKWARD else "next"
        if direction is None:
            summary = f"root address is attributed to VASP {reached}"
        elif speculative:
            # NOT "nearest". The word states a measured distance along a traced
            # path, and there is no traced path here — saying it would put a
            # guess in the same sentence shape as an answer, which is the one
            # thing following a mixer is not allowed to do.
            summary = (
                f"possible {side} VASP beyond a mixer: {reached}{hops} — reached on a "
                f"speculative branch proposed by {basis}; the funds may never have gone "
                f"here at all"
            )
        else:
            summary = f"nearest {side} VASP: {reached}{hops}"
        return Finding(
            kind=FindingKind.VASP_ENDPOINT,
            subject=address,
            summary=summary,
            confidence=result.confidence,
            direction=direction,
            evidence=tuple(evidence),
        )

    @staticmethod
    def _bridge_finding(address: Address, node: NodeRow, hint: BridgeHint) -> Finding:
        """Value crossing a bridge is an ANSWER, not a dead end.

        "The trail continues on Polygon via the PoS bridge" tells an
        investigator where to look next; a bare terminal would not. The
        crossing itself is an on-chain fact; that it *is* a bridge rests on
        the registry's sourced claim, so both are recorded separately.
        """
        heading = f" toward {hint.counterpart_chain}" if hint.counterpart_chain else ""
        entering = hint.direction is BridgeDirection.DEPOSIT
        return Finding(
            kind=FindingKind.BRIDGE_CROSSING,
            subject=address,
            summary=(
                f"value {'entered' if entering else 'arrived from'} bridge "
                f"'{hint.bridge_id}'{heading} — the trail continues off this chain"
            ),
            confidence=0.8,
            direction=Direction(node.direction) if node.direction else None,
            evidence=(
                Evidence(
                    kind=EvidenceKind.ONCHAIN_FACT,
                    summary="transaction moves value against the bridge contract",
                    refs=hint.refs,
                ),
                Evidence(
                    kind=EvidenceKind.THIRD_PARTY_CLAIM,
                    summary=f"contract registered as bridge '{hint.bridge_id}'",
                    source="bridge-registry",
                    confidence=0.8,
                ),
            ),
        )

    @staticmethod
    def _mixer_finding(address: Address, node: NodeRow, result: AttributionResult) -> Finding:
        """Mixer contact — the sourced claim, and nothing about the outcome.

        This finding is filed the moment the label resolves, before the run
        knows whether anything can be followed past the pool. So it states only
        what the label establishes and what is true either way: the link is
        severed by design, and whatever the trace shows beyond this point is a
        candidate rather than a hop.

        It used to end "the trail cannot be followed through it — CipherChain does
        not attempt de-anonymization", which was accurate while the branch
        stopped here and became a contradiction the moment it did not: a report
        cannot say the trail was cut and then print three addresses past the
        cut. The kind, the confidence and the evidence are untouched — only the
        sentence that stopped being true.
        """
        return Finding(
            kind=FindingKind.MIXER_INTERACTION,
            subject=address,
            summary=(
                f"funds reached a known mixer ({result.entity}); the deposit-to-withdrawal "
                f"link is severed by design, so anything beyond this point is a heuristic "
                f"candidate and not a traced hop"
            ),
            confidence=result.confidence,
            direction=Direction(node.direction) if node.direction else None,
            evidence=(
                Evidence(
                    kind=EvidenceKind.THIRD_PARTY_CLAIM,
                    summary=f"address identified as mixer '{result.entity}'",
                    source=result.source,
                    source_date=result.source_date,
                    confidence=result.confidence,
                ),
            ),
        )

    @staticmethod
    def _stored_crowd_caveat() -> str:
        """Why an anonymity set printed by this engine is a floor, not a size.

        The ladder counts the crowd out of the movements CipherChain has stored for
        the pool, and rung 5 turns that count directly into a confidence
        (1/N, capped). A pool the run has seen 40 transactions of reports a
        much smaller crowd than the pool has, so the arithmetic errs in the one
        direction that matters — it makes a guess look stronger than it is.
        Saying so is the difference between a lead and a misleading number.
        """
        return (
            "the anonymity set was counted from the mixer transactions CipherChain has stored, "
            "not from the pool's full history, so the real crowd is larger and every "
            "candidate is correspondingly weaker than its confidence suggests"
        )

    @staticmethod
    def _unanchored_caveat(unanchored: int) -> str:
        """Why one crossing is not the whole of what this trace did at a pool.

        A fixed-denomination mixer is used repeatedly by design — ten deposits
        of 10 ETH, not one of 100 — and the engine crosses a mixer NODE once,
        anchored on the largest movement that reached it. The others are real
        crossings this run declined to make, and without this sentence the
        finding's "N not followed" count describes only the candidates of the
        one anchor, which reads as the whole picture.
        """
        return (
            f"{unanchored} further transaction(s) between this address and the pool on the "
            "same side were not used as an anchor; each is a separate crossing, with its own "
            "window and its own crowd, whose exits were never enumerated"
        )

    @staticmethod
    def _mixer_stopped_finding(
        address: Address,
        node: NodeRow,
        reason: str,
        result: MixerExitResult | None,
        *,
        skipped: Mapping[str, int] | None = None,
        unanchored: int = 0,
    ) -> Finding:
        """The branch really did end at the pool — recorded as a run fact.

        Kept separate from the contact finding above because they answer
        different questions: that one says the money touched a mixer, this one
        says what the exit ladder was able to do about it. Merging them would
        put an ENGINE_OBSERVATION and a THIRD_PARTY_CLAIM behind one sentence,
        and a reader could no longer tell which half the source vouches for.
        """
        parts = [f"the trail stopped at this mixer: {reason}"]
        if result is not None:
            parts.append(result.observation)
        if skipped:
            parts.append(
                "dropped: " + ", ".join(f"{count} {why}" for why, count in sorted(skipped.items()))
            )
        if unanchored:
            parts.append(InvestigationEngine._unanchored_caveat(unanchored))
        parts.append(InvestigationEngine._stored_crowd_caveat())
        return Finding(
            kind=FindingKind.MIXER_INTERACTION,
            subject=address,
            summary=(
                f"the trail stopped at this mixer — {reason}; "
                "no branch past it is offered, which is not evidence that none exists"
            ),
            confidence=1.0,
            direction=Direction(node.direction) if node.direction else None,
            evidence=(Evidence(kind=EvidenceKind.ENGINE_OBSERVATION, summary="; ".join(parts)),),
        )

    @staticmethod
    def _mixer_crossed_finding(
        address: Address,
        node: NodeRow,
        result: MixerExitResult,
        *,
        followed: Sequence[MixerCandidate],
        skipped: Mapping[str, int],
        unanchored: int = 0,
    ) -> Finding:
        """The crossing itself, with the weakness of what was followed.

        A reader has to be able to see three things from this one finding: that
        the trail crossed a mixer at all, how much of the far side was followed
        against how much was not, and why the branches that were followed might
        belong to somebody else. The last of those is the candidate's own
        ``weakness`` text, carried verbatim — it is written by the rung that
        made the guess, and paraphrasing it here would be this engine restating
        a caveat it did not author.
        """
        dropped = sum(skipped.values())
        detail = [result.observation]
        if skipped:
            detail.append(
                "not followed: "
                + ", ".join(f"{count} {why}" for why, count in sorted(skipped.items()))
            )
        # A count of dropped CANDIDATES answers "how much of this crossing was
        # followed". It does not answer "how many crossings were there", and a
        # reader who is not told the second reads the first as both.
        if unanchored:
            detail.append(InvestigationEngine._unanchored_caveat(unanchored))
        detail.append(InvestigationEngine._stored_crowd_caveat())
        weakness = followed[0].weakness
        # ``rung`` is None only when nothing fired, and then there is nothing
        # to follow — the candidate's own heuristic id is the same string and
        # cannot be absent, so the evidence can always name what produced it.
        rung = result.rung or followed[0].heuristic
        return Finding(
            kind=FindingKind.MIXER_INTERACTION,
            subject=address,
            summary=(
                f"the trail crossed this mixer on a heuristic ({rung}): "
                f"{len(followed)} candidate branch(es) followed as SPECULATIVE, "
                f"{dropped} not followed — every address beyond this point may belong "
                f"to an unrelated party"
            ),
            confidence=1.0,
            direction=Direction(node.direction) if node.direction else None,
            evidence=(
                Evidence(
                    kind=EvidenceKind.ENGINE_OBSERVATION,
                    summary="; ".join(detail),
                ),
                Evidence(
                    kind=EvidenceKind.HEURISTIC_INFERENCE,
                    summary=f"why these branches may be the wrong ones: {weakness}",
                    heuristic=rung,
                    confidence=followed[0].confidence,
                    refs=tuple(sorted({ref for c in followed for ref in c.refs})),
                ),
            ),
        )

    @staticmethod
    def _sanction_finding(address: Address, node: NodeRow, result: AttributionResult) -> Finding:
        return Finding(
            kind=FindingKind.SANCTIONED_ADDRESS,
            subject=address,
            summary=f"address appears in sanctions data ({result.entity}); trace continued",
            confidence=result.confidence,
            direction=Direction(node.direction) if node.direction else None,
            evidence=(
                Evidence(
                    kind=EvidenceKind.THIRD_PARTY_CLAIM,
                    summary=f"listed by {result.source}",
                    source=result.source,
                    source_date=result.source_date,
                    confidence=result.confidence,
                ),
            ),
        )

    @staticmethod
    def _supernode_finding(
        address: Address, node: NodeRow, degree: int, stored: Sequence[object], *, followed: int
    ) -> Finding:
        from cipherchain.storage.repositories import StoredMovement

        # Sorted, not set-ordered: the same stored data must yield the same
        # evidence refs across processes, or the finding can't be byte-replayed
        # (REVIEW_FINDINGS.md, nondeterminism).
        unique_hashes = sorted({m.tx_hash for m in stored if isinstance(m, StoredMovement)})
        refs = tuple(unique_hashes[:10]) or (address.value,)
        skipped = degree - followed
        return Finding(
            kind=FindingKind.TERMINAL,
            subject=address,
            summary=(
                f"high-degree address ({degree} counterparties): followed the {followed} "
                f"largest by value, {skipped} branch(es) not followed"
            ),
            confidence=1.0,
            direction=Direction(node.direction) if node.direction else None,
            evidence=(
                Evidence(
                    kind=EvidenceKind.ONCHAIN_FACT,
                    summary="sample of transactions touching the high-degree address",
                    refs=refs,
                ),
                # What the engine chose to do is not checkable against the
                # chain, only against this run's record (Ruling 4).
                Evidence(
                    kind=EvidenceKind.ENGINE_OBSERVATION,
                    summary=(
                        f"expansion capped at the {followed} highest-value counterparties; "
                        f"{skipped} were reached but never explored"
                    ),
                ),
            ),
        )

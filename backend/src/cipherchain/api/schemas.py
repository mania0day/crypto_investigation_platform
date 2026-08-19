"""Request/response models for the API — presentation only.

These are the wire shapes. Core domain objects are mapped into them at the
edge so the domain model never leaks HTTP concerns and vice versa.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from cipherchain.core.models import Evidence, EvidenceKind, Finding
from cipherchain.investigation.answers import BestEffortFinding, DirectionAnswer, RankedFinding
from cipherchain.investigation.budgets import Budgets
from cipherchain.investigation.objectives import Objective
from cipherchain.storage.repositories import GraphEdge, GraphNode


class BudgetsIn(BaseModel):
    """The limits a caller sets, and what happens when one runs out.

    ``pursue_until_answered`` is on by default and is a ruling, not a tuning
    knob: a run that stops on a cost budget with an objective unanswered and
    hundreds of addresses queued has not answered the question, it has stopped
    paying for it — and the operator was left to notice that and resume by hand.
    A caller who genuinely wants the old behaviour (a fixed, predictable spend)
    turns it off here; ``max_extensions`` moves the ceiling without switching
    pursuit off entirely.
    """

    api_calls: int = Field(default=100, ge=1)
    seconds: float = Field(default=300.0, gt=0)
    max_depth: int = Field(default=6, ge=1)
    max_nodes: int = Field(default=500, ge=1)
    pursue_until_answered: bool = True
    max_extensions: int = Field(default=8, ge=0)

    def to_budgets(self) -> Budgets:
        """One mapping, used by start AND resume.

        Both routes built ``Budgets`` field by field, so every new budget had to
        be added in two places; a resume that silently dropped one would have
        re-run the investigation under a different policy from the one the
        caller asked for and reported success.
        """
        return Budgets(
            api_calls=self.api_calls,
            seconds=self.seconds,
            max_depth=self.max_depth,
            max_nodes=self.max_nodes,
            pursue_until_answered=self.pursue_until_answered,
            max_extensions=self.max_extensions,
        )


class ResumeInvestigationRequest(BaseModel):
    """Fresh budgets for a run that stopped short.

    Budgets are the whole payload: a resume cannot change the subject, the
    chain or the objectives, because those decide what the run MEANS and a
    trace that quietly changed its question halfway would be unciteable.

    Defaulted rather than required so ``POST .../resume`` with no body is
    legal — but the route still refuses a budget the run has already spent,
    since the engine carries prior spend forward and would otherwise exhaust
    on its first check and hand back a second, identical partial result.
    """

    budgets: BudgetsIn = Field(default_factory=BudgetsIn)


class StartInvestigationRequest(BaseModel):
    address: str
    # Omit to detect the chain from the address format. Supply it only to
    # disambiguate a format shared by several chains (any two EVM chains).
    chain: str | None = None
    objectives: list[Objective] = Field(min_length=1)
    budgets: BudgetsIn = Field(default_factory=BudgetsIn)

    @field_validator("address")
    @classmethod
    def _address_is_present(cls, value: str) -> str:
        """Reject a blank address here, where it is a 422.

        Left to the domain it became a 500: `Address` raises `ValueError` on an
        empty value, nothing catches it, and a caller who simply forgot a field
        is told the server broke. Stripping matches the chain detector, which
        already tolerates surrounding whitespace on a pasted address.
        """
        stripped = value.strip()
        if not stripped:
            raise ValueError("address must not be blank")
        return stripped


class StartInvestigationResponse(BaseModel):
    investigation_id: uuid.UUID
    status: str
    chain: str  # echoes the chain used, so the caller sees what was detected


class CoverageOut(BaseModel):
    """What this run did NOT read, as numbers a caller can act on.

    ``status`` alone cannot carry this. ``completed`` means the frontier ran
    dry, which is also the status of a run that hit a page limit on six
    addresses, stopped twenty more at the depth horizon and followed a fifth of
    one hub's counterparties on the way there. A consumer that showed "completed"
    and nothing else — the demo UI, a dashboard, a script writing a case note —
    would present a cut trace as an exhausted one.

    ``complete`` is the single flag those consumers should branch on; the
    counters say which limit bit. Both are the same values the rendered report
    prints, read by ``reporting.collect_coverage``, so the document and the wire
    cannot disagree about one run.
    """

    complete: bool
    addresses_reached: int
    transactions_examined: int | None
    truncated_histories: int
    depth_horizon_stops: int
    unexplored_frontier: int
    capped_expansions: int
    counterparties_dropped: int
    mixer_stops: int
    mixer_crossings: int
    max_depth: int | None
    # How many addresses were read with an acquisition feed nobody could serve,
    # and WHICH feeds those were. Both on the wire, because the count alone
    # cannot tell a caller whether the missing rows were token transfers — and a
    # caller tracing a stablecoin payment needs exactly that to know whether a
    # quiet result means anything. This is the routine state once the keyed
    # provider quotas are spent, so it is normal traffic, not an edge case.
    addresses_missing_feeds: int
    feeds_unavailable: list[str]
    # Every allowance the run granted itself when a cost budget ran out with an
    # objective still unanswered. On the wire because `budgets` echoes what the
    # caller ASKED for and always will: without this a consumer comparing budget
    # to spend would read a run that cost nine times its allowance as a bug in
    # the accounting rather than as the pursuit it was.
    budget_extensions: list[str]

    @classmethod
    def of(cls, coverage: Any) -> CoverageOut:
        return cls(
            complete=coverage.complete,
            addresses_reached=coverage.addresses_reached,
            transactions_examined=coverage.transactions_examined,
            truncated_histories=coverage.truncated_histories,
            depth_horizon_stops=coverage.depth_horizon_stops,
            unexplored_frontier=coverage.unexplored_frontier,
            capped_expansions=coverage.capped_expansions,
            counterparties_dropped=coverage.counterparties_dropped,
            mixer_stops=coverage.mixer_stops,
            mixer_crossings=coverage.mixer_crossings,
            max_depth=coverage.max_depth,
            addresses_missing_feeds=coverage.addresses_missing_feeds,
            feeds_unavailable=list(coverage.feeds_unavailable),
            budget_extensions=list(coverage.budget_extensions),
        )


class InvestigationStatusResponse(BaseModel):
    investigation_id: uuid.UUID
    status: str
    chain: str
    root_address: str
    objectives: list[str]
    budgets: dict[str, Any]
    spent: dict[str, Any]
    engine_version: str
    ruleset_version: str
    error: str | None
    # Non-optional: a status body that could omit its coverage would be read as
    # a complete one every time the field was left out.
    coverage: CoverageOut


class EvidenceOut(BaseModel):
    kind: str
    summary: str
    refs: list[str]
    source: str | None
    # A third-party claim must reach consumers WITH its date, or a fresh
    # attribution is indistinguishable from a years-stale one
    # (REVIEW_FINDINGS.md, presentation-integrity).
    source_date: datetime | None
    heuristic: str | None
    confidence: float | None

    @classmethod
    def of(cls, evidence: Evidence) -> EvidenceOut:
        return cls(
            kind=str(evidence.kind),
            summary=evidence.summary,
            refs=list(evidence.refs),
            source=evidence.source,
            source_date=evidence.source_date,
            heuristic=evidence.heuristic,
            confidence=evidence.confidence,
        )


class FindingOut(BaseModel):
    kind: str
    subject_chain: str
    subject_address: str
    direction: str | None
    summary: str
    confidence: float
    evidence: list[EvidenceOut]

    @classmethod
    def of(cls, finding: Finding) -> FindingOut:
        return cls(
            kind=str(finding.kind),
            subject_chain=finding.subject.chain,
            subject_address=finding.subject.value,
            direction=str(finding.direction) if finding.direction else None,
            summary=finding.summary,
            confidence=finding.confidence,
            evidence=[EvidenceOut.of(e) for e in finding.evidence],
        )


class AnswerEntryOut(BaseModel):
    """One endpoint offered as an answer, with what backs it."""

    address: str
    hop: int
    confidence: float
    # Whether an OPERATOR is named on a citable source. A behavioural inference
    # describes what an address does and never who runs it, and the difference
    # decides whether an investigator can act on it.
    named: bool
    claim: str | None  # the third-party claim's own words, when there is one
    summary: str
    # How this endpoint was REACHED, which is a separate question from what it
    # is: a sourced label stays true on a branch whose path was selected rather
    # than witnessed. Without this on the wire a renderer has nothing to draw
    # the difference from, and a guessed route prints as a traced one.
    # Defaulted so the field can be adopted by one caller at a time.
    speculative: bool = False
    speculative_basis: str | None = None

    @classmethod
    def of(cls, ranked: RankedFinding) -> AnswerEntryOut:
        claim = next(
            (e for e in ranked.finding.evidence if e.kind is EvidenceKind.THIRD_PARTY_CLAIM), None
        )
        return cls(
            address=ranked.finding.subject.value,
            hop=ranked.hop,
            confidence=ranked.finding.confidence,
            named=ranked.named,
            claim=claim.summary if claim else None,
            summary=ranked.finding.summary,
            speculative=ranked.speculative,
            speculative_basis=ranked.speculative_basis,
        )


class BestEffortOut(AnswerEntryOut):
    """The lead offered where a direction produced no answer at all.

    ``weakness`` is non-nullable and rejects the empty string, mirroring
    ``BestEffortFinding`` in the domain. That is the property the wire model
    exists to preserve: this entry is rendered in the HEADLINE position
    (REACHING_THE_VASP.md §4, so a weak run still hands its reader a name to
    act on), and a headline whose caveat could arrive as ``null`` would be a
    guess printed exactly like a traced answer.
    """

    weakness: str = Field(min_length=1)

    @classmethod
    def of_lead(cls, lead: BestEffortFinding) -> BestEffortOut:
        """Named apart from ``of`` on purpose — the argument type is narrower.

        Overriding ``of`` would let any ``RankedFinding`` in and leave the
        weakness to be invented at the edge, which is the one thing the domain
        type refuses to allow.
        """
        entry = AnswerEntryOut.of(lead)
        return cls(**entry.model_dump(), weakness=lead.weakness)


class AnswerOut(BaseModel):
    """What one objective can be answered with — possibly several things.

    ``nearest`` answers "what is closest"; ``nearest_named`` answers "what can I
    act on". They are different questions and a run routinely produces different
    answers to them, so both are reported rather than one being chosen silently
    (investigation/answers.py). ``same`` is True when the nearest endpoint is
    itself named, so a consumer prints it once instead of twice.

    ``best_effort`` is present only when ``nearest`` is null — the domain refuses
    to build the pair — and carries its own weakness text. A consumer that shows
    it must show the weakness beside it, not behind a hover: the two are one
    statement, and the tooltip half of it is the half that gets dropped when the
    line is copied into a filing.
    """

    direction: str
    nearest: AnswerEntryOut | None
    nearest_named: AnswerEntryOut | None
    same: bool
    best_effort: BestEffortOut | None = None

    @classmethod
    def of(cls, answer: DirectionAnswer) -> AnswerOut:
        return cls(
            direction=str(answer.direction),
            nearest=AnswerEntryOut.of(answer.nearest) if answer.nearest else None,
            nearest_named=(
                AnswerEntryOut.of(answer.nearest_named) if answer.nearest_named else None
            ),
            same=answer.same,
            best_effort=(BestEffortOut.of_lead(answer.best_effort) if answer.best_effort else None),
        )


class FindingsResponse(BaseModel):
    investigation_id: uuid.UUID
    status: str
    findings: list[FindingOut]
    # Derived server-side so every consumer that states "nearest previous/next
    # VASP" states the same thing — the demo UI today, a formal report later.
    answers: list[AnswerOut] = []
    # Carried beside the answers, not left to a second request. "No endpoint was
    # found" and "no endpoint was found, and here is what the run never read"
    # are different statements, and this is the endpoint where a consumer reads
    # the first one.
    coverage: CoverageOut


class GraphNodeOut(BaseModel):
    """One traversal node, as the graph view needs it.

    ``value_share`` is a decimal STRING, not an int: smallest-unit sums exceed
    2^53 routinely (a live trace carried 8.72e29), and JSON numbers land in a
    JavaScript double, which would silently round them.
    """

    id: int
    chain: str
    address: str
    direction: str | None
    hop: int
    value_share: str | None
    state: str
    # Traversal honesty, carried into the picture: a branch whose history was
    # cut, and the reason a node stopped, must not be drawable as a clean end.
    history_truncated: bool
    terminal_reason: str | None
    discovered_reason: str
    # The same honesty for a branch whose LINK was selected rather than
    # witnessed. Without these two the renderer physically cannot obey "never
    # draw a guess as a traced path" — it has no way to tell which nodes are
    # guesses, so a mixer candidate lands on the canvas as an ordinary hop with
    # an ordinary edge. ``speculative_basis`` gives the picture the engine's own
    # reason to show, rather than a caption invented at the edge.
    speculative: bool = False
    speculative_basis: str | None = None
    # How many onward branches this address had that the supernode guard never
    # followed. Without it the picture draws a hub with sixty ways out as a hub
    # with twenty and no mark on it — the same overclaim as omitting
    # ``history_truncated``.
    counterparties_dropped: int | None = None

    @classmethod
    def of(cls, node: GraphNode) -> GraphNodeOut:
        return cls(
            id=node.id,
            chain=node.chain,
            address=node.address,
            direction=node.direction,
            hop=node.hop_distance,
            value_share=None if node.value_share is None else str(node.value_share),
            state=node.state,
            history_truncated=node.history_truncated,
            terminal_reason=node.terminal_reason,
            discovered_reason=node.discovered_reason,
            speculative=node.speculative,
            speculative_basis=node.speculative_basis,
            counterparties_dropped=node.counterparties_dropped,
        )


class GraphEdgeOut(BaseModel):
    """One traversal edge and the movement it stands for.

    ``asset_verified`` reports the provenance floor: a token contract may emit
    transfers naming any amount it likes, so an amount in an unverified asset
    is a real event but not a trustworthy quantity. The renderer must be able
    to say which it is drawing.
    """

    id: int
    src: int
    dst: int
    kind: str
    amount: str | None
    asset_symbol: str | None
    asset_decimals: int | None
    asset_kind: str | None
    asset_verified: bool
    tx_hash: str | None
    timestamp: datetime | None

    @classmethod
    def of(cls, edge: GraphEdge, *, asset_verified: bool) -> GraphEdgeOut:
        return cls(
            id=edge.id,
            src=edge.src_node_id,
            dst=edge.dst_node_id,
            kind=edge.kind,
            amount=None if edge.amount is None else str(edge.amount),
            asset_symbol=edge.asset_symbol,
            asset_decimals=edge.asset_decimals,
            asset_kind=edge.asset_kind,
            asset_verified=asset_verified,
            tx_hash=edge.tx_hash,
            timestamp=edge.timestamp,
        )


class GraphResponse(BaseModel):
    """The traversal graph, plus what was left out of it.

    ``node_total`` against ``len(nodes)`` is deliberate: a real trace can hold
    a thousand addresses, and a view that quietly drew the first two hundred
    would read as complete coverage. The caller is told the size it did not
    receive so it can say so on screen.

    ``truncated`` means exactly "there are nodes you did not receive", and it
    keeps that meaning now that labelled and finding-bearing nodes come back
    outside the per-level quota: those nodes are ordinary address nodes counted
    in ``node_total`` like any other, so ``len(nodes)`` can grow towards the
    total but never past it.
    """

    investigation_id: uuid.UUID
    status: str
    chain: str
    root_address: str
    nodes: list[GraphNodeOut]
    edges: list[GraphEdgeOut]
    node_total: int
    truncated: bool

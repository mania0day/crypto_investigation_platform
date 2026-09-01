"""Request/response models for the API — presentation only.

These are the wire shapes. Core domain objects are mapped into them at the
edge so the domain model never leaks HTTP concerns and vice versa.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from cipherchain.core.models import Evidence, EvidenceKind, Finding
from cipherchain.harvest.runs import SyncStatus
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


class ManualLabelOut(BaseModel):
    """An ACTIVE, sourced claim only — the same rule the automated engine
    holds. A manual explorer that let itself print an unverified guess as a
    name would be strictly worse than the engine it sits beside."""

    entity: str
    confidence: float
    source: str


class CounterpartyOut(BaseModel):
    """One counterparty from a single, unpersisted history page — the manual
    explorer's building block. Deliberately thinner than GraphEdgeOut: no
    asset_verified flag (that provenance check belongs to the automated
    engine's stored-movement pipeline, not this one-shot lookup), and no
    Finding/Evidence — a human is deciding what this means, not the engine.

    ``amount`` is a decimal STRING, matching GraphEdgeOut: smallest-unit sums
    routinely exceed 2^53 and a JSON number would silently round them.
    """

    address: str
    direction: str
    amount: str
    asset_symbol: str
    asset_decimals: int
    tx_hash: str
    timestamp: datetime
    label: ManualLabelOut | None = None


class ServiceEndpointOut(BaseModel):
    """"Behaves like custodial infrastructure" — a ROLE, never an identity.

    The same thresholds and confidence curve the autonomous engine's
    service-endpoint heuristic uses (analysis/heuristics/service.py), applied
    to the counterparties the manual explorer just read. It says an address
    looks like an exchange; it does not and cannot say WHICH — that needs a
    sourced label, which is why this carries no entity name.

    ``page_bounded`` is always true and must be shown: the degree comes from
    one page of history, so a busy address can fail the thresholds and go
    unmarked. The error only runs toward under-detection, never toward
    calling something a service on counterparties it does not have.
    """

    senders: int
    recipients: int
    #: None when the degree does NOT clear the thresholds — there is no
    #: confidence to report for an inference that was not drawn.
    confidence: float | None = None
    #: False means "measured, did not qualify". Returned anyway because the
    #: degree itself is the evidence an investigator wants to see, and a
    #: silent absence cannot be told apart from "never looked".
    meets_threshold: bool = True
    page_bounded: bool = True


class AddressExpandResponse(BaseModel):
    address: str
    chain: str
    counterparties: list[CounterpartyOut]
    truncated: bool
    total_count: int
    #: Set only when the degree on this page clears the service thresholds.
    service_endpoint: ServiceEndpointOut | None = None
    #: Opaque resume point for the NEXT page of history, or None when the
    #: provider says there is nothing after this one. Ranking is per-page, so
    #: paging widens the set of counterparties rather than continuing a single
    #: global ordering — the caller merges.
    next_cursor: str | None = None


class ManualLabelRequest(BaseModel):
    """An investigator's own claim about an address, entered by hand in the
    manual explorer. This is deliberately the SAME shape of claim a public
    explorer tag is (intel/explorer_tags.py): a name with no first-party
    publication behind it. It arrives ``pending`` and stays that way until a
    trusted-method source corroborates it (intel/policy.py) — this endpoint
    cannot make an address read as a confirmed VASP by itself, on purpose.
    """

    chain: str
    entity: str = Field(min_length=1, max_length=64)
    # Matches storage/tables.py's ``category_values`` CHECK constraint —
    # kept as a real, separate list here (not imported) because this is the
    # untrusted-input boundary and a typo in a shared constant would silently
    # widen it on both ends at once.
    category: Literal["vasp", "sanctioned", "mixer", "infrastructure"] = "vasp"

    @field_validator("entity")
    @classmethod
    def _no_smuggled_annotation(cls, v: str) -> str:
        # Mirrors IntelClaim's own check (intel/policy.py) so a bad entity is
        # rejected here, as a normal 422, rather than surfacing as the
        # ValueError IntelClaim raises deeper in the call.
        v = v.strip()
        if not v:
            raise ValueError("entity must not be empty")
        if any(c in v for c in "\r\n"):
            raise ValueError("entity must be a single line")
        if "(" in v or ")" in v:
            raise ValueError("entity must not contain parentheses")
        if "://" in v:
            raise ValueError("entity must not contain a URL")
        return v


class TransferOut(BaseModel):
    """One movement as the manual explorer's Transfer tab needs it.

    ``amount`` is a decimal STRING for the same reason CounterpartyOut's is:
    smallest-unit values routinely exceed 2^53 and a JSON number would round
    them silently.
    """

    counterparty: str
    direction: str
    amount: str
    asset_symbol: str
    asset_decimals: int
    tx_hash: str
    timestamp: datetime
    label: ManualLabelOut | None = None


class AddressTransfersResponse(BaseModel):
    address: str
    chain: str
    transfers: list[TransferOut]
    truncated: bool
    total_count: int
    next_cursor: str | None = None


class UsdValueOut(BaseModel):
    """A market conversion — NOT a chain fact.

    Never serialised without ``source`` and ``as_of``. A price is the one
    number in this API that is only true at an instant, and a dollar figure
    with no timestamp is a claim nobody can check.
    """

    usd: float
    unit_price_usd: float
    source: str
    source_url: str
    #: The MARKET's own quote time where the feed gives one, else when we
    #: asked. Distinct from retrieved_at: a frozen feed still answers fast.
    as_of: datetime
    retrieved_at: datetime
    stale: bool = False


class HoldingOut(BaseModel):
    """One asset an address holds. ``amount`` is a decimal STRING, matching
    CounterpartyOut — smallest-unit values exceed 2^53 routinely."""

    symbol: str
    decimals: int
    amount: str
    contract: str | None = None
    value: UsdValueOut | None = None


class AddressBalanceResponse(BaseModel):
    """What an address holds, or an explicit statement of why we cannot say.

    ``native`` is null ONLY together with ``unavailable``, never alongside an
    ``amount`` of "0": a balance of zero and a balance nobody could read are
    different facts, and a fraud investigation must not confuse them (the
    same argument ``Movement.gas_price`` makes in core/models.py).

    ``price_unavailable`` is the softer failure — the balance is still true,
    only the conversion is missing.
    """

    address: str
    chain: str
    native: HoldingOut | None
    staked: HoldingOut | None = None
    tokens: list[HoldingOut] = []
    retrieved_at: datetime | None = None
    #: Set ONLY when every returned holding carries a value. A partial
    #: cross-asset total is a wrong number, and this codebase already refuses
    #: cross-asset sums as displayed values (investigation/manual_expand.py).
    total_usd: float | None = None
    unavailable: str | None = None
    price_unavailable: str | None = None


class LeadLookupRequest(BaseModel):
    """Addresses to ask a public explorer about, for the manual explorer.

    Bounded small on purpose: the reader behind this spaces its requests to
    stay welcome at a free public API (intel/explorer_tags.py), so a large
    batch is a long wall-clock wait, not a bigger answer.
    """

    chain: str
    addresses: list[str] = Field(min_length=1, max_length=12)


class LeadOut(BaseModel):
    """A name a public explorer puts on an address. NOT evidence.

    Deliberately a different shape from ManualLabelOut: that one carries a
    confidence and a source because an ACTIVE claim earned them. This one
    carries the explorer's name and nothing that would let a caller rank it
    against a sourced label — it is a lead to check, and the UI must not be
    able to render it as anything else.
    """

    address: str
    entity: str
    source: str


class LeadLookupResponse(BaseModel):
    chain: str
    leads: list[LeadOut]
    examined: int
    unsupported_chain: bool = False


class ManualLabelResponse(BaseModel):
    """What actually happened to the claim — never a bare "saved", because
    the one fact this response exists to state is that ``status`` is
    ``pending``, not ``active``: the UI must not let a submitted tag look
    like a confirmed label."""

    chain: str
    address: str
    entity: str
    status: str
    outcome: str


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


class UnverifiedTagOut(BaseModel):
    """A name an explorer puts on an address — a LEAD, never evidence.

    Carried on the node rather than in ``findings``/``evidence`` deliberately.
    The evidence taxonomy is frozen at four kinds and every one of them is
    citable; a name that nothing verified must not be able to enter that
    channel at all, so it travels on a field the report's citation machinery
    does not read. ``source`` and ``confidence`` ship with it so the UI can
    show *who* says this and how little weight it carries, rather than
    presenting a bare name that looks like an attribution.
    """

    entity: str
    source: str
    confidence: float


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
    # Names nothing verified. Populated only for addresses whose operator our
    # own heuristic could not name, and never consulted when deciding the
    # answer — see intel.leads for why that separation is the whole design.
    unverified_tags: list[UnverifiedTagOut] = []

    @classmethod
    def of(
        cls, node: GraphNode, unverified_tags: list[UnverifiedTagOut] | None = None
    ) -> GraphNodeOut:
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
            unverified_tags=unverified_tags or [],
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


class SyncSourceOut(BaseModel):
    """One source's line in the sync panel.

    ``ok`` and ``stale`` are tri-state on purpose. ``None`` means the newest
    run said nothing about this source — it was scheduled after that run, or
    no run has happened — which is a different fact from "it failed" and must
    not render as either a tick or a cross.
    """

    source: str
    entity: str | None = None
    transport: str
    document_url: str | None = None
    ok: bool | None = None
    # True when this drop-only source has never been supplied here. A setup
    # step, not a break — see harvest.sources.SourceNotSupplied.
    not_supplied: bool | None = None
    error: str | None = None
    claims: int | None = None
    added: int | None = None
    updated: int | None = None
    unchanged: int | None = None
    published_at: str | None = None
    age_days: float | None = None
    stale_after_days: int | None = None
    stale: bool | None = None


class SyncStatusResponse(BaseModel):
    """Is the label store being kept current, and if not, whose move is it?

    ``state`` separates four things a single boolean would flatten:
    ``syncing`` (a cycle is in flight), ``idle`` (one finished, the timer is
    working), ``stalled`` (a cycle was killed and never closed its row) and
    ``never_run`` (nothing has ever harvested against this database, so every
    label in it arrived by hand).
    """

    state: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    outcome: str | None = None
    exit_code: int | None = None
    error: str | None = None
    host: str | None = None
    sources: list[SyncSourceOut] = []
    labels_total: int = 0
    labels_by_chain: dict[str, int] = {}
    # Prose, already worded for the reader, because the action differs by
    # transport: a stale fetched source means the publisher went quiet, a stale
    # dropped source means somebody owes the drop directory a download.
    attention: list[str] = []

    @classmethod
    def of(cls, status: SyncStatus) -> SyncStatusResponse:
        return cls(
            state=status.state,
            started_at=status.started_at,
            finished_at=status.finished_at,
            outcome=status.outcome,
            exit_code=status.exit_code,
            error=status.error,
            host=status.host,
            sources=[SyncSourceOut(**row) for row in status.sources],
            labels_total=status.labels_total,
            labels_by_chain=dict(status.labels_by_chain),
            attention=list(status.attention),
        )

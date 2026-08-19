"""What the report SAYS, decided once, before anything is rendered.

A report is not a view of the API response. The API answers "what did the run
record"; a report answers "what may a reader conclude, and what may they NOT
conclude" — and the second half is the part that gets a case thrown out if the
document gets it wrong. So the decisions live here, in plain value objects, and
both renderers (HTML, and PDF via HTML) are dumb about them.

Four of those decisions are load-bearing:

**Two answers stay two answers.** ``DirectionAnswer`` already refuses to choose
between "nearest" and "nearest named" (investigation/answers.py). A report is
where that refusal either survives or quietly dies, because prose invites a
single headline. So the divergence is not merely displayed, it is *explained*:
an investigator can serve a request on Binance and cannot serve one on
"custodial infrastructure, operator unnamed, 61%", and the document has to say
which of the two it is holding.

**A third-party claim is worth its date.** A label recorded last week and one
recorded three years ago are the same sentence with different weight, and a
renderer that prints the sentence alone silently equates them. ``describe_claim_age``
exists so no output path can forget, including the "no date recorded" case,
which is the worst of the three and used to look like the best.

**The verdict is decided here too.** ``summarise_answers`` turns the sections
into the front-page statement — in from whom, out to whom — and every claim
that a request can be served rests on ``AnswerSection.actionable``, the same
property the status banner counts. Deciding it once is what stops the document
contradicting itself: a summary naming an exchange above a banner reading "not
answered" is not a layout bug, it is two answers to the same question, and a
reader picks whichever suits them. ``actionable`` asks for an extracted
operator NAME rather than for the presence of a third-party claim, because a
claim can describe the address — "identified as mixer 'Tornado Cash'" — and
leave nobody to address anything to.

**Caveats are computed, never passed in.** ``derive_caveats`` cannot return an
empty tuple: with nothing to report it returns the statement that nothing was
recorded, together with what that does and does not prove. A report that hides
its own gaps is the most dangerous artefact this tool can produce, and the only
way to be sure the section is never dropped is to make emptiness unrepresentable
rather than to remember to render it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from cipherchain.chains.base import feed_name_for_code
from cipherchain.core.models import (
    Address,
    Direction,
    Evidence,
    EvidenceKind,
    Finding,
    FindingKind,
)
from cipherchain.investigation.answers import (
    BestEffortFinding,
    DirectionAnswer,
    RankedFinding,
    claim_entity,
    claim_evidence,
    naming_claim,
)

# Reading order for evidence, and it is an argument, not a preference: what
# anyone can check first, then who asserted what and when, then what CipherChain
# reasoned, then what CipherChain itself did. A reader who stops early stops on the
# strongest material, and the weakest can never be mistaken for the strongest by
# appearing above it.
EVIDENCE_ORDER: tuple[EvidenceKind, ...] = (
    EvidenceKind.ONCHAIN_FACT,
    EvidenceKind.THIRD_PARTY_CLAIM,
    EvidenceKind.HEURISTIC_INFERENCE,
    EvidenceKind.ENGINE_OBSERVATION,
)

EVIDENCE_TITLES: Mapping[EvidenceKind, str] = {
    EvidenceKind.ONCHAIN_FACT: "On-chain facts",
    EvidenceKind.THIRD_PARTY_CLAIM: "Third-party claims",
    EvidenceKind.HEURISTIC_INFERENCE: "Heuristic inferences",
    EvidenceKind.ENGINE_OBSERVATION: "Engine observations",
}

# Said in the document itself, because the taxonomy is the whole reason this
# tool's output can be relied on and a reader who does not know the four kinds
# apart will read a guess as a fact.
EVIDENCE_CAPTIONS: Mapping[EvidenceKind, str] = {
    EvidenceKind.ONCHAIN_FACT: (
        "Verifiable by anyone against the ledger, using the transaction references given."
    ),
    EvidenceKind.THIRD_PARTY_CLAIM: (
        "Someone else's assertion, reproduced with its source and its date. This is the only "
        "kind of evidence that ever names an operator, and its age is part of its weight."
    ),
    EvidenceKind.HEURISTIC_INFERENCE: (
        "CipherChain's own reasoning from on-chain behaviour, with the versioned rule that "
        "produced it. An inference describes what an address does — it never names who runs it."
    ),
    EvidenceKind.ENGINE_OBSERVATION: (
        "A statement about this run: what it examined, where it stopped, what it never looked "
        "at. Check it against the investigation record, not against the chain."
    ),
}

# A claim older than this is flagged in the report. One year is not a legal
# threshold and is not presented as one — it is the point past which a deposit
# address may plausibly have been recycled, an exchange may have been acquired,
# and the reader deserves to be nudged into checking rather than trusting.
STALE_CLAIM_DAYS = 365


def _as_utc(moment: datetime) -> datetime:
    """Naive timestamps are treated as UTC — the store writes tz-aware rows.

    A naive value here means someone constructed one by hand; assuming UTC keeps
    arithmetic possible instead of raising inside a report renderer, and every
    printed timestamp is labelled UTC so the assumption is visible.
    """
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def format_moment(moment: datetime | date | None) -> str:
    """One timestamp format for the whole document, absence included."""
    if moment is None:
        return "not recorded"
    if isinstance(moment, datetime):
        return _as_utc(moment).strftime("%Y-%m-%d %H:%M UTC")
    return moment.strftime("%Y-%m-%d")


def describe_claim_age(evidence: Evidence, now: datetime) -> tuple[str, bool]:
    """How old this claim is, and whether that should worry the reader.

    Returns the sentence and a "flag it" boolean. The undated case is deliberately
    the loudest of the three: a claim with no date is not fresh, it is *unknown*,
    and printing the source alone made an unknown-age attribution look identical
    to one recorded this morning.
    """
    if evidence.source_date is None:
        return ("no source date recorded — the age of this claim is unknown", True)
    recorded = evidence.source_date
    days = (_as_utc(now).date() - _as_utc(recorded).date()).days
    if days < 0:
        # A future-dated source is a data defect, not a fresh claim. Saying so is
        # better than printing "-12 days old" and letting a reader trust it.
        return (f"dated {format_moment(recorded)} — dated in the future, treat as unverified", True)
    if days >= STALE_CLAIM_DAYS:
        years = days / 365.0
        return (f"dated {format_moment(recorded)} — {days} days old (~{years:.1f} years)", True)
    return (f"dated {format_moment(recorded)} — {days} days old", False)


# The engine writes a VASP claim's summary as "<entity> labeled '<category>'"
# (InvestigationEngine._vasp_finding). Reading the operator's name back out is
# the only way to key VASP metadata, since the finding stores prose rather than
# a structured entity. Coupled on purpose and narrowly: an unrecognised shape
# yields None, so metadata is reported ABSENT rather than looked up under a
# name we guessed at.
@dataclass(frozen=True, slots=True)
class VaspProfile:
    """What is on file about a named VASP — the part an investigator acts on.

    Every field is optional because this is reference data assembled from public
    sources: a half-filled row is normal and is still useful (a jurisdiction
    alone tells a reader which regime to write under). A row that is entirely
    empty is reported as absent rather than as a table of blanks pretending to
    be knowledge.
    """

    entity: str
    jurisdiction: str | None = None
    legal_entity: str | None = None
    kyc_regime: str | None = None
    kyc_since: date | datetime | None = None
    le_request_channel: str | None = None
    source: str | None = None
    source_date: date | datetime | None = None

    @property
    def has_content(self) -> bool:
        return any(
            (
                self.jurisdiction,
                self.legal_entity,
                self.kyc_regime,
                self.kyc_since,
                self.le_request_channel,
            )
        )


@dataclass(frozen=True, slots=True)
class EvidenceGroup:
    """One evidence kind's items, with the taxonomy explained beside them."""

    kind: EvidenceKind
    title: str
    caption: str
    items: tuple[Evidence, ...]


def group_evidence(finding: Finding) -> tuple[EvidenceGroup, ...]:
    """Group a finding's evidence by kind, in reading order, dropping empty kinds.

    Kinds with nothing in them are omitted rather than printed empty: the fact
    that a conclusion carries no third-party claim is already stated where it
    matters — in the answer block, as "no operator is named" — and an empty
    heading invites a reader to conclude something was withheld.
    """
    groups: list[EvidenceGroup] = []
    for kind in EVIDENCE_ORDER:
        items = tuple(e for e in finding.evidence if e.kind is kind)
        if items:
            groups.append(
                EvidenceGroup(
                    kind=kind,
                    title=EVIDENCE_TITLES[kind],
                    caption=EVIDENCE_CAPTIONS[kind],
                    items=items,
                )
            )
    return tuple(groups)


@dataclass(frozen=True, slots=True)
class ReportHeader:
    """Provenance of the document itself.

    Version fields are not decoration: a conclusion is only reproducible against
    the engine and ruleset that produced it, and a report handed on without them
    cannot be re-run by the recipient.
    """

    investigation_id: str
    subject: Address
    status: str
    generated_at: datetime
    engine_version: str
    ruleset_version: str
    objectives: tuple[str, ...] = ()
    started_at: datetime | None = None
    updated_at: datetime | None = None
    budgets: Mapping[str, Any] = field(default_factory=dict)
    spent: Mapping[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def is_partial(self) -> bool:
        return self.status == "partial"

    @property
    def is_settled(self) -> bool:
        """Did the run reach an end state, or is this a snapshot of one in flight?"""
        return self.status in ("completed", "partial", "failed")


@dataclass(frozen=True, slots=True)
class AnswerEndpoint:
    """One endpoint offered as an answer, with everything needed to act on it."""

    address: str
    chain: str
    hop: int
    confidence: float
    named: bool
    summary: str
    finding: Finding
    entity: str | None = None
    vasp: VaspProfile | None = None

    @classmethod
    def of(cls, ranked: RankedFinding, profiles: Mapping[str, VaspProfile]) -> AnswerEndpoint:
        finding = ranked.finding
        entity = claim_entity(finding)
        return cls(
            address=finding.subject.value,
            chain=finding.subject.chain,
            hop=ranked.hop,
            confidence=finding.confidence,
            named=ranked.named,
            summary=finding.summary,
            finding=finding,
            entity=entity,
            # Reference data attaches ONLY where a third-party claim already
            # named an operator. Profiles are keyed by address, so an
            # address-indexed lookup will answer for an endpoint that rests on
            # nothing but a behavioural inference — and its answer is a legal
            # entity, a jurisdiction and a filing channel. Rendering that turns
            # "this address behaves like a custodian" into "serve Binance
            # Holdings Ltd in the Cayman Islands" on a card whose own badge
            # reads "operator not named". The inference names nobody; metadata
            # may only describe somebody a claim already named.
            vasp=profiles.get(finding.subject.value) if ranked.named and entity else None,
        )

    @property
    def carries_a_claim(self) -> bool:
        """Is there a sourced claim here at all — even one that names nobody?

        Distinct from ``named``, and the distinction is the whole point. A
        sanctions listing is a third_party_claim ABOUT the address that
        identifies no operator. The empty-state message for the NAMED slot used
        to say "no endpoint on this route carries a third-party claim", which
        was true only while ``is_named`` wrongly counted such a claim as an
        attribution. Once it stopped, that sentence started appearing on pages
        that were showing the reader exactly such a claim.
        """
        return bool(claim_evidence(self.finding))

    @property
    def names_an_operator(self) -> bool:
        """Is there an operator NAME here, and not merely a claim of the right kind?

        ``named`` is ``is_named(finding)``, which asks only whether some
        third_party_claim exists — and plenty of legitimate claims name no
        operator at all. "address identified as mixer 'Tornado Cash'" is a
        third_party_claim about the address itself; ``claim_entity`` cannot
        read a VASP out of it and returns None. Every actionability statement
        in this document turns on THIS property rather than on ``named``,
        because "a source says something about this address" and "there is a
        company a request can be served on" are different facts, and only the
        second one has a respondent.
        """
        return self.named and self.entity is not None

    @property
    def evidence_groups(self) -> tuple[EvidenceGroup, ...]:
        return group_evidence(self.finding)


@dataclass(frozen=True, slots=True)
class BestEffortEndpoint(AnswerEndpoint):
    """A lead offered where the direction has no answer — with its weakness.

    Mirrors ``answers.BestEffortFinding`` one layer up, and for the same reason:
    ``weakness`` cannot be omitted, so there is no way to render the name
    without the sentence that says the path to it was selected rather than
    followed. A subclass so every existing renderer, caveat and metadata lookup
    written against ``AnswerEndpoint`` keeps working on it unchanged.
    """

    # No default: omitting it is a TypeError at construction, not a validation
    # failure afterwards. `__post_init__` then catches the blank string, which
    # satisfies the signature but says nothing.
    weakness: str = field(kw_only=True)

    def __post_init__(self) -> None:
        if not self.weakness.strip():
            raise ValueError("a best-effort endpoint must state its weakness in plain language")

    @classmethod
    def of_lead(
        cls, ranked: BestEffortFinding, profiles: Mapping[str, VaspProfile]
    ) -> BestEffortEndpoint:
        """Built only from a ``BestEffortFinding``, never a plain ranked one.

        Named apart from ``of`` deliberately: if the ordinary constructor
        accepted a lead, a caller could produce one with an invented weakness or
        none at all, which is the failure the mandatory field exists to stop.
        """
        base = AnswerEndpoint.of(ranked, profiles)
        return cls(
            address=base.address,
            chain=base.chain,
            hop=base.hop,
            confidence=base.confidence,
            named=base.named,
            summary=base.summary,
            finding=base.finding,
            entity=base.entity,
            vasp=base.vasp,
            weakness=ranked.weakness,
        )


@dataclass(frozen=True, slots=True)
class AnswerSection:
    """One direction's answer, and the plain-language reason there may be two."""

    direction: Direction
    heading: str
    question: str
    nearest: AnswerEndpoint | None
    nearest_named: AnswerEndpoint | None
    same: bool
    # Present only where `nearest` is empty — `DirectionAnswer` refuses the
    # pair, so a lead can never sit beside an endpoint that was actually
    # traced. It is rendered in the headline position (REACHING_THE_VASP.md §4)
    # because a report whose only line is "no named endpoint" is not usable by
    # the body it is written for.
    best_effort: BestEffortEndpoint | None = None

    @property
    def actionable(self) -> bool:
        """Is there anyone here to send a request to?

        A lead is deliberately NOT actionable however well sourced its label:
        the operator may be real, but this run cannot show that the subject's
        funds reached it, and "actionable" is what decides whether the report
        tells a reader they have someone to serve.

        Reaching ``nearest_named`` is not enough on its own. That endpoint is
        selected on the presence of a third_party_claim, and a claim that names
        the address rather than an operator — a sanctions listing, a mixer
        identification — satisfies that test while leaving nobody to write to.
        There has to be a name.
        """
        return self.nearest_named is not None and self.nearest_named.names_an_operator

    @property
    def has_answer(self) -> bool:
        """Did this direction produce anything at all to show — lead included?"""
        return (
            self.nearest is not None
            or self.nearest_named is not None
            or self.best_effort is not None
        )

    @property
    def divergence(self) -> str | None:
        """Why this direction shows two rows, said the way a reader needs it.

        None when there is nothing to explain — one row, or none. The wording
        avoids "confidence" as the distinguishing feature on purpose: the
        difference is not that one answer is weaker, it is that one of them
        identifies a party who can be asked a question and the other does not.
        """
        if self.nearest is None or self.same:
            return None
        if self.nearest_named is None:
            return (
                "The closest endpoint on this route is not attributed to any named operator. "
                "CipherChain inferred from its on-chain behaviour that it is custodial "
                "infrastructure; that inference describes what the address does, and by "
                "design it never names who runs it. There is no operator here to serve a "
                "legal request on — the address itself is the lead, and a sourced label "
                "for it is what would turn this into an answer."
            )
        if not self.nearest_named.names_an_operator:
            # Two rows and still no respondent. Falling through to the sentence
            # below would tell the reader the further endpoint "is attributed to
            # an operator … which is the one a legal request can be addressed
            # to" — of an endpoint whose claim named nobody.
            return (
                "These are two different addresses, and neither one names an operator. The "
                f"closest endpoint ({self.nearest.hop} hop(s) out) carries no sourced "
                f"attribution at all. The further one ({self.nearest_named.hop} hop(s) out) "
                "does carry a third-party claim, but that claim describes the address rather "
                "than naming who runs it. There is nobody on this route to address a legal "
                "request to."
            )
        return (
            "These are two different addresses answering two different questions. "
            f"The closest endpoint ({self.nearest.hop} hop(s) out) carries no sourced "
            "attribution, so nobody is named and there is no operator to approach. The "
            f"closest NAMED endpoint ({self.nearest_named.hop} hop(s) out) is attributed to "
            "an operator on a citable source, which is the one a legal request can be "
            "addressed to. Neither replaces the other: the nearer address is where the "
            "funds actually went first."
        )


#: Why the address in a verdict's second slot is not the answer. Two different
#: sentences, because the two are weak in different places: a behavioural
#: endpoint was genuinely reached and nobody is named at it, while an untraced
#: lead may carry a perfectly good name at the end of a route this run guessed.
LEAD_BEHAVIOURAL = "behavioural"
LEAD_UNTRACED = "untraced"


@dataclass(frozen=True, slots=True)
class SummaryEndpoint:
    """One address as the front-page summary states it.

    A separate type from ``AnswerEndpoint`` for one reason: ``operator`` is
    filled here only from a third-party claim, so "the summary cannot name an
    operator a claim did not name" is a property of how this object is built
    rather than a rule each renderer has to remember. The block is the first
    thing a reader sees and the last thing they quote, and an address that
    merely behaves like custodial infrastructure appearing there under a name
    is the single worst thing this document could do.
    """

    address: str
    chain: str
    hop: int
    confidence: float
    operator: str | None = None
    #: The third-party claim this endpoint rests on — source and age — never a
    #: heuristic, which is the case where a reader would otherwise read a
    #: heuristic's id as a citation. Absent where there is no claim at all.
    #:
    #: Set WITHOUT ``operator`` where a claim exists that names nobody, and a
    #: renderer may not word it as "the name rests on" there: a sanctions
    #: listing is real evidence about the address and belongs on the page, but
    #: it is not the provenance of a name this report never had.
    basis: str | None = None
    stale: bool = False

    @property
    def names_an_operator(self) -> bool:
        """Is there a party here a legal request could name as its respondent?"""
        return self.operator is not None


@dataclass(frozen=True, slots=True)
class DirectionVerdict:
    """One direction's answer in the words the summary states it in.

    ``named`` holds ``AnswerSection.nearest_named`` — the endpoint, whether or
    not a name could be read off it — while ``answered`` is what every
    actionability statement branches on. The status banner counts the same
    ``AnswerSection.actionable``, so the two cannot disagree: a document that
    printed "Answered" over a summary naming nobody is a contradiction a reader
    resolves by trusting whichever half suits them.
    """

    direction: Direction
    label: str
    lead_in: str
    named: SummaryEndpoint | None
    lead: SummaryEndpoint | None = None
    #: ``LEAD_BEHAVIOURAL``/``LEAD_UNTRACED``, or empty when there is no lead.
    lead_kind: str = ""

    @property
    def answered(self) -> bool:
        """Is there anyone here a legal request could be addressed to?

        A reached endpoint is not an answer to this question. ``named`` is
        present for any endpoint a third_party_claim attaches to, and a claim
        that identifies the ADDRESS — "identified as mixer 'Tornado Cash'" —
        names no respondent. Answering yes on that would tell an investigator
        to serve a request on nobody.
        """
        return self.named is not None and self.named.names_an_operator


@dataclass(frozen=True, slots=True)
class TraversalCoverage:
    """What the traversal record says about its own completeness.

    Read from the investigation record rather than from prose, so a caveat can
    state a number the reader could recount themselves. ``complete`` is the
    guard that stops a summary claiming the trace read everything while these
    counters say otherwise.
    """

    addresses_reached: int = 0
    truncated_histories: int = 0
    depth_horizon_stops: int = 0
    unexplored_frontier: int = 0
    transactions_examined: int | None = None
    max_depth: int | None = None
    #: Branches that ended AT a mixer with nothing followed past it. A hard stop
    #: in the trail, and a coverage gap in the plainest sense.
    mixer_stops: int = 0
    #: Branches the run continued past a mixer by heuristic. Not a stop, but not
    #: coverage either: everything beyond is selected rather than traced, so a
    #: report that counted these as explored ground would overstate the trace.
    mixer_crossings: int = 0
    #: Addresses whose expansion the supernode guard capped, and the number of
    #: counterparty branches that cap reached and never followed. Absent from
    #: this record until it was found stating "no address was left partially
    #: read" over a run that had dropped forty branches at one hub.
    capped_expansions: int = 0
    counterparties_dropped: int = 0
    #: Addresses for which at least one acquisition feed could not be served by
    #: any provider, and the distinct feeds lost across the run (the stable
    #: ``FeedGap.code`` strings). This is the routine shape of degradation once
    #: the keyed quotas are spent: the trace keeps moving on the fallback tier
    #: and pays for it in whichever feed that tier could not answer. Counted
    #: separately from ``truncated_histories`` — which these addresses also set
    #: — because that figure merges three limits and so cannot say WHAT is
    #: missing, and "the token feed was dead here" is the half a reader needs to
    #: judge whether the gap could have hidden a VASP.
    addresses_missing_feeds: int = 0
    feeds_unavailable: tuple[str, ...] = ()
    #: One statement per allowance the run granted itself while an objective had
    #: no named endpoint (``BudgetExtension.statement()``, read back from the
    #: investigation record). Not a gap — the extensions are what the run did to
    #: CLOSE gaps — but a report that showed an answer without saying it cost
    #: nine times the authorised budget would be describing a different run from
    #: the one that happened, and this is the only place that number survives.
    budget_extensions: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not (
            self.truncated_histories
            or self.depth_horizon_stops
            or self.unexplored_frontier
            or self.mixer_stops
            or self.mixer_crossings
            or self.capped_expansions
            # In practice implied by truncated_histories, since the engine sets
            # both. Named anyway: this flag is what every consumer branches on,
            # and it must not depend on two writes in another module staying
            # paired for a run that lost every token feed to keep reporting
            # complete=True — the exact lie this whole path exists to prevent.
            or self.addresses_missing_feeds
        )


@dataclass(frozen=True, slots=True)
class Caveat:
    """One gap, stated as a gap.

    ``code`` is stable and machine-readable so a consumer can assert on the
    presence of a specific caveat without matching prose; ``detail`` carries the
    consequence, because a limitation a reader cannot act on is decoration.
    """

    code: str
    headline: str
    detail: str
    subject: str | None = None


@dataclass(frozen=True, slots=True)
class InvestigationReport:
    """One investigation, ready to render. Caveats are never empty by construction."""

    header: ReportHeader
    answers: tuple[AnswerSection, ...]
    other_findings: tuple[Finding, ...]
    coverage: TraversalCoverage
    caveats: tuple[Caveat, ...]

    def __post_init__(self) -> None:
        if not self.caveats:
            raise ValueError("a report always states its coverage — derive_caveats never empties")

    @property
    def title(self) -> str:
        return f"CipherChain investigation — {self.header.subject.value}"

    def caveat(self, code: str) -> Caveat | None:
        return next((c for c in self.caveats if c.code == code), None)


_DIRECTION_HEADING: Mapping[Direction, str] = {
    Direction.BACKWARD: "Nearest previous VASP",
    Direction.FORWARD: "Nearest next VASP",
}

_DIRECTION_QUESTION: Mapping[Direction, str] = {
    Direction.BACKWARD: "Where did the funds reaching this address come from?",
    Direction.FORWARD: "Where did the funds leaving this address go?",
}

# The summary states the movement where the answer section asks the question.
# A reader who reads one block reads this one, and they have to come away
# holding "in from X, out to Y" rather than two questions they still have to
# answer for themselves.
_DIRECTION_LABEL: Mapping[Direction, str] = {
    Direction.BACKWARD: "Money in",
    Direction.FORWARD: "Money out",
}

_DIRECTION_LEAD_IN: Mapping[Direction, str] = {
    Direction.BACKWARD: "Funds came IN from",
    Direction.FORWARD: "Funds went OUT to",
}


def _summary_endpoint(endpoint: AnswerEndpoint, now: datetime) -> SummaryEndpoint:
    claim = naming_claim(endpoint.finding) if endpoint.named else None
    basis: str | None = None
    stale = False
    if claim is not None:
        age, stale = describe_claim_age(claim, now)
        basis = f"third-party claim from {claim.source or 'source not recorded'}, {age}"
    return SummaryEndpoint(
        address=endpoint.address,
        chain=endpoint.chain,
        hop=endpoint.hop,
        confidence=endpoint.confidence,
        # The guard is what keeps behaviour out of the name column: an endpoint
        # reached by inference alone arrives here with operator=None however
        # confidently it behaves like an exchange, and whatever a reference
        # table says about its address. It also leaves operator=None where a
        # claim exists but named nobody — ``entity`` is None there, and
        # ``basis`` below still carries the claim so the evidence is not lost.
        operator=endpoint.entity if endpoint.names_an_operator else None,
        basis=basis,
        stale=stale,
    )


def summarise_answers(
    sections: Sequence[AnswerSection], now: datetime
) -> tuple[DirectionVerdict, ...]:
    """The front-page verdicts: what came in from whom, what went out to whom.

    ``now`` is needed because a name is worth its date — the verdict quotes the
    age of the claim it rests on, so the summary cannot present a three-year-old
    attribution as this morning's.
    """
    verdicts: list[DirectionVerdict] = []
    for section in sections:
        named = (
            _summary_endpoint(section.nearest_named, now)
            if section.nearest_named is not None
            else None
        )
        lead: SummaryEndpoint | None = None
        lead_kind = ""
        if section.best_effort is not None:
            lead, lead_kind = _summary_endpoint(section.best_effort, now), LEAD_UNTRACED
        elif section.nearest is not None and not section.same:
            # ``same`` means the nearest endpoint IS the named one, so there is
            # one fact and one row. Where they differ the nearer address is
            # shown too — it is where the funds actually went first — but never
            # as an answer, because nobody is named at it.
            lead, lead_kind = _summary_endpoint(section.nearest, now), LEAD_BEHAVIOURAL
        verdicts.append(
            DirectionVerdict(
                direction=section.direction,
                label=_DIRECTION_LABEL[section.direction],
                lead_in=_DIRECTION_LEAD_IN[section.direction],
                named=named,
                lead=lead,
                lead_kind=lead_kind,
            )
        )
    return tuple(verdicts)


def build_answer_sections(
    answers: Sequence[DirectionAnswer], profiles: Mapping[str, VaspProfile] | None = None
) -> tuple[AnswerSection, ...]:
    """Turn selected answers into report sections, metadata attached."""
    resolved = profiles or {}
    return tuple(
        AnswerSection(
            direction=answer.direction,
            heading=_DIRECTION_HEADING[answer.direction],
            question=_DIRECTION_QUESTION[answer.direction],
            nearest=(
                AnswerEndpoint.of(answer.nearest, resolved) if answer.nearest is not None else None
            ),
            nearest_named=(
                AnswerEndpoint.of(answer.nearest_named, resolved)
                if answer.nearest_named is not None
                else None
            ),
            same=answer.same,
            best_effort=(
                BestEffortEndpoint.of_lead(answer.best_effort, resolved)
                if answer.best_effort is not None
                else None
            ),
        )
        for answer in answers
    )


def _status_caveats(header: ReportHeader) -> list[Caveat]:
    if header.is_partial:
        return [
            Caveat(
                code="partial_run",
                headline="This run did not finish.",
                detail=(
                    "The investigation stopped on an exhausted budget rather than on an "
                    "exhausted trail. Everything below is what was found before it stopped. "
                    "Absence of a result in any direction is not evidence that none exists — "
                    "re-running with a larger budget may reach further."
                ),
            )
        ]
    if header.status == "failed":
        return [
            Caveat(
                code="failed_run",
                headline="This run failed part-way through.",
                detail=(
                    "The engine recorded an error and stopped: "
                    f"{header.error or 'no error text was recorded'}. Treat every conclusion "
                    "below as provisional — the trace was interrupted, not completed."
                ),
            )
        ]
    if not header.is_settled:
        return [
            Caveat(
                code="run_in_flight",
                headline="This report was produced while the run was still in progress.",
                detail=(
                    f"The investigation was '{header.status}' at the moment this document was "
                    "generated, so it is a snapshot of an unfinished trace. Findings can still "
                    "be added, and a nearer endpoint may yet be discovered."
                ),
            )
        ]
    return []


def _answer_caveats(sections: Sequence[AnswerSection]) -> list[Caveat]:
    caveats: list[Caveat] = []
    for section in sections:
        route = "backward" if section.direction is Direction.BACKWARD else "forward"
        if section.best_effort is not None:
            # A lead is shown in the headline, so the caveat cannot be silence.
            # It states the thing the headline cannot: the endpoint on display
            # was SELECTED, and the run reached nothing by following value.
            caveats.append(
                Caveat(
                    code=f"lead_only_{route}",
                    headline=(f"No endpoint was reached tracing {route} without crossing a mixer."),
                    detail=(
                        "The endpoint shown for this direction is a lead, not a traced result. "
                        "The route to it passes through a mixer, where the deposit-to-withdrawal "
                        "link is severed by design; CipherChain selected the onward branch by "
                        "heuristic rather than following a movement, so the address may belong "
                        "to an unrelated party. "
                        f"{section.best_effort.weakness} "
                        "It is offered because a direction that reports nothing is not usable, "
                        "and it is marked because it cannot carry the weight of a traced hop."
                    ),
                    subject=section.best_effort.address,
                )
            )
        elif section.nearest is None and section.nearest_named is None:
            caveats.append(
                Caveat(
                    code=f"no_endpoint_{route}",
                    headline=f"No endpoint was reached tracing {route}.",
                    detail=(
                        "The trace recorded no attributed endpoint in this direction. That is a "
                        "statement about what this run reached, not about the funds: the trail "
                        "may continue past the addresses examined here."
                    ),
                )
            )
        elif section.nearest_named is None:
            address = section.nearest.address if section.nearest else ""
            # The NAMED slot is empty for two quite different reasons, and the
            # wording cannot be shared between them. "Only a behavioural
            # inference supports it" is FALSE where a sourced claim exists and
            # merely names nobody — a sanctions listing is real, citable
            # evidence about the address — and a caveat that misdescribes its
            # own evidence is one a reader discounts. Which branch fires is
            # decided by whether a claim is present, not by whether it named
            # anyone.
            if section.nearest is not None and section.nearest.carries_a_claim:
                caveats.append(
                    Caveat(
                        code=f"claim_names_no_operator_{route}",
                        headline=f"No NAMED operator was found tracing {route}.",
                        detail=(
                            "An endpoint was reached and a third-party source does make a claim "
                            "about it — but that claim describes the address itself rather than "
                            "naming an operator, so no company or service is identified here. "
                            "The claim is set out in the evidence for this direction and may "
                            "matter a great deal on its own; it simply does not supply a "
                            "respondent, and there is nobody to serve a legal request on until "
                            "a sourced label attributes this address to a named party."
                        ),
                        subject=address,
                    )
                )
            else:
                caveats.append(
                    Caveat(
                        code=f"unnamed_endpoint_only_{route}",
                        headline=f"No NAMED operator was found tracing {route}.",
                        detail=(
                            "An endpoint was reached, but only a behavioural inference supports "
                            "it and an inference never names an operator. There is nobody here "
                            "to serve a legal request on until a sourced label attributes this "
                            "address."
                        ),
                        subject=address,
                    )
                )
        elif not section.actionable:
            # Reached, claim-backed, and still nobody to serve. Without this
            # branch the direction produces no caveat at all — the endpoint is
            # a ``nearest_named`` and every case above has already been ruled
            # out — so the one document-level statement that this direction
            # ends without a respondent would be missing entirely. The wording
            # cannot be borrowed from the branch above either: "only a
            # behavioural inference supports it" is false here, and a caveat
            # that misdescribes its own evidence is one a reader discounts.
            named = section.nearest_named
            caveats.append(
                Caveat(
                    code=f"claim_names_no_operator_{route}",
                    headline=f"No NAMED operator was found tracing {route}.",
                    detail=(
                        "An endpoint was reached and a third-party source does make a claim "
                        "about it — but that claim describes the address itself rather than "
                        "naming an operator, so no company or service is identified here. The "
                        "claim is set out in the evidence for this direction and may matter a "
                        "great deal on its own; it simply does not supply a respondent, and "
                        "there is nobody to serve a legal request on until a sourced label "
                        "attributes this address to a named party."
                    ),
                    subject=named.address if named else "",
                )
            )
    return caveats


def _coverage_caveats(coverage: TraversalCoverage) -> list[Caveat]:
    caveats: list[Caveat] = []
    if coverage.truncated_histories:
        caveats.append(
            Caveat(
                code="truncated_history",
                headline=(
                    f"{coverage.truncated_histories} address(es) had more history than was read."
                ),
                detail=(
                    "An address is read one page at a time, through several acquisition feeds. "
                    "These addresses were read only in part: the page had more behind it, a feed "
                    "could not be served by any provider, or more movements were stored than one "
                    "expansion query returns. The unread part was never examined, so an earlier "
                    "counterparty — possibly a nearer VASP — could not have been seen."
                ),
            )
        )
    if coverage.addresses_missing_feeds:
        # Deliberately its own caveat rather than a clause inside the truncation
        # one above. That caveat says "there was more history"; this says "a
        # whole KIND of value transfer is absent from what was read" — and a
        # reader tracing a stablecoin payment needs to be told when the token
        # feed specifically is the one that went missing, because for them the
        # difference is whether the report's silence means anything at all.
        feeds = ", ".join(feed_name_for_code(code) for code in coverage.feeds_unavailable)
        caveats.append(
            Caveat(
                code="feed_unavailable",
                headline=(
                    f"{coverage.addresses_missing_feeds} address(es) were read with at least "
                    f"one acquisition feed unavailable ({feeds})."
                ),
                detail=(
                    "An address is read through several feeds at once — native transfers, token "
                    "transfers, and value delivered by a contract — and no provider was able to "
                    "serve the feed(s) named above for these addresses. This is how the system "
                    "degrades when its data providers are exhausted: the trace continues on "
                    "whatever tier still answers rather than stopping, and pays for it here. "
                    "Value that moved ONLY through a missing feed is absent from this report, so "
                    "for these addresses an absence of onward movement is not evidence that none "
                    "occurred. Re-running these addresses against a working provider would settle "
                    "it."
                ),
            )
        )
    if coverage.depth_horizon_stops:
        limit = f" (depth limit {coverage.max_depth})" if coverage.max_depth else ""
        caveats.append(
            Caveat(
                code="depth_horizon",
                headline=(
                    f"{coverage.depth_horizon_stops} address(es) were reached at the depth "
                    f"horizon and never expanded{limit}."
                ),
                detail=(
                    "These addresses sit at the edge of the configured search depth. They are "
                    "unexplored leads, not dead ends: the trail continues through them and a "
                    "deeper run would follow it."
                ),
            )
        )
    if coverage.capped_expansions:
        caveats.append(
            Caveat(
                code="capped_expansion",
                headline=(
                    f"{coverage.capped_expansions} high-degree address(es) were expanded only in "
                    f"part — {coverage.counterparties_dropped} onward branch(es) were never "
                    "followed."
                ),
                detail=(
                    "An address with very many counterparties is expanded partially: the trace "
                    "follows the largest by value and abandons the rest. The abandoned branches "
                    "were reached and never read. They are ranked by value in VERIFIED assets "
                    "only, so a large movement in an unverified token is among the first "
                    "dropped — and a small onward hop can still be the one that reaches an "
                    "exchange."
                ),
            )
        )
    if coverage.unexplored_frontier:
        caveats.append(
            Caveat(
                code="unexplored_frontier",
                headline=(
                    f"{coverage.unexplored_frontier} address(es) were queued but never explored."
                ),
                detail=(
                    "The run ended with work still on the frontier. Each of these is an address "
                    "the trace reached and did not read — a lead that remains open."
                ),
            )
        )
    if coverage.mixer_crossings:
        caveats.append(
            Caveat(
                code="mixer_crossings",
                headline=(
                    f"{coverage.mixer_crossings} branch(es) were continued past a mixer by "
                    "heuristic, not by following value."
                ),
                detail=(
                    "At a mixer the deposit-to-withdrawal link is severed by design. Where this "
                    "run continued, it SELECTED which withdrawal to follow using a published "
                    "heuristic; it did not observe the connection. Every address beyond such a "
                    "crossing may belong to an unrelated party, and nothing downstream of one is "
                    "offered as a traced result."
                ),
            )
        )
    if coverage.mixer_stops:
        caveats.append(
            Caveat(
                code="mixer_stops",
                headline=f"{coverage.mixer_stops} branch(es) ended at a mixer.",
                detail=(
                    "The trail reached a mixing service and no onward candidate could be "
                    "proposed. These are hard stops in this run, not evidence that the funds "
                    "stopped moving — the value continues past them unattributed."
                ),
            )
        )
    return caveats


def _pursuit_caveats(coverage: TraversalCoverage) -> list[Caveat]:
    """What the run granted itself, in the section a reader checks for limits.

    Kept out of ``_coverage_caveats`` because an extension is not a gap: a run
    that extended eight times and then read every address it reached has a clean
    coverage record, and this caveat must not be the reason "no coverage gaps
    were recorded" goes unsaid. It is disclosed here for the opposite reason —
    the header prints the budget the operator authorised, and without this the
    document would show an answer bought with several times that and never
    mention it.
    """
    if not coverage.budget_extensions:
        return []
    return [
        Caveat(
            code="budget_extended",
            headline=(
                f"This run extended its own budget {len(coverage.budget_extensions)} time(s) "
                "to keep looking for a named endpoint."
            ),
            detail=(
                "A cost budget ran out while an objective still had no named endpoint and "
                "addresses were still queued, so the run granted itself another allowance of "
                "the same size and carried on, up to a fixed ceiling. It did so "
                + "; ".join(coverage.budget_extensions)
                + ". The search DEPTH was never extended — that is the operator's setting and "
                "it decides what the trace means — so this run cost more than the budget in "
                "the header while asking exactly the question that header states."
            ),
        )
    ]


def _finding_caveats(findings: Sequence[Finding], coverage: TraversalCoverage) -> list[Caveat]:
    """Caveats the findings themselves carry: cut trails, and the engine's own words.

    Mixer contacts are called out by finding kind rather than by reading prose.
    Everything else is lifted VERBATIM from ``engine_observation`` evidence: the
    engine already states where it capped an expansion or stopped short, and
    re-describing those statements in the report's own words is how a gap gets
    softened. Deduplicated by text, since a per-run coverage sentence is
    attached to every terminal finding and repeating it eight times buries the
    statements that differ.
    """
    caveats: list[Caveat] = []
    mixers = [f for f in findings if f.kind is FindingKind.MIXER_INTERACTION]
    if mixers:
        addresses = ", ".join(sorted({f.subject.value for f in mixers}))
        # The wording has to follow what this run actually DID. "value passing
        # through is not guessed at" was true while every mixer was a full stop;
        # printed over a run that crossed one, it contradicts the crossing
        # caveat three lines above it — in the section a reader consults
        # precisely to learn what the report cannot support.
        detail = (
            "Funds reached a known mixer, where the deposit-to-withdrawal link is severed by "
            "design. CipherChain does not de-anonymize the pool. Where this run continued past "
            "one, it SELECTED a candidate withdrawal by published heuristic and marked "
            "everything beyond as speculative; that branch is a lead, not a traced path, and "
            "the true onward path may be one this report never names."
            if coverage.mixer_crossings
            else (
                "Funds reached a known mixer and the branch stops there. CipherChain does not "
                "attempt de-anonymization, so value passing through cannot be followed and "
                "is not guessed at. What happened on the far side of the mixer is unknown "
                "to this report."
            )
        )
        caveats.append(
            Caveat(
                code="mixer_contact",
                headline=(
                    f"The trail reached {len(mixers)} mixer contact(s)."
                    if coverage.mixer_crossings
                    else f"The trail was cut at {len(mixers)} mixer contact(s)."
                ),
                detail=detail,
                subject=addresses,
            )
        )
    seen: set[str] = set()
    for finding in findings:
        for evidence in finding.evidence:
            if evidence.kind is not EvidenceKind.ENGINE_OBSERVATION:
                continue
            if evidence.summary in seen:
                continue
            seen.add(evidence.summary)
            caveats.append(
                Caveat(
                    code="engine_observation",
                    headline="The engine's own statement about what it examined:",
                    detail=evidence.summary,
                    subject=finding.subject.value,
                )
            )
    return caveats


def derive_caveats(
    header: ReportHeader,
    sections: Sequence[AnswerSection],
    findings: Sequence[Finding],
    coverage: TraversalCoverage,
) -> tuple[Caveat, ...]:
    """Every gap this run recorded — and, when there are none, that fact.

    The no-gap branch is the point of the function. "Nothing to report" rendered
    as an omitted section reads as completeness, so it is rendered as a caveat
    that says what a clean coverage record does and does not prove.

    Budget extensions are appended AFTER that branch on purpose: they are effort
    spent rather than ground unread, and a run that extended its budget and then
    read everything it reached still owes the reader "no coverage gaps were
    recorded". Folding them in above would have deleted that sentence for
    exactly the runs that worked hardest to earn it.
    """
    caveats = [
        *_status_caveats(header),
        *_answer_caveats(sections),
        *_coverage_caveats(coverage),
        *_finding_caveats(findings, coverage),
    ]
    if not caveats:
        caveats.append(
            Caveat(
                code="no_gaps_recorded",
                headline="No coverage gaps were recorded for this run.",
                detail=(
                    "Every address the trace reached was explored within budget, and no history "
                    "was left partly read. That is a statement about what CipherChain examined — "
                    "not proof that no other funds moved. The trace saw what the configured "
                    "sources returned for the addresses it visited, and nothing beyond them."
                ),
            )
        )
    caveats.extend(_pursuit_caveats(coverage))
    return tuple(caveats)


def build_report(
    *,
    header: ReportHeader,
    findings: Sequence[Finding],
    answers: Sequence[DirectionAnswer],
    coverage: TraversalCoverage | None = None,
    profiles: Mapping[str, VaspProfile] | None = None,
) -> InvestigationReport:
    """Assemble a renderable report from one investigation's domain objects.

    Pure and synchronous by design: the document's content is decided without a
    database, a browser, or a clock, so every claim it makes is assertable in a
    unit test. ``collect_report`` supplies the arguments from storage.

    Findings already shown as an answer are not repeated in the trailing list.
    That is matched by VALUE rather than identity, because the answer findings
    and the full finding list are reconstructed by separate queries and are
    equal objects, not the same ones.
    """
    resolved_coverage = coverage or TraversalCoverage()
    sections = build_answer_sections(answers, profiles)
    surfaced = [
        endpoint.finding
        for section in sections
        for endpoint in (section.nearest, section.nearest_named, section.best_effort)
        if endpoint is not None
    ]
    others = tuple(
        f for f in findings if not (f.kind is FindingKind.VASP_ENDPOINT and f in surfaced)
    )
    return InvestigationReport(
        header=header,
        answers=sections,
        other_findings=others,
        coverage=resolved_coverage,
        caveats=derive_caveats(header, sections, findings, resolved_coverage),
    )

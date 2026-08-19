"""Selecting the answer to an objective from the findings a run produced.

One direction can now yield three genuinely different answers, and the tool is
not allowed to quietly pick one:

- **nearest** — the closest attributed endpoint, whatever its basis. Answers
  "what is closest to this address".
- **nearest named** — the closest endpoint carrying a ``third_party_claim``.
  Answers "what can I actually act on", which is a different question: an
  investigator can subpoena Binance, and cannot subpoena "custodial
  infrastructure, operator unnamed, 61%".
- **best effort** — the best guess this direction can offer when it has no
  answer at all. Answers "you found nothing solid; what have you got?", which
  is the question the reader is left holding when the first two are empty.

Before labels were read at discovery (ATTRIBUTION_AT_DISCOVERY.md) these almost
never coexisted, so the frontend took the first VASP finding it saw and was
usually right. Now they routinely coexist in the same direction, and "first
recorded" is traversal order — a hop-1 behavioural inference filed before a
hop-2 sourced label would take the headline and hide a named exchange behind a
guess. Choosing between them by confidence or by hop would bake an invisible
judgement into the report; showing both, labelled, does not.

The third slot is the same principle applied to silence. REACHING_THE_VASP.md
§4: a report whose only line is "no named endpoint" is not usable by the body
it is written for, so the lead is offered — and offered *marked*, because a
weak lead that reaches a regulator looking like a strong one costs the strong
ones their credibility when it comes back wrong. Two properties keep that
honest and both are structural rather than conventional:

1. ``best_effort`` is populated only where ``nearest`` is empty
   (``DirectionAnswer`` refuses to hold both), so a guess can never displace or
   outrank a real answer.
2. It cannot be built without a plain-language ``weakness`` string, so no
   consumer can print the name and drop the caveat.

Speculative endpoints — anything downstream of a mixer crossing — are barred
from ``nearest`` and ``nearest_named`` entirely and can only ever reach the
best-effort slot. That is the whole reason the slot exists: with the bar in
place, a run that crossed a mixer would otherwise report nothing at all.

This lives in the domain rather than in the frontend on purpose: every consumer
that states "nearest previous/next VASP" — the demo UI, and any report built
later — must state the same thing.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from cipherchain.core.models import Direction, Evidence, EvidenceKind, Finding, FindingKind

#: A basis that is a versioned heuristic id — ``name@version``, no spaces —
#: rather than a sentence.
#:
#: This exists because the two shapes cannot be worded the same way, and only
#: one of them is what production supplies. ``nodes.speculative_basis`` holds
#: the ID: the schema documents it as "WHICH heuristic proposed it, e.g.
#: 'mixer-exit-address-match@1'", ``_enqueue_mixer_candidates`` writes
#: ``candidate.heuristic`` into it, and descendants inherit that same string. A
#: prose basis only ever arrives from a ``RankedFinding`` built by hand.
#:
#: Dropped into a sentence after a colon, an ID reads as the REASON the branch
#: is a guess — and "mixer-exit-anonymity-set@1" is not a reason anybody can
#: weigh. Named as what it is, it is useful: it tells a reader exactly which
#: published rule to go and read.
_HEURISTIC_ID = re.compile(r"^\S+@\S+$")


#: The shape a claim has to take before a NAME can be read out of it. The
#: engine writes claim summaries as "<entity> labeled '<category>'"; anything
#: else is a claim about the address that names nobody.
_CLAIM_ENTITY = re.compile(r"^(?P<entity>.+?) labeled '(?P<category>[^']+)'$")


def claim_evidence(finding: Finding) -> tuple[Evidence, ...]:
    """The third-party claims backing a finding — the only kind that names anyone."""
    return tuple(e for e in finding.evidence if e.kind is EvidenceKind.THIRD_PARTY_CLAIM)


def naming_claim(finding: Finding) -> Evidence | None:
    """The one claim an operator's NAME is read out of — not merely any claim.

    A finding can carry several claims (a VASP label and a sanctions listing,
    say). Dating the name against whichever claim happened to be first would put
    a fresh date under a stale attribution, so the claim that yields the name is
    the claim that gets quoted. Falls back to the first claim only to describe a
    finding whose claims name nobody at all.
    """
    claims = claim_evidence(finding)
    for evidence in claims:
        if _CLAIM_ENTITY.match(evidence.summary):
            return evidence
    return claims[0] if claims else None


def claim_entity(finding: Finding) -> str | None:
    """The operator named by this finding's claim, or None if nobody is named."""
    evidence = naming_claim(finding)
    if evidence is None:
        return None
    matched = _CLAIM_ENTITY.match(evidence.summary)
    return matched.group("entity") if matched else None


def is_named(finding: Finding) -> bool:
    """Does this finding NAME an operator, on a source that can be cited?

    The same test the ``answered`` gate uses (engine ``_finish_partial``): a
    behavioural inference describes what an address does, never who runs it.

    An operator name must actually come OUT of the claim. This used to ask only
    whether a third_party_claim existed, and plenty of legitimate claims name
    nobody: "address identified as mixer 'Tornado Cash'" is a sourced claim
    about the address itself, not an attribution to a company. Under the old
    test such a finding satisfied this gate, so an objective closed as ANSWERED
    with no operator, and the report — whose actionability lines all hang off
    this — told an investigator a legal request could be served on a respondent
    it could not name. The claim stays visible as evidence either way; it simply
    does not answer "who runs this address".
    """
    return claim_entity(finding) is not None


@dataclass(frozen=True, slots=True)
class RankedFinding:
    """A finding with the traversal facts the conclusion itself does not carry.

    Hop cannot be read off the finding — it belongs to the traversal, not the
    conclusion — so it is joined in at read time. Without it "nearest" would
    have to be inferred from insertion order, which stopped tracking hop order
    the moment labels began resolving at discovery.

    ``speculative`` is joined in the same way and for a harder reason. It is a
    property of the *path* to the node, never of the finding: a mixer-exit
    branch can reach an address a sourced label names perfectly well, and the
    label stays true while the path to it remains a guess. Nothing in the
    finding records that, so a reader of findings alone cannot tell a traced
    endpoint from a selected one — which is precisely the failure
    REACHING_THE_VASP.md §3 is built around.

    Both speculation fields default so the wiring can adopt them a caller at a
    time; a caller that does not supply them reports its findings as traced,
    which is what every caller meant before mixer crossing existed.
    """

    finding: Finding
    hop: int
    # False means "reached by observed movement". True means the path crossed a
    # link this engine SELECTED rather than witnessed, and stays true for every
    # descendant of that crossing — speculation is inherited and sticky.
    speculative: bool = False
    # WHY the path is a guess, as ``nodes.speculative_basis`` records it: the
    # id of the versioned heuristic that proposed the crossing
    # ("mixer-exit-anonymity-set@1"), inherited unchanged by every descendant.
    # It is an identifier, not a sentence — see ``_weakness_of``, which words
    # it as one. A prose basis is accepted too, for a caller ranking findings
    # by hand outside a traversal.
    speculative_basis: str | None = None

    @property
    def named(self) -> bool:
        return is_named(self.finding)

    @property
    def claim_backed(self) -> bool:
        """Is a sourced claim attached, whether or not it names an operator?

        Deliberately weaker than ``named``, and the two are needed separately.
        ``named`` gates whether an objective is ANSWERED, and that must require
        an operator. But an endpoint a sanctions list has something to say about
        is worth putting in front of a reader even though it identifies no
        company — so selection for the report turns on this, and actionability
        still turns on ``named``. Collapsing the two either promises a
        respondent that does not exist, or drops a sanctions hit off the page.
        """
        return bool(claim_evidence(self.finding))


@dataclass(frozen=True, slots=True)
class BestEffortFinding(RankedFinding):
    """A lead offered where there is no answer — with its weakness attached.

    ``weakness`` has no default and may not be blank, the same enforcement
    ``MixerCandidate`` uses one layer down and for the same reason: a lead whose
    caveat can be dropped somewhere downstream is indistinguishable in a filing
    from a signature-verified hop. There is no constructor path that produces a
    named lead with no stated reason to doubt it, so "show the weakness" is not
    a rule a renderer has to remember.

    It is a *subclass* rather than a separate pair of fields so that consumers
    already written against ``RankedFinding`` — the report model, the API
    mappers — keep working on it unchanged, and so the caveat travels with the
    endpoint rather than beside it in some parallel structure that a refactor
    could separate.
    """

    weakness: str = field(kw_only=True)

    def __post_init__(self) -> None:
        if not self.weakness.strip():
            raise ValueError("a best-effort answer must state its weakness in plain language")


@dataclass(frozen=True, slots=True)
class DirectionAnswer:
    """What one objective can be answered with, honestly.

    ``best_effort`` defaults to absent, which is also what it must be whenever
    ``nearest`` holds anything: the constructor rejects the pair outright rather
    than trusting selection to keep the invariant. A guess sitting beside a real
    answer is the one arrangement this slot must never be able to produce —
    every consumer renders ``best_effort`` in the headline position
    (REACHING_THE_VASP.md §4), so a stray one would take the headline off an
    endpoint that was actually traced.
    """

    direction: Direction
    nearest: RankedFinding | None
    nearest_named: RankedFinding | None
    best_effort: BestEffortFinding | None = None

    def __post_init__(self) -> None:
        if self.best_effort is not None and self.nearest is not None:
            raise ValueError(
                "best_effort fills silence and never competes: it cannot be set "
                "alongside a nearest endpoint"
            )

    @property
    def same(self) -> bool:
        """True when the nearest endpoint is itself named — one answer, not two.

        Identity, never equal-looking: one row is correct only when there is
        genuinely one fact to show. Two addresses at the same hop distance stay
        two rows even though "nearest" and "nearest named" then print the same
        distance, because an investigator is entitled to know a same-distance
        address exists that we are only 62% sure about.
        """
        return (
            self.nearest is not None
            and self.nearest_named is not None
            and self.nearest.finding is self.nearest_named.finding
        )


def _closest(candidates: Iterable[RankedFinding]) -> RankedFinding | None:
    """Nearest first, then unnamed, then the stronger claim.

    The middle term is the load-bearing one, and it looks backwards until you
    read it against the other slot. A guess and a sourced label tied at the same
    hop are two different addresses, so preferring the label here would put it in
    BOTH slots and collapse the pair to one row — silently deleting the guess
    from the headline. Preferring the unnamed one costs nothing, because the
    named slot is about to show the label anyway. So the two slots together
    surface two facts wherever two exist.

    Ties are only ever broken among equals after that: strongest confidence
    wins between two guesses. Never insertion order, which is traversal order
    and means nothing to a reader.
    """
    return min(candidates, key=lambda r: (r.hop, r.named, -r.finding.confidence), default=None)


def _best_lead(candidates: Iterable[RankedFinding]) -> RankedFinding | None:
    """Nearest first, then NAMED, then the stronger claim.

    The middle term is deliberately the reverse of ``_closest``'s, and the
    reversal is not an inconsistency — it falls out of there being one slot here
    instead of two. ``_closest`` prefers the unnamed candidate because the named
    slot beside it is about to show the label anyway, so preferring it there
    would collapse two facts into one row. Nothing sits beside this slot. A
    reader who gets one lead and no answer needs the lead that can actually be
    filed on, and "Kraken, via a mixer" is filable in a way that "custodial
    infrastructure, operator unnamed" is not.
    """
    return min(candidates, key=lambda r: (r.hop, not r.named, -r.finding.confidence), default=None)


def _weakness_of(lead: RankedFinding) -> str:
    """The plain-language caveat printed beside a best-effort answer.

    Built from the node's own ``speculative_basis``, and the wording turns on
    WHAT that basis is, because the field carries an identifier and not a
    sentence. ``nodes.speculative_basis`` is the heuristic id — the schema says
    so and ``_enqueue_mixer_candidates`` writes ``candidate.heuristic`` into it
    — so the string that arrives here in production is
    ``mixer-exit-anonymity-set@1``. Interpolated after a colon, as this used to
    do, the sentence a regulator read was "reached only by following a
    speculative branch: mixer-exit-anonymity-set@1", which offers a reason that
    cannot be weighed in the one field ``BestEffortFinding`` exists to make
    unskippable. Named as a rule instead, the id earns its place: it is what a
    reader looks up to check the arithmetic.

    A prose basis is still interpolated as prose — a caller can rank findings
    by hand — and a missing one degrades to a generic clause rather than
    emptying the string, which is the value ``BestEffortFinding`` refuses. So
    all three shapes produce a sentence.

    The engine's actual arithmetic ("one of 412 withdrawals in the anonymity
    set within 7 days") is NOT lost by this: the mixer crossing records the
    candidate's own weakness verbatim as heuristic-inference evidence on the
    crossing finding (``_mixer_crossed_finding``), so it reaches the same
    document. What this line must not do is claim to be quoting it.

    The "lead, not an attribution" phrase is appended only when the basis has
    not already said it — the mixer module ends its own weakness strings that
    way, and printing it twice in one line reads as a template rather than as a
    warning, which is how a warning stops being read.
    """
    basis = (lead.speculative_basis or "").strip().rstrip(".")
    if not basis:
        opening = (
            "reached only by following a speculative branch — the link into it "
            "was inferred, not observed"
        )
    elif _HEURISTIC_ID.match(basis):
        opening = (
            "reached only by following a speculative branch — the link into it was "
            f"selected by the heuristic {basis}, not observed"
        )
    else:
        opening = f"reached only by following a speculative branch: {basis}"
    parts = [opening]
    if "attribution" not in parts[0].lower():
        parts.append("this is a lead, not an attribution")
    text = "; ".join(parts)
    if not lead.named:
        # Worth stating separately: the weakness above is about the PATH. This
        # one is about the endpoint, and a reader must not take a marked path to
        # mean the destination was at least identified.
        text += ", and no source names the operator here"
    return text


def select_answers(
    ranked: Sequence[RankedFinding], directions: Iterable[Direction]
) -> list[DirectionAnswer]:
    """The answers per direction: the closest endpoint, the closest named one, a lead.

    Speculative endpoints are held out of the first two slots rather than ranked
    below them. A guess and a traced endpoint are not the same kind of statement,
    so ordering them against each other would only decide which one appears —
    and whichever appeared would be printed under a heading ("nearest previous
    VASP") that asserts a path was followed.
    """
    answers: list[DirectionAnswer] = []
    for direction in directions:
        here = [
            r
            for r in ranked
            if r.finding.kind is FindingKind.VASP_ENDPOINT and r.finding.direction is direction
        ]
        traced = [r for r in here if not r.speculative]
        nearest = _closest(traced)
        # Only ever consulted where the direction produced nothing traced, so a
        # lead cannot displace an answer even before DirectionAnswer refuses it.
        lead = None if nearest is not None else _best_lead([r for r in here if r.speculative])
        answers.append(
            DirectionAnswer(
                direction=direction,
                nearest=nearest,
                nearest_named=_closest([r for r in traced if r.claim_backed]),
                # Field by field rather than a copy helper: the lead must arrive
                # as a BestEffortFinding, which is the only shape that carries a
                # weakness, and a field added to RankedFinding has to be added
                # here too or it silently stops reaching this slot.
                best_effort=None
                if lead is None
                else BestEffortFinding(
                    finding=lead.finding,
                    hop=lead.hop,
                    speculative=lead.speculative,
                    speculative_basis=lead.speculative_basis,
                    weakness=_weakness_of(lead),
                ),
            )
        )
    return answers

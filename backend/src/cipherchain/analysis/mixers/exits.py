"""Mixer exit heuristics — crossing a Tornado-style pool without inventing a link.

A mixer severs the deposit-to-withdrawal link on purpose. "Tracing through"
one is therefore not a lookup; it is a *selection*, and most of the time the
thing selected belongs to a stranger. The failure this module is built around
is not missing a link — it is emitting one that reads exactly like a clean
hop. REACHING_THE_VASP.md §3 states the consequence plainly: a confident path
to an exchange account with no connection to the case, with nothing in the
report marking it as a guess.

Two structural properties exist to make that failure impossible rather than
unlikely:

**1. A candidate cannot be built without a weakness.** ``MixerCandidate``
takes ``weakness`` as a required field and rejects a blank one. There is no
constructor path that produces a named candidate with no stated reason to
doubt it, so no renderer downstream can drop the caveat while keeping the
name. This is the whole safety property of the module; everything else is
ranking.

**2. Direction is carried by the argument type, not by a flag.** The public
entry points are ``trace_back_from_withdrawal`` and
``trace_forward_from_deposit``. Handing the forward function a withdrawal is
a type error, not a wrong answer.

Why that second point earns a type distinction
----------------------------------------------
The candidate set is drawn from the *opposite* end of the pool, and the two
ends are not interchangeable:

    backward (where did this money come from?)
        anchor = a WITHDRAWAL   candidates = DEPOSITS strictly BEFORE it
    forward  (where did this money go?)
        anchor = a DEPOSIT      candidates = WITHDRAWALS strictly AFTER it

Swapping those enumerates events that could not possibly be related, and the
output is byte-for-byte indistinguishable from a working one — same shape,
same confidences, same weakness strings, entirely wrong addresses. It is the
quietest bug available here, which is why ``MixerDeposit`` and
``MixerWithdrawal`` are separate types despite holding identical fields, and
why the ladder below is written *once* over a normalised internal event with
the direction applied at exactly two places (which side supplies candidates,
and the sign of the time comparison). A per-direction copy of the ladder is
how the swap gets written.

The ladder, strongest first
---------------------------
Evaluated in order; the first rung that fires wins and the rest are not
consulted. A run that resolves a pool by address match must never also spend
budget enumerating a crowd.

1. ``address match``      — the same address deposited and withdrew. Decisive
                            about the address; still not decisive about the
                            *subject*, because that address may be a service.
2. ``linked address``     — a depositor and a withdrawer transacted directly
                            outside the mixer.
3. ``unique gas price``   — a hand-set, pre-EIP-1559 gas price appearing
                            exactly once on each side of the pool.
4. ``multi-denomination`` — the multiset of pool denominations one party used
                            is matched by exactly one party on the far side.
5. ``anonymity set``      — nothing above fired: the crowd in the window is
                            the answer, capped and ranked, and labelled a lead
                            rather than an attribution.

Rungs 1-4 are identity signals, so they deliberately ignore the Δ window and
respect only the direction constraint. Boxing a decisive address match into
seven days would discard real matches to buy nothing; Δ exists to size the
*crowd* in rung 5, and nowhere else.

Rung 4 refuses rather than guesses. A fingerprint matched by two parties is
not a fingerprint, so the rung declines and the branch falls to 5 — the same
posture ``clustering/cospend.py`` takes when two entities collide inside one
cluster. Picking a winner there would publish a name derived from a signal we
have just shown to be non-distinctive.

Addresses are integer ids, as everywhere else in ``analysis``. String
addresses would drag in a case-folding decision that is right for EVM and
catastrophic on base58 chains, where case is significant.

Nothing here touches a database, a provider, or the engine. The inputs are
plain values the caller assembles from the fact store, which is what makes
both directions testable without a session.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from cipherchain.core.models import Direction

# ── heuristic ids ─────────────────────────────────────────────────────────
# Stable strings: they land in evidence rows and in reports that outlive the
# code, so a change of behaviour bumps the version rather than editing these.

ADDRESS_MATCH_HEURISTIC = "mixer-exit-address-match@1"
LINKED_ADDRESS_HEURISTIC = "mixer-exit-linked-address@1"
GAS_PRICE_HEURISTIC = "mixer-exit-unique-gas-price@1"
MULTI_DENOMINATION_HEURISTIC = "mixer-exit-multi-denomination@1"
ANONYMITY_SET_HEURISTIC = "mixer-exit-anonymity-set@1"

#: The ladder in evaluation order. Exported so a report can render "which
#: rungs were tried" without hard-coding the sequence a second time.
MIXER_EXIT_LADDER: tuple[str, ...] = (
    ADDRESS_MATCH_HEURISTIC,
    LINKED_ADDRESS_HEURISTIC,
    GAS_PRICE_HEURISTIC,
    MULTI_DENOMINATION_HEURISTIC,
    ANONYMITY_SET_HEURISTIC,
)

# ── tuning constants ──────────────────────────────────────────────────────

#: Δ — how far either side of the anchor the anonymity-set fallback looks.
#:
#: **Seven days is a guess**, and REACHING_THE_VASP.md §9 says so in those
#: words: the value should be set from real Tornado withdrawal-delay
#: measurements and has not been. It is a named constant, and a parameter on
#: every entry point, precisely so that replacing it is a one-line change and
#: so no reader mistakes it for a measured threshold.
#:
#: It bounds rung 5 only. Rungs 1-4 are identity matches and ignore it.
MIXER_WINDOW = timedelta(days=7)

#: How many fallback candidates a single mixer crossing may hand back.
#:
#: The cap is a budget device, not a confidence one: following 400 branches
#: exhausts a run on a crowd that was never going to name anyone. When it
#: bites, the candidates dropped are the low-value tail, and every surviving
#: candidate says so in its weakness — an investigator must be able to see
#: that the subject may be among the ones not followed.
MAX_FOLLOW = 20

#: Ceiling on any mixer-exit confidence (RFC §3). Even a rung-1 address match
#: is capped: it proves what the *address* did, and an address can be a
#: service acting for thousands of customers.
MAX_MIXER_CONFIDENCE = 0.8

ADDRESS_MATCH_CONFIDENCE = 0.8
LINKED_ADDRESS_CONFIDENCE = 0.7
GAS_PRICE_CONFIDENCE = 0.65
MULTI_DENOMINATION_CONFIDENCE = 0.6

#: Rung 5 never exceeds this, even at N == 1. A Tornado withdrawal is not
#: paired to any deposit: the single candidate in a window may correspond to
#: a deposit from months earlier, and the subject may not have withdrawn at
#: all. "One candidate" is not "certainty" and the arithmetic must not be
#: able to say otherwise.
ANONYMITY_SET_MAX_CONFIDENCE = 0.5

#: A fingerprint spanning one pool is not a fingerprint — "deposited 1 ETH"
#: describes tens of thousands of people. Rung 4 requires the party's habits
#: to span at least this many distinct pools before it will match on them.
MIN_FINGERPRINT_POOLS = 2


# ── inputs ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MixerDeposit:
    """One deposit into a mixer pool, as plain values.

    Structurally identical to ``MixerWithdrawal`` on purpose *not* merged with
    it: the separate type is what makes handing a deposit to the backward
    entry point a type error instead of a confidently wrong answer.
    """

    tx_hash: str
    address_id: int
    pool: str
    denomination: int
    timestamp: datetime
    gas_price: int | None = None

    def __post_init__(self) -> None:
        _validate_event(self.tx_hash, self.pool, self.denomination)


@dataclass(frozen=True, slots=True)
class MixerWithdrawal:
    """One withdrawal out of a mixer pool. See ``MixerDeposit`` on the split."""

    tx_hash: str
    address_id: int
    pool: str
    denomination: int
    timestamp: datetime
    gas_price: int | None = None

    def __post_init__(self) -> None:
        _validate_event(self.tx_hash, self.pool, self.denomination)


@dataclass(frozen=True, slots=True)
class DirectInteraction:
    """A transfer between two addresses that did not run through the mixer.

    Rung 2's entire signal. Direction of the transfer is irrelevant — that
    two addresses transacted at all is the link — so the rung matches either
    orientation.
    """

    tx_hash: str
    from_address_id: int
    to_address_id: int

    def __post_init__(self) -> None:
        if not self.tx_hash:
            raise ValueError("an interaction must name its transaction")


@dataclass(frozen=True, slots=True)
class MixerActivity:
    """Everything observed at one mixer, across all of its pools.

    Deliberately mixer-wide rather than pool-wide: rung 4's fingerprint is a
    statement about a party's behaviour *across* denominations, and a
    pool-scoped view cannot express it.

    ``mixer_address_ids`` holds the pool contracts, routers and relayers
    themselves. They are excluded from the candidate set on every rung, for
    the reason ``cospend.build_clusters`` skips them: a mixer's own
    transactions belong to its participants, not to it. A router appears on
    both sides of its own pool and transacts directly with every user, so
    left in, it wins rung 2 against everybody and the crossing "resolves" by
    naming the mixer as the source of the money. Every time.
    """

    deposits: tuple[MixerDeposit, ...] = ()
    withdrawals: tuple[MixerWithdrawal, ...] = ()
    interactions: tuple[DirectInteraction, ...] = ()
    mixer_address_ids: frozenset[int] = frozenset()


# ── outputs ───────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MixerCandidate:
    """One party on the far side of the mixer that might be the subject.

    ``weakness`` has no default and may not be blank. That is not a style
    choice: it is the reason this module is safe to call. A candidate is a
    *lead*, and a lead whose caveat can be omitted somewhere downstream is
    indistinguishable in a report from a signature-verified hop.
    """

    heuristic: str
    direction: Direction
    address_id: int
    tx_hash: str
    pool: str
    timestamp: datetime
    value: int
    confidence: float
    weakness: str
    refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.heuristic or "@" not in self.heuristic:
            # Same rule the evidence constructors enforce: an inference must
            # name the versioned heuristic that produced it.
            raise ValueError("a mixer candidate must name its heuristic as 'name@version'")
        if not self.weakness.strip():
            raise ValueError("a mixer candidate must state its weakness in plain language")
        if not 0.0 < self.confidence <= MAX_MIXER_CONFIDENCE:
            raise ValueError(
                f"mixer-exit confidence must be in (0, {MAX_MIXER_CONFIDENCE}] — "
                "a crossing is never a certainty"
            )
        if self.value < 0:
            raise ValueError("a mixer candidate cannot carry a negative value")


@dataclass(frozen=True, slots=True)
class MixerExitResult:
    """What one mixer crossing produced, including the honest nothing.

    ``observation`` is always populated, including when ``candidates`` is
    empty. The empty case is a real result — REACHING_THE_VASP.md calls it
    "the honest no" — and it needs a sentence a report can print, otherwise a
    refusal renders as a blank where a reader expects a finding.

    ``anonymity_set`` is the size of the windowed crowd on the far side and is
    reported whichever rung fired, so a report can say "resolved by address
    match out of 412 contemporaneous deposits". ``capped`` is true only when
    rung 5 truncated that crowd to ``MAX_FOLLOW``.
    """

    direction: Direction
    anchor_tx_hash: str
    pool: str
    rung: str | None
    candidates: tuple[MixerCandidate, ...]
    anonymity_set: int
    capped: bool
    observation: str

    def __post_init__(self) -> None:
        if not self.observation.strip():
            raise ValueError("a mixer exit result must state what happened, including a refusal")

    @property
    def resolved(self) -> bool:
        """Did any rung produce something to follow?"""
        return bool(self.candidates)


# ── internals ─────────────────────────────────────────────────────────────


def _validate_event(tx_hash: str, pool: str, denomination: int) -> None:
    if not tx_hash:
        raise ValueError("a mixer event must name its transaction")
    if not pool:
        raise ValueError("a mixer event must name its pool — denomination is the whole lever")
    if denomination < 0:
        raise ValueError("a mixer event cannot carry a negative denomination")


@dataclass(frozen=True, slots=True)
class _Event:
    """Direction-free view of a deposit or a withdrawal.

    The ladder runs over this so it exists once rather than twice. The public
    types keep the direction distinction; by the time control reaches the
    rungs, direction lives in exactly one variable.
    """

    tx_hash: str
    address_id: int
    pool: str
    denomination: int
    timestamp: datetime
    gas_price: int | None


def _of_deposit(deposit: MixerDeposit) -> _Event:
    return _Event(
        tx_hash=deposit.tx_hash,
        address_id=deposit.address_id,
        pool=deposit.pool,
        denomination=deposit.denomination,
        timestamp=deposit.timestamp,
        gas_price=deposit.gas_price,
    )


def _of_withdrawal(withdrawal: MixerWithdrawal) -> _Event:
    return _Event(
        tx_hash=withdrawal.tx_hash,
        address_id=withdrawal.address_id,
        pool=withdrawal.pool,
        denomination=withdrawal.denomination,
        timestamp=withdrawal.timestamp,
        gas_price=withdrawal.gas_price,
    )


@dataclass(frozen=True, slots=True)
class _Match:
    """A rung's raw output: which event, and why to doubt it.

    ``reason`` is the rung-specific half of the weakness. The driver appends
    the crowd-wide half — how many addresses this rung named — because a rung
    cannot know that until all of its matches are in.
    """

    event: _Event
    reason: str
    refs: tuple[str, ...]


def _describe_window(window: timedelta) -> str:
    hours = window.total_seconds() / 3600.0
    if hours >= 24 and hours.is_integer() and int(hours) % 24 == 0:
        days = int(hours) // 24
        return f"{days} day{'' if days == 1 else 's'}"
    if hours.is_integer():
        whole = int(hours)
        return f"{whole} hour{'' if whole == 1 else 's'}"
    return str(window)


def _damped(base: float, distinct_addresses: int) -> float:
    """Split a rung's confidence across the addresses it named.

    A rung that names one address gets its full base. A rung that names three
    is offering three mutually exclusive stories, and at most one is true.
    Floored above zero so the candidate constructor's ``> 0`` guard cannot be
    tripped by arithmetic on a very wide match.
    """
    return max(round(base / max(distinct_addresses, 1), 3), 0.001)


def _rank(anchor: _Event, events: Iterable[_Event]) -> list[_Event]:
    """Value first, then closeness in time, then hash.

    Value leads because the RFC ranks follow-ups by value and because a pool
    with mixed denominations must not be ordered by timing alone. Inside a
    real Tornado pool every candidate carries the *same* denomination, so
    proximity to the anchor is what actually does the ordering — and tx_hash
    is the final tie-break purely so two runs over the same data agree.
    """
    return sorted(
        events,
        key=lambda e: (-e.denomination, abs(e.timestamp - anchor.timestamp), e.tx_hash),
    )


def _own_side_events(own_side: Sequence[_Event], anchor: _Event) -> list[_Event]:
    """The anchor's own party's events, with the anchor guaranteed present.

    Callers assemble ``MixerActivity`` from a query and may or may not include
    the transaction being traced. Rung 4's fingerprint would silently differ
    between those two callers, so the anchor is folded in here and duplicates
    are dropped by hash.
    """
    seen: dict[str, _Event] = {anchor.tx_hash: anchor}
    for event in own_side:
        if event.address_id == anchor.address_id and event.tx_hash not in seen:
            seen[event.tx_hash] = event
    return list(seen.values())


def _fingerprint(events: Iterable[_Event]) -> tuple[tuple[str, int], ...]:
    """The multiset of pools a party used, in a comparable, ordered form."""
    return tuple(sorted(Counter(event.pool for event in events).items()))


def _describe_fingerprint(fingerprint: tuple[tuple[str, int], ...]) -> str:
    return ", ".join(f"{count}x {pool}" for pool, count in fingerprint)


# ── the rungs ─────────────────────────────────────────────────────────────


def _rung_address_match(anchor: _Event, far_pool: Sequence[_Event]) -> list[_Match]:
    """Rung 1 — the same address is on both sides of the pool.

    Decisive about the *address*, which is not the same as decisive about the
    subject: a custodial deposit address, a relayer, or any wallet operating
    for several people produces this match for every one of its customers.
    That caveat is the weakness, and it is why even this rung is capped at
    0.8 rather than treated as a fact.
    """
    matches = [event for event in far_pool if event.address_id == anchor.address_id]
    reason = (
        "the same address is on both sides of this pool; if that address is run by a "
        "service on behalf of many customers, the match names the service and not the subject"
    )
    return [
        _Match(event=event, reason=reason, refs=_refs(anchor.tx_hash, event.tx_hash))
        for event in matches
    ]


def _rung_linked_address(
    anchor: _Event,
    far_pool: Sequence[_Event],
    interactions: Sequence[DirectInteraction],
) -> list[_Match]:
    """Rung 2 — the two parties transacted directly, away from the mixer.

    Links are not transitive here, and deliberately so. "Both parties paid the
    same intermediary" is not a link between them; chaining through one hop
    would connect most of a pool to most of the rest of it and turn the
    anonymity set into a list of named links — the exact output this package
    exists to prevent. The mixer's own contracts are already gone from
    ``far_pool`` before this rung sees it, which is what stops a router from
    winning this rung against every user it ever served.

    The anchor's *own* address needs no exclusion here: rung 1 matches exactly
    that case and returns before this rung is consulted. Adding a second guard
    for it would be a branch no input can reach, and an unreachable guard is
    a claim the tests cannot check.
    """
    links: dict[int, list[str]] = {}
    for interaction in interactions:
        endpoints = (interaction.from_address_id, interaction.to_address_id)
        if anchor.address_id not in endpoints:
            continue
        other = endpoints[1] if endpoints[0] == anchor.address_id else endpoints[0]
        links.setdefault(other, []).append(interaction.tx_hash)

    matches: list[_Match] = []
    for event in far_pool:
        if event.address_id not in links:
            continue
        linking = sorted(links[event.address_id])
        matches.append(
            _Match(
                event=event,
                reason=(
                    f"these two addresses transacted directly outside the mixer "
                    f"({linking[0]}); addresses that pay each other are linked, but the "
                    f"funds that went through the pool need not be the same funds"
                ),
                refs=_refs(anchor.tx_hash, event.tx_hash, *linking),
            )
        )
    return matches


def _rung_unique_gas_price(
    anchor: _Event,
    far_pool: Sequence[_Event],
    far_side: Sequence[_Event],
    own_side: Sequence[_Event],
) -> list[_Match]:
    """Rung 3 — one hand-set gas price, appearing exactly once on each side.

    Pre-EIP-1559 a user could type any gas price they liked, and people reuse
    the odd number they typed. The signal dies the moment the number is a
    client default, so uniqueness is checked across the *whole* mixer rather
    than within this pool: a price appearing on thirty deposits into other
    pools is a default, and matching on it would name a stranger with a
    straight face.

    A missing or non-positive gas price carries no signal at all and is not
    treated as a value that can match — the ``None``/``0`` conflation is the
    obvious way to make this rung fire on every transaction that lacks data.
    """
    price = anchor.gas_price
    if price is None or price <= 0:
        return []

    far_sharers = [event for event in far_side if event.gas_price == price]
    if len(far_sharers) != 1:
        return []
    own_sharers = [
        event
        for event in own_side
        if event.gas_price == price and event.tx_hash != anchor.tx_hash
    ]
    if own_sharers:
        return []

    candidate = far_sharers[0]
    # Unique across the mixer, but it only counts if it also sits in this
    # pool on the correct side of the anchor in time.
    if not any(event.tx_hash == candidate.tx_hash for event in far_pool):
        return []

    return [
        _Match(
            event=candidate,
            reason=(
                f"matched only on a gas price of {price} appearing exactly once on each "
                f"side of this mixer; a wallet or relayer that reuses one hand-set gas "
                f"price for unrelated users would produce the same match"
            ),
            refs=_refs(anchor.tx_hash, candidate.tx_hash),
        )
    ]


def _rung_multi_denomination(
    anchor: _Event,
    far_pool: Sequence[_Event],
    far_directional: Sequence[_Event],
    own_side: Sequence[_Event],
) -> list[_Match]:
    """Rung 4 — a distinctive multiset of pool denominations, matched once.

    Someone who moves 3x 10 ETH and 1x 1 ETH leaves a shape on the far side.
    The rung matches that shape exactly, across pools, and then applies the
    refusal that makes it worth anything: **if two parties share the
    fingerprint it is not a fingerprint**, so the rung declines and the branch
    falls through to the anonymity set. Choosing between two identical shapes
    would publish a name derived from a signal just shown to be
    non-distinctive — the posture ``cospend.propose_cluster_labels`` takes
    when one cluster carries two entity names.

    A single-pool shape is refused up front for the same reason: "deposited
    1 ETH" is a description of tens of thousands of people.
    """
    own_events = _own_side_events(own_side, anchor)
    fingerprint = _fingerprint(own_events)
    if len(fingerprint) < MIN_FINGERPRINT_POOLS:
        return []

    by_address: dict[int, list[_Event]] = {}
    for event in far_directional:
        by_address.setdefault(event.address_id, []).append(event)

    matched = [
        (address_id, events)
        for address_id, events in by_address.items()
        if _fingerprint(events) == fingerprint
    ]
    if len(matched) != 1:
        return []

    address_id, events = matched[0]
    # Cannot be empty, and the reason matters. ``own_events`` always contains
    # the anchor, so the fingerprint always names the anchor's own pool; a far
    # party whose fingerprint equals it therefore has at least one leg in that
    # pool, drawn from the same direction-filtered list ``far_pool`` is. A
    # guard here would be unreachable — and worse than useless, because the
    # only way to reach it is for ``far_directional`` to stop being direction
    # filtered, which is exactly the bug that must fail loudly rather than
    # quietly return nothing on some inputs and a wrong name on others.
    in_pool = [event for event in far_pool if event.address_id == address_id]
    best = _rank(anchor, in_pool)[0]
    total = sum(count for _, count in fingerprint)
    return [
        _Match(
            event=best,
            reason=(
                f"matched on a deposit-and-withdrawal fingerprint of {total} transactions "
                f"across {len(fingerprint)} pools ({_describe_fingerprint(fingerprint)}); "
                f"another user with the same habits in the same period would be "
                f"indistinguishable from the subject"
            ),
            refs=_refs(anchor.tx_hash, *(event.tx_hash for event in events)),
        )
    ]


def _refs(*hashes: str) -> tuple[str, ...]:
    """Sorted and de-duplicated, so two runs over the same data agree."""
    return tuple(sorted({h for h in hashes if h}))


# ── the driver ────────────────────────────────────────────────────────────


def _resolve(
    anchor: _Event,
    *,
    direction: Direction,
    own_side: Sequence[_Event],
    far_side: Sequence[_Event],
    interactions: Sequence[DirectInteraction],
    mixer_address_ids: frozenset[int],
    window: timedelta,
    max_follow: int,
) -> MixerExitResult:
    """Run the ladder once, for whichever direction the caller committed to.

    The two directions meet here and differ in exactly two things: which list
    the caller passed as ``far_side``, and the sign of the comparison below.
    Everything after that is shared, which is the point — a second copy of
    this function is where a direction bug would live.
    """
    if window <= timedelta(0):
        raise ValueError("the mixer window must be a positive duration")
    if max_follow < 1:
        raise ValueError("max_follow must allow at least one candidate")

    backward = direction is Direction.BACKWARD

    # The mixer is not a party to its own pool. Dropped once, here, so every
    # rung and the crowd count inherit it — a router left in the candidate set
    # resolves the crossing by naming the mixer, on every rung it touches.
    far_side = [event for event in far_side if event.address_id not in mixer_address_ids]

    def in_direction(event: _Event) -> bool:
        # Strict on both sides. An event sharing the anchor's timestamp is
        # excluded from BOTH directions: it cannot have funded a withdrawal
        # that happened in the same instant, and admitting it would let one
        # event be a candidate for the same anchor whichever way we trace.
        if backward:
            return event.timestamp < anchor.timestamp
        return event.timestamp > anchor.timestamp

    def in_window(event: _Event) -> bool:
        if backward:
            return anchor.timestamp - window <= event.timestamp
        return event.timestamp <= anchor.timestamp + window

    far_directional = [event for event in far_side if in_direction(event)]
    far_pool = [event for event in far_directional if event.pool == anchor.pool]
    crowd = [event for event in far_pool if in_window(event)]
    anonymity_set = len(crowd)

    far_noun = "deposits" if backward else "withdrawals"
    window_text = _describe_window(window)

    # Held as thunks, not results. Rung 4 groups every party on the far side
    # by fingerprint, which is the expensive one, and there is no reason to
    # pay for it when rung 1 has already named the address.
    ladder: tuple[tuple[str, float, Callable[[], list[_Match]]], ...] = (
        (
            ADDRESS_MATCH_HEURISTIC,
            ADDRESS_MATCH_CONFIDENCE,
            lambda: _rung_address_match(anchor, far_pool),
        ),
        (
            LINKED_ADDRESS_HEURISTIC,
            LINKED_ADDRESS_CONFIDENCE,
            lambda: _rung_linked_address(anchor, far_pool, interactions),
        ),
        (
            GAS_PRICE_HEURISTIC,
            GAS_PRICE_CONFIDENCE,
            lambda: _rung_unique_gas_price(anchor, far_pool, far_side, own_side),
        ),
        (
            MULTI_DENOMINATION_HEURISTIC,
            MULTI_DENOMINATION_CONFIDENCE,
            lambda: _rung_multi_denomination(anchor, far_pool, far_directional, own_side),
        ),
    )

    for heuristic, base, rung in ladder:
        matches = rung()
        if not matches:
            continue
        distinct = len({match.event.address_id for match in matches})
        confidence = _damped(base, distinct)
        ordered = _rank(anchor, [match.event for match in matches])
        by_hash = {match.event.tx_hash: match for match in matches}
        candidates = tuple(
            MixerCandidate(
                heuristic=heuristic,
                direction=direction,
                address_id=event.address_id,
                tx_hash=event.tx_hash,
                pool=event.pool,
                timestamp=event.timestamp,
                value=event.denomination,
                confidence=confidence,
                weakness=_weakness(by_hash[event.tx_hash].reason, distinct),
                refs=by_hash[event.tx_hash].refs,
            )
            for event in ordered
        )
        return MixerExitResult(
            direction=direction,
            anchor_tx_hash=anchor.tx_hash,
            pool=anchor.pool,
            rung=heuristic,
            candidates=candidates,
            anonymity_set=anonymity_set,
            capped=False,
            observation=(
                f"mixer exit resolved by {heuristic}: {len(candidates)} candidate "
                f"{far_noun} naming {distinct} address(es), out of {anonymity_set} "
                f"contemporaneous {far_noun} in pool {anchor.pool}"
            ),
        )

    # Rung 5. Nothing identified anybody, so the crowd itself is the answer.
    if not crowd:
        return MixerExitResult(
            direction=direction,
            anchor_tx_hash=anchor.tx_hash,
            pool=anchor.pool,
            rung=None,
            candidates=(),
            anonymity_set=0,
            capped=False,
            observation=(
                f"no {far_noun} in pool {anchor.pool} within {window_text} "
                f"{'before' if backward else 'after'} this transaction; the branch has "
                f"nothing to follow"
            ),
        )

    ordered_crowd = _rank(anchor, crowd)
    followed = ordered_crowd[:max_follow]
    capped = len(followed) < anonymity_set
    confidence = max(round(min(ANONYMITY_SET_MAX_CONFIDENCE, 1 / anonymity_set), 3), 0.001)
    weakness = (
        f"one of {anonymity_set} {far_noun} in the anonymity set within {window_text}; "
        f"this is a lead, not an attribution"
    )
    if capped:
        weakness += (
            f"; only the {len(followed)} highest-value of those are followed, so the "
            f"subject's may not be among them"
        )

    candidates = tuple(
        MixerCandidate(
            heuristic=ANONYMITY_SET_HEURISTIC,
            direction=direction,
            address_id=event.address_id,
            tx_hash=event.tx_hash,
            pool=event.pool,
            timestamp=event.timestamp,
            value=event.denomination,
            confidence=confidence,
            weakness=weakness,
            refs=_refs(anchor.tx_hash, event.tx_hash),
        )
        for event in followed
    )
    return MixerExitResult(
        direction=direction,
        anchor_tx_hash=anchor.tx_hash,
        pool=anchor.pool,
        rung=ANONYMITY_SET_HEURISTIC,
        candidates=candidates,
        anonymity_set=anonymity_set,
        capped=capped,
        observation=(
            f"no identifying heuristic fired; anonymity set of {anonymity_set} {far_noun} "
            f"in pool {anchor.pool} within {window_text}, following {len(followed)}"
        ),
    )


def _weakness(reason: str, distinct_addresses: int) -> str:
    if distinct_addresses <= 1:
        return reason
    return (
        f"{reason}; this heuristic named {distinct_addresses} different addresses here, "
        f"so at most one of them belongs to the subject"
    )


# ── public entry points ───────────────────────────────────────────────────


def trace_back_from_withdrawal(
    withdrawal: MixerWithdrawal,
    activity: MixerActivity,
    *,
    window: timedelta = MIXER_WINDOW,
    max_follow: int = MAX_FOLLOW,
) -> MixerExitResult:
    """Backward — *where did this money come from?*

    The anchor is a withdrawal the subject received, and the candidates are
    **deposits into the same pool that happened strictly before it**. Deposits
    after the withdrawal cannot have funded it and are never considered.

    Expect this direction to refuse more often than the forward one. A pool
    accumulates deposits over its whole life, so the backward crowd is
    typically the larger of the two. That asymmetry is real and is reported
    rather than smoothed over.
    """
    return _resolve(
        _of_withdrawal(withdrawal),
        direction=Direction.BACKWARD,
        own_side=[_of_withdrawal(w) for w in activity.withdrawals],
        far_side=[_of_deposit(d) for d in activity.deposits],
        interactions=activity.interactions,
        mixer_address_ids=activity.mixer_address_ids,
        window=window,
        max_follow=max_follow,
    )


def trace_forward_from_deposit(
    deposit: MixerDeposit,
    activity: MixerActivity,
    *,
    window: timedelta = MIXER_WINDOW,
    max_follow: int = MAX_FOLLOW,
) -> MixerExitResult:
    """Forward — *where did this money go?*

    The anchor is a deposit the subject made, and the candidates are
    **withdrawals from the same pool that happened strictly after it**.
    Withdrawals before the deposit cannot have spent it and are never
    considered.
    """
    return _resolve(
        _of_deposit(deposit),
        direction=Direction.FORWARD,
        own_side=[_of_deposit(d) for d in activity.deposits],
        far_side=[_of_withdrawal(w) for w in activity.withdrawals],
        interactions=activity.interactions,
        mixer_address_ids=activity.mixer_address_ids,
        window=window,
        max_follow=max_follow,
    )

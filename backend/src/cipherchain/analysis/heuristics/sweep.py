"""Sweep detection: value in, nearly all of it straight back out.

A *sweep* is an address that receives value and forwards essentially all of
it onward shortly afterwards, retaining nothing. It is the signature of
pass-through infrastructure — exchange deposit addresses being collected,
laundering hops, automated forwarders — and it is one of the strongest
signals that an address is a waypoint rather than a destination.

Deliberate limits, because an over-eager detector is worse than none:
- **Same asset only.** A swap is a different pattern; conflating them
  would silently misreport value.
- **Forward must follow receipt.** Time-respecting: value cannot leave
  before it arrived.
- **Ratio and delay thresholds are explicit**, and both feed confidence:
  a 99.9% forward within a minute is stronger evidence than 95% after two
  days, and the emitted confidence says so.

Emits inferences, never identity claims: "behaves as a pass-through
address" is supportable from the chain; "is Binance" is not.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta

from cipherchain.core.models import (
    Address,
    Evidence,
    EvidenceKind,
    Finding,
    FindingKind,
)
from cipherchain.storage.repositories import StoredMovement

SWEEP_HEURISTIC = "sweep@1"

DEFAULT_MIN_RATIO = 0.95
DEFAULT_MAX_DELAY = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class SweepMatch:
    received: StoredMovement
    forwarded: StoredMovement
    ratio: float
    delay: timedelta


def _confidence(ratio: float, delay: timedelta, max_delay: timedelta) -> float:
    """Higher when more value moves on, and faster. Capped below 1.0 —
    a heuristic is never certainty."""
    promptness = 1.0 - (delay.total_seconds() / max_delay.total_seconds())
    score = 0.55 + 0.30 * min(ratio, 1.0) + 0.10 * max(promptness, 0.0)
    return round(min(score, 0.95), 3)


def find_sweep_matches(
    incoming: Sequence[StoredMovement],
    outgoing: Sequence[StoredMovement],
    *,
    min_ratio: float = DEFAULT_MIN_RATIO,
    max_delay: timedelta = DEFAULT_MAX_DELAY,
) -> list[SweepMatch]:
    """Pair each receipt with the earliest qualifying onward movement.

    Each outgoing movement may satisfy only one receipt, so a single
    forward is never counted as sweeping several deposits.
    """
    if not (0.0 < min_ratio <= 1.0):
        raise ValueError("min_ratio must be in (0, 1]")
    if max_delay <= timedelta(0):
        raise ValueError("max_delay must be positive")
    matches: list[SweepMatch] = []
    consumed: set[int] = set()
    for received in sorted(incoming, key=lambda m: m.timestamp):
        if received.amount <= 0:
            continue
        candidates = [
            movement
            for movement in outgoing
            if movement.id not in consumed
            and movement.asset_id == received.asset_id  # same asset only
            # A different transaction: value arriving and leaving inside ONE
            # atomic transaction is a swap or router hop, not a wallet holding
            # funds and passing them on. Counting it as a sweep produced
            # self-referential evidence ("received in X, forwarded in X").
            and movement.transaction_id != received.transaction_id
            and movement.timestamp >= received.timestamp  # time-respecting
            and movement.timestamp - received.timestamp <= max_delay
            and movement.amount >= received.amount * min_ratio
            and movement.amount <= received.amount  # forwarded, not topped up
        ]
        if not candidates:
            continue
        forwarded = min(candidates, key=lambda m: (m.timestamp, m.id))
        consumed.add(forwarded.id)
        matches.append(
            SweepMatch(
                received=received,
                forwarded=forwarded,
                ratio=forwarded.amount / received.amount,
                delay=forwarded.timestamp - received.timestamp,
            )
        )
    return matches


def detect_sweeps(
    address: Address,
    incoming: Sequence[StoredMovement],
    outgoing: Sequence[StoredMovement],
    *,
    min_ratio: float = DEFAULT_MIN_RATIO,
    max_delay: timedelta = DEFAULT_MAX_DELAY,
) -> list[Finding]:
    """ONE aggregated finding per address, however many sweeps it performed.

    A relay wallet can sweep hundreds of times; emitting a finding per
    occurrence buries every other signal in the report. The behaviour being
    reported is a property of the ADDRESS ("this wallet passes funds
    through"), so it is stated once, with the count as the evidence of
    scale and the strongest occurrences cited.
    """
    matches = find_sweep_matches(incoming, outgoing, min_ratio=min_ratio, max_delay=max_delay)
    if not matches:
        return []

    strongest = max(matches, key=lambda m: (m.ratio, -m.delay.total_seconds()))
    confidence = _confidence(strongest.ratio, strongest.delay, max_delay)
    avg_ratio = sum(m.ratio for m in matches) / len(matches)
    fastest_h = min(m.delay for m in matches).total_seconds() / 3600
    # Cite the highest-ratio occurrences, sorted so the finding is
    # byte-reproducible from the same stored data.
    top = sorted(matches, key=lambda m: (-m.ratio, m.received.tx_hash))[:4]
    refs = tuple(sorted({h for m in top for h in (m.received.tx_hash, m.forwarded.tx_hash)}))[:8]

    if len(matches) == 1:
        headline = (
            f"{strongest.ratio * 100:.1f}% of a received amount forwarded after "
            f"{strongest.delay.total_seconds() / 3600:.1f}h — address behaves as a "
            f"pass-through"
        )
    else:
        headline = (
            f"pass-through: {len(matches)} receive-and-forward cycles "
            f"(avg {avg_ratio * 100:.1f}% forwarded, fastest {fastest_h:.1f}h)"
        )

    return [
        Finding(
            kind=FindingKind.SWEEP_PATTERN,
            subject=address,
            summary=headline,
            confidence=confidence,
            evidence=(
                Evidence(
                    kind=EvidenceKind.ONCHAIN_FACT,
                    summary=(
                        f"{len(matches)} matched receive/forward pair(s) of the same asset; "
                        f"strongest cited"
                    ),
                    refs=refs,
                ),
                Evidence(
                    kind=EvidenceKind.HEURISTIC_INFERENCE,
                    summary=(
                        f"best forwarded ratio {strongest.ratio:.4f} within "
                        f"{strongest.delay.total_seconds() / 3600:.1f}h; "
                        f"{len(matches)} such cycle(s) overall"
                    ),
                    heuristic=SWEEP_HEURISTIC,
                    confidence=confidence,
                    refs=refs,
                ),
            ),
        )
    ]

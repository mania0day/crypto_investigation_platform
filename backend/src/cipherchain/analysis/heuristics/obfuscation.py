"""Structural obfuscation detectors.

Every detector here works from stored movements alone — no address lists, no
external data — so they apply to any chain the moment its adapter lands.

The governing rule for all of them: **a forensics tool that cries wolf is
worse than none.** Each detector therefore states the legitimate behaviour
that looks identical to it, sets thresholds that exclude the common benign
case, and emits a HEURISTIC_INFERENCE describing *behaviour*, never an
accusation of intent. "This address fans value out to many recipients" is
supportable from the chain; "this address is laundering" is not.

Detectors implemented:

- ``peel_chain``    — a large balance walks forward, shedding a small slice
                      at each hop while the bulk continues. The classic way
                      to move a big sum without a big transfer.
- ``distribution``  — the outflow-dispersion axis: splitter, batch payout,
                      or equal-value (CoinJoin-shaped) outputs — one finding.
- ``fan_in``        — many senders consolidated into one address, the
                      collection side of the same pattern.
- ``rapid_hop``     — value arriving and leaving repeatedly within minutes,
                      i.e. an address used purely as a relay.
"""

from __future__ import annotations

from collections import Counter
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

PEEL_HEURISTIC = "peel-chain@1"
FAN_OUT_HEURISTIC = "fan-out@1"
FAN_IN_HEURISTIC = "fan-in@1"
EQUAL_SPLIT_HEURISTIC = "equal-split@1"
DISTRIBUTION_HEURISTIC = "batch-distribution@1"
RAPID_HOP_HEURISTIC = "rapid-hop@1"

# A peel keeps most of the value moving and sheds a minority slice.
PEEL_REMAINDER_MIN = 0.70  # bulk continues
PEEL_SLICE_MAX = 0.30  # slice shed
PEEL_WINDOW = timedelta(hours=6)

FAN_MIN_COUNTERPARTIES = 8  # below this, ordinary payment activity
FAN_WINDOW = timedelta(hours=6)

EQUAL_SPLIT_MIN_COUNT = 4  # 4+ identical amounts is not coincidence
RAPID_HOP_WINDOW = timedelta(minutes=30)
RAPID_HOP_MIN_HOPS = 3


def _confidence(base: float, strength: float) -> float:
    """Bounded below 1.0 — a heuristic is never certainty."""
    return round(min(base + 0.25 * min(strength, 1.0), 0.9), 3)


def _refs(*movements: StoredMovement, limit: int = 8) -> tuple[str, ...]:
    seen: list[str] = []
    for m in movements:
        if m.tx_hash not in seen:
            seen.append(m.tx_hash)
    return tuple(sorted(seen)[:limit])  # sorted: reproducible across processes


@dataclass(frozen=True, slots=True)
class _Window:
    incoming: list[StoredMovement]
    outgoing: list[StoredMovement]


def _same_asset_window(
    incoming: Sequence[StoredMovement],
    outgoing: Sequence[StoredMovement],
    asset_id: int,
    start: object,
    window: timedelta,
) -> _Window:
    ins = [m for m in incoming if m.asset_id == asset_id]
    outs = [
        m
        for m in outgoing
        if m.asset_id == asset_id and start <= m.timestamp <= start + window  # type: ignore[operator]
    ]
    return _Window(incoming=ins, outgoing=outs)


# ── peel chain ────────────────────────────────────────────────────────────


def detect_peel_chain(
    address: Address,
    incoming: Sequence[StoredMovement],
    outgoing: Sequence[StoredMovement],
) -> list[Finding]:
    """A large receipt leaves as one small slice plus one large remainder.

    Looks identical to: a merchant paying an invoice and returning change to
    themselves, or any wallet spending part of a balance. The 70/30 split
    plus the same-asset, same-window constraints are what separate a peel
    from ordinary spending — and it is still only an inference.
    """
    findings: list[Finding] = []
    for received in sorted(incoming, key=lambda m: m.timestamp):
        if received.amount <= 0:
            continue
        window = _same_asset_window(
            incoming, outgoing, received.asset_id, received.timestamp, PEEL_WINDOW
        )
        candidates = [m for m in window.outgoing if m.timestamp >= received.timestamp]
        if len(candidates) < 2:
            continue
        remainder = max(candidates, key=lambda m: m.amount)
        slice_ = min(candidates, key=lambda m: m.amount)
        if remainder.id == slice_.id:
            continue
        r_ratio = remainder.amount / received.amount
        s_ratio = slice_.amount / received.amount
        if not (PEEL_REMAINDER_MIN <= r_ratio <= 1.0 and 0 < s_ratio <= PEEL_SLICE_MAX):
            continue
        findings.append(
            Finding(
                kind=FindingKind.OBFUSCATION_PATTERN,
                subject=address,
                summary=(
                    f"peel: {s_ratio * 100:.1f}% shed while {r_ratio * 100:.1f}% of the "
                    f"received amount continued onward"
                ),
                confidence=_confidence(0.55, r_ratio),
                evidence=(
                    Evidence(
                        kind=EvidenceKind.ONCHAIN_FACT,
                        summary=(
                            f"received in {received.tx_hash}; onward in "
                            f"{remainder.tx_hash}; slice in {slice_.tx_hash}"
                        ),
                        refs=_refs(received, remainder, slice_),
                    ),
                    Evidence(
                        kind=EvidenceKind.HEURISTIC_INFERENCE,
                        summary=(
                            "one large remainder plus one small slice of the same asset "
                            "within the peel window — consistent with a peel chain, and "
                            "also with ordinary change-making"
                        ),
                        heuristic=PEEL_HEURISTIC,
                        confidence=_confidence(0.55, r_ratio),
                        refs=_refs(received, remainder, slice_),
                    ),
                ),
            )
        )
        break  # one peel finding per address is enough signal
    return findings


# ── fan-out / fan-in ──────────────────────────────────────────────────────


def _distinct(movements: Sequence[StoredMovement], attr: str) -> int:
    return len({getattr(m, attr) for m in movements if getattr(m, attr) is not None})


def _largest_equal_group(
    outgoing: Sequence[StoredMovement],
) -> tuple[int, int, list[StoredMovement]] | None:
    """The biggest same-asset group of byte-identical output values."""
    by_asset: dict[int, list[StoredMovement]] = {}
    for m in outgoing:
        by_asset.setdefault(m.asset_id, []).append(m)
    best: tuple[int, int, list[StoredMovement]] | None = None
    for movements in by_asset.values():
        counts = Counter(m.amount for m in movements if m.amount > 0)
        for amount, n in counts.most_common(1):
            if n >= EQUAL_SPLIT_MIN_COUNT and (best is None or n > best[1]):
                best = (amount, n, [m for m in movements if m.amount == amount])
    return best


def detect_distribution(
    address: Address,
    incoming: Sequence[StoredMovement],
    outgoing: Sequence[StoredMovement],
) -> list[Finding]:
    """ONE finding for the outflow-dispersion axis.

    Recipient fan-out and equal-value outputs are two views of the same
    behaviour: run separately they stamped every batch-payout contract in a
    report with an identical pair of findings ("splitter: 100 addresses" +
    "equal-value split: 100 outputs"), doubling the noise. Merged, the
    shape picks the headline:

    - many recipients, mostly identical values → *batch distribution*
      (airdrops, reward payouts, CoinJoin-style structuring all look so)
    - many recipients, varied values          → *splitter*
    - identical values to few recipients      → *equal-value split*
      (the CoinJoin shape on UTXO chains)

    Consolidation (fan-in) is the other axis and stays its own detector.
    """
    recipients = _distinct(outgoing, "to_address_id")
    equal = _largest_equal_group(outgoing)
    fanned = recipients >= FAN_MIN_COUNTERPARTIES

    if not fanned and equal is None:
        return []

    if fanned and equal is not None and equal[1] >= 0.8 * recipients:
        amount, n, matching = equal
        strength = min(recipients / (FAN_MIN_COUNTERPARTIES * 4), 1.0)
        summary = (
            f"batch distribution: {n} outputs of identical value across "
            f"{recipients} distinct addresses — automated payout, airdrop, or "
            f"CoinJoin-style structuring"
        )
        fact = f"{n} outgoing movements each of exactly {amount} (smallest unit)"
        inference = (
            f"outbound degree {recipients} with {n} byte-identical values; "
            f"reward payouts and airdrops produce the same shape"
        )
        heuristic, refs = DISTRIBUTION_HEURISTIC, _refs(*matching)
    elif fanned:
        strength = min(recipients / (FAN_MIN_COUNTERPARTIES * 4), 1.0)
        summary = f"splitter: value distributed to {recipients} distinct addresses"
        fact = f"{len(outgoing)} outgoing movements to {recipients} addresses"
        inference = (
            f"outbound degree {recipients} exceeds the splitter threshold "
            f"{FAN_MIN_COUNTERPARTIES}; batch payouts by a service look the same"
        )
        if equal is not None:  # minority equal subset: noted, not a second finding
            inference += f"; includes {equal[1]} outputs of identical value"
        heuristic, refs = FAN_OUT_HEURISTIC, _refs(*outgoing)
    else:
        assert equal is not None
        amount, n, matching = equal
        strength = min(n / (EQUAL_SPLIT_MIN_COUNT * 3), 1.0)
        summary = (
            f"equal-value split: {n} outputs of identical value — "
            f"structure consistent with CoinJoin-style mixing or an "
            f"automated splitter"
        )
        fact = f"{n} outgoing movements each of exactly {amount} (smallest unit)"
        inference = (
            f"{n} byte-identical output values of one asset; airdrops and "
            f"fixed-price payouts produce the same shape"
        )
        heuristic, refs = EQUAL_SPLIT_HEURISTIC, _refs(*matching)

    confidence = _confidence(0.5, strength)
    return [
        Finding(
            kind=FindingKind.OBFUSCATION_PATTERN,
            subject=address,
            summary=summary,
            confidence=confidence,
            evidence=(
                Evidence(kind=EvidenceKind.ONCHAIN_FACT, summary=fact, refs=refs),
                Evidence(
                    kind=EvidenceKind.HEURISTIC_INFERENCE,
                    summary=inference,
                    heuristic=heuristic,
                    confidence=confidence,
                    refs=refs,
                ),
            ),
        )
    ]


def detect_fan_in(
    address: Address,
    incoming: Sequence[StoredMovement],
    outgoing: Sequence[StoredMovement],
) -> list[Finding]:
    """Many senders consolidated into one address — the collection side.

    Looks identical to: an exchange deposit address, or a donation wallet.
    """
    senders = _distinct(incoming, "from_address_id")
    if senders < FAN_MIN_COUNTERPARTIES:
        return []
    strength = min(senders / (FAN_MIN_COUNTERPARTIES * 4), 1.0)
    return [
        Finding(
            kind=FindingKind.OBFUSCATION_PATTERN,
            subject=address,
            summary=f"consolidation: value collected from {senders} distinct addresses",
            confidence=_confidence(0.5, strength),
            evidence=(
                Evidence(
                    kind=EvidenceKind.ONCHAIN_FACT,
                    summary=f"{len(incoming)} incoming movements from {senders} addresses",
                    refs=_refs(*incoming),
                ),
                Evidence(
                    kind=EvidenceKind.HEURISTIC_INFERENCE,
                    summary=(
                        f"inbound degree {senders} exceeds the consolidation threshold "
                        f"{FAN_MIN_COUNTERPARTIES}; a service deposit address looks the same"
                    ),
                    heuristic=FAN_IN_HEURISTIC,
                    confidence=_confidence(0.5, strength),
                    refs=_refs(*incoming),
                ),
            ),
        )
    ]


# ── rapid relay ───────────────────────────────────────────────────────────


def detect_rapid_hop(
    address: Address,
    incoming: Sequence[StoredMovement],
    outgoing: Sequence[StoredMovement],
) -> list[Finding]:
    """Value repeatedly arriving and leaving within minutes.

    An address used as a relay rather than a destination. Looks identical
    to: an automated market-maker or bot wallet, which also never rests.
    """
    pairs = 0
    used: set[int] = set()
    for received in sorted(incoming, key=lambda m: m.timestamp):
        for sent in sorted(outgoing, key=lambda m: m.timestamp):
            if sent.id in used or sent.asset_id != received.asset_id:
                continue
            delta = sent.timestamp - received.timestamp
            if timedelta(0) <= delta <= RAPID_HOP_WINDOW:
                used.add(sent.id)
                pairs += 1
                break
    if pairs < RAPID_HOP_MIN_HOPS:
        return []
    strength = min(pairs / (RAPID_HOP_MIN_HOPS * 3), 1.0)
    sample = [m for m in outgoing if m.id in used]
    return [
        Finding(
            kind=FindingKind.OBFUSCATION_PATTERN,
            subject=address,
            summary=(
                f"relay: {pairs} receive-then-forward cycles within "
                f"{int(RAPID_HOP_WINDOW.total_seconds() // 60)} minutes each"
            ),
            confidence=_confidence(0.5, strength),
            evidence=(
                Evidence(
                    kind=EvidenceKind.ONCHAIN_FACT,
                    summary=f"{pairs} inflow/outflow pairs of the same asset in quick succession",
                    refs=_refs(*sample),
                ),
                Evidence(
                    kind=EvidenceKind.HEURISTIC_INFERENCE,
                    summary=(
                        f"{pairs} rapid receive-forward cycles; trading bots and market "
                        f"makers exhibit the same timing"
                    ),
                    heuristic=RAPID_HOP_HEURISTIC,
                    confidence=_confidence(0.5, strength),
                    refs=_refs(*sample),
                ),
            ),
        )
    ]


ALL_DETECTORS = (
    detect_peel_chain,
    detect_distribution,
    detect_fan_in,
    detect_rapid_hop,
)

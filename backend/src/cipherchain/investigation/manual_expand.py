"""One-hop, unpersisted counterparty lookup for manual exploration.

Distinct from the autonomous engine's frontier (frontier.py): this reads a
single page of an address's raw history straight off the Chain SDK,
normalizes it, and folds it into counterparties in memory. Nothing is
written to Postgres, no investigation record is created, no heuristic runs,
and no Finding/Evidence is produced — it exists for the click-to-expand
manual explorer, where a human decides what to look at next, not the engine.

Ranking follows the same v1 approximation the autonomous frontier uses
(frontier.py, ENGINE_DESIGN.md §3): a raw smallest-unit sum across
movements orders which counterparty is shown first, but the amount actually
returned for a counterparty is always one real movement (the largest),
never a cross-asset sum.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from cipherchain.chains.base import ChainAdapter
from cipherchain.core.models import Address, Movement, MovementKind


@dataclass(frozen=True, slots=True)
class ServiceDegree:
    """Distinct counterparty degree seen on ONE page of history.

    Fed to the same thresholds the autonomous engine's service-endpoint
    heuristic uses (analysis/heuristics/service.py), so the manual explorer
    can mark custodial-looking addresses instead of leaving a human to spot
    them by eye.

    ``page_bounded`` is the honesty field and is always true here: these are
    the counterparties on one page, not the address's lifetime degree. The
    error can therefore only run one way — a busy address may fail the
    thresholds on a short page and go unmarked, but nothing can be marked a
    service on counterparties it does not have. Under-detection is the safe
    direction, and it must still be SAID rather than implied.
    """

    senders: int
    recipients: int
    page_bounded: bool = True


@dataclass(frozen=True, slots=True)
class OneHopCounterparty:
    address: str
    direction: str  # "in" | "out", relative to the queried address
    amount: int
    asset_symbol: str
    asset_decimals: int
    tx_hash: str
    timestamp: datetime


@dataclass(frozen=True, slots=True)
class OneHopTransfer:
    """One movement, uncollapsed.

    The counterparty view above answers "who did this address deal with";
    this answers "what actually happened, transaction by transaction". They
    are deliberately separate readings of the same page rather than one
    endpoint returning both: an investigator reading a transfer list wants
    every row, including several with the same counterparty, which is
    precisely what :func:`one_hop_counterparties` exists to fold away.
    """

    counterparty: str
    direction: str  # "in" | "out", relative to the queried address
    amount: int
    asset_symbol: str
    asset_decimals: int
    tx_hash: str
    timestamp: datetime


async def one_hop_transfers(
    adapter: ChainAdapter,
    chain: str,
    address: str,
    *,
    limit: int = 50,
    page_size: int = 50,
    cursor: str | None = None,
) -> tuple[list[OneHopTransfer], bool, int, str | None]:
    """Individual movements touching ``address``, newest first.

    Same page, same normalization and the same UTXO joining rule as
    :func:`one_hop_counterparties` — only the folding differs. Returns
    ``(transfers, truncated, total_count, next_cursor)``; ``truncated``
    carries the identical meaning (there is more than this shows, said
    rather than hidden) and ``next_cursor`` is how to ask for it.
    """
    canonical = adapter.canonical_address(address)
    page = await adapter.address_history(
        Address(chain, canonical), limit=page_size, cursor=cursor
    )

    out: list[OneHopTransfer] = []

    def record(addr: str, direction: str, movement: Movement) -> None:
        out.append(
            OneHopTransfer(
                counterparty=addr,
                direction=direction,
                amount=movement.amount,
                asset_symbol=movement.asset.symbol,
                asset_decimals=movement.asset.decimals,
                tx_hash=movement.tx.tx_hash,
                timestamp=movement.tx.timestamp,
            )
        )

    for tx in page.items:
        normalized = await adapter.normalize(tx)
        moves = normalized.movements
        utxo_inputs = [m for m in moves if m.kind == MovementKind.UTXO_INPUT]
        utxo_outputs = [m for m in moves if m.kind == MovementKind.UTXO_OUTPUT]
        focus_is_recipient = any(
            m.to_address and m.to_address.value == canonical for m in utxo_outputs
        )
        focus_is_payer = any(
            m.from_address and m.from_address.value == canonical for m in utxo_inputs
        )

        for m in moves:
            if m.kind in (MovementKind.UTXO_INPUT, MovementKind.UTXO_OUTPUT):
                continue  # joined transaction-wide, below
            if (
                m.to_address
                and m.to_address.value == canonical
                and m.from_address
                and m.from_address.value != canonical
            ):
                record(m.from_address.value, "in", m)
            elif (
                m.from_address
                and m.from_address.value == canonical
                and m.to_address
                and m.to_address.value != canonical
            ):
                record(m.to_address.value, "out", m)

        if focus_is_recipient:
            for half in utxo_inputs:
                if half.from_address and half.from_address.value != canonical:
                    record(half.from_address.value, "in", half)
        if focus_is_payer:
            for half in utxo_outputs:
                if half.to_address and half.to_address.value != canonical:
                    record(half.to_address.value, "out", half)

    out.sort(key=lambda t: t.timestamp, reverse=True)
    truncated = (
        len(out) > limit or page.next_cursor is not None or bool(page.gaps) or page.truncated
    )
    return out[:limit], truncated, len(out), page.next_cursor


async def one_hop_counterparties(
    adapter: ChainAdapter,
    chain: str,
    address: str,
    *,
    limit: int = 25,
    page_size: int = 50,
    cursor: str | None = None,
) -> tuple[list[OneHopCounterparty], bool, int, str | None, ServiceDegree]:
    """Counterparties of ``address`` from one page of its raw history.

    Returns ``(counterparties, truncated, total_count, next_cursor)``, ranked
    by an approximate raw-value total (largest first) and capped at ``limit``.
    ``truncated`` is true whenever there is more to see than this page
    shows — either more counterparties than ``limit`` keeps, or the history
    page itself said it was incomplete — stated rather than hidden, the same
    rule the rest of this project holds everywhere else. ``next_cursor`` is
    how a caller asks for the next page of history; note the ranking is
    per-page, so paging discovers MORE counterparties rather than continuing
    one global ordering.
    """
    canonical = adapter.canonical_address(address)
    page = await adapter.address_history(
        Address(chain, canonical), limit=page_size, cursor=cursor
    )

    totals: dict[tuple[str, str], int] = {}
    representative: dict[tuple[str, str], Movement] = {}

    def accumulate(addr: str, direction: str, movement: Movement) -> None:
        key = (addr, direction)
        totals[key] = totals.get(key, 0) + movement.amount
        best = representative.get(key)
        if best is None or movement.amount > best.amount:
            representative[key] = movement

    for tx in page.items:
        normalized = await adapter.normalize(tx)
        moves = normalized.movements
        utxo_inputs = [m for m in moves if m.kind == MovementKind.UTXO_INPUT]
        utxo_outputs = [m for m in moves if m.kind == MovementKind.UTXO_OUTPUT]
        focus_is_recipient = any(
            m.to_address and m.to_address.value == canonical for m in utxo_outputs
        )
        focus_is_payer = any(
            m.from_address and m.from_address.value == canonical for m in utxo_inputs
        )

        for m in moves:
            if m.kind in (MovementKind.UTXO_INPUT, MovementKind.UTXO_OUTPUT):
                continue  # UTXO halves are joined transaction-wide, below
            if (
                m.to_address
                and m.to_address.value == canonical
                and m.from_address
                and m.from_address.value != canonical
            ):
                accumulate(m.from_address.value, "in", m)
            elif (
                m.from_address
                and m.from_address.value == canonical
                and m.to_address
                and m.to_address.value != canonical
            ):
                accumulate(m.to_address.value, "out", m)

        # UTXO output half naming the focus as recipient: the paying
        # counterparties are this same transaction's input halves.
        if focus_is_recipient:
            for half in utxo_inputs:
                if half.from_address and half.from_address.value != canonical:
                    accumulate(half.from_address.value, "in", half)
        # UTXO input half naming the focus as payer: the receiving
        # counterparties are this same transaction's output halves (change
        # included — change detection is analysis, not this lookup).
        if focus_is_payer:
            for half in utxo_outputs:
                if half.to_address and half.to_address.value != canonical:
                    accumulate(half.to_address.value, "out", half)

    ranked = sorted(totals.keys(), key=lambda k: -totals[k])
    out = []
    for addr, direction in ranked:
        m = representative[(addr, direction)]
        out.append(
            OneHopCounterparty(
                address=addr,
                direction=direction,
                amount=m.amount,
                asset_symbol=m.asset.symbol,
                asset_decimals=m.asset.decimals,
                tx_hash=m.tx.tx_hash,
                timestamp=m.tx.timestamp,
            )
        )
    truncated = (
        len(out) > limit or page.next_cursor is not None or bool(page.gaps) or page.truncated
    )
    # Degree over EVERY counterparty on the page, not the `limit` slice: the
    # cap is a legibility bound on what gets drawn, and letting it shrink the
    # evidence would make the mark depend on the caller's page size.
    degree = ServiceDegree(
        senders=len({addr for addr, direction in totals if direction == "in"}),
        recipients=len({addr for addr, direction in totals if direction == "out"}),
    )
    return out[:limit], truncated, len(out), page.next_cursor, degree

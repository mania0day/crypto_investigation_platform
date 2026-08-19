"""Counterparty derivation from stored movements — paradigm-uniform.

The data shape itself encodes the paradigm (core model design note):
account movements carry both endpoints; UTXO halves carry one, and the
joining transaction's opposite halves supply the counterparties. No chain
identity is ever consulted (vision principle 5).

Value attribution is a stated v1 ranking approximation (ENGINE_DESIGN.md
§3): raw smallest-unit sums, per direction. It orders the frontier; it
never limits coverage.

Ranking counts only movements in assets whose provenance is established.
A token contract can emit transfers naming any amount it likes, so ranking
on unverified assets hands an attacker the steering wheel: spray a victim
with a worthless token carrying an astronomical nominal amount and the real
trail sinks below the spam until the budget runs out. Unverified
counterparties are still returned and still explored — they simply do not
get to decide what is explored FIRST.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from cipherchain.core.models import Direction, MovementKind
from cipherchain.storage.repositories import FactRepository, StoredMovement


@dataclass(frozen=True, slots=True)
class Counterparty:
    address_id: int
    value: int
    movement_id: int  # representative (largest) movement — becomes the edge


async def derive_counterparties(
    facts: FactRepository,
    movements: Sequence[StoredMovement],
    focus_address_id: int,
    direction: Direction,
    *,
    ranking_assets: frozenset[int] | None = None,
) -> list[Counterparty]:
    """Counterparties of ``focus_address_id``, ranked by value.

    ``ranking_assets`` limits which assets CONTRIBUTE to the ranking value.
    Every counterparty is still returned regardless — an address reached only
    through an unverified token ranks at zero, not out of the frontier. Passing
    ``None`` ranks on everything, which is the unguarded behaviour.
    """
    totals: dict[int, int] = {}
    representative: dict[int, tuple[int, int]] = {}  # address_id -> (amount, movement_id)
    seen_transactions: set[int] = set()

    def add(address_id: int, amount: int, movement_id: int, asset_id: int) -> None:
        if address_id == focus_address_id:
            return
        ranks = ranking_assets is None or asset_id in ranking_assets
        totals[address_id] = totals.get(address_id, 0) + (amount if ranks else 0)
        # The representative movement becomes the graph EDGE, so it stays the
        # largest REAL movement even when that movement cannot rank.
        best = representative.get(address_id)
        if best is None or amount > best[0]:
            representative[address_id] = (amount, movement_id)

    for movement in movements:
        if direction is Direction.BACKWARD:
            if movement.to_address_id != focus_address_id:
                continue
            if movement.from_address_id is not None:
                add(movement.from_address_id, movement.amount, movement.id, movement.asset_id)
            elif movement.transaction_id not in seen_transactions:
                # UTXO output half into focus: counterparties are the same
                # transaction's input halves.
                seen_transactions.add(movement.transaction_id)
                for half in await facts.movements_for_transaction(movement.transaction_id):
                    if (
                        half.kind == str(MovementKind.UTXO_INPUT)
                        and half.from_address_id is not None
                    ):
                        add(half.from_address_id, half.amount, half.id, half.asset_id)
        else:
            if movement.from_address_id != focus_address_id:
                continue
            if movement.to_address_id is not None:
                add(movement.to_address_id, movement.amount, movement.id, movement.asset_id)
            elif movement.transaction_id not in seen_transactions:
                # UTXO input half from focus: counterparties are the same
                # transaction's output halves (change included — change
                # detection is analysis, not engine).
                seen_transactions.add(movement.transaction_id)
                for half in await facts.movements_for_transaction(movement.transaction_id):
                    if (
                        half.kind == str(MovementKind.UTXO_OUTPUT)
                        and half.to_address_id is not None
                    ):
                        add(half.to_address_id, half.amount, half.id, half.asset_id)

    ranked = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
    return [
        Counterparty(address_id=aid, value=value, movement_id=representative[aid][1])
        for aid, value in ranked
    ]

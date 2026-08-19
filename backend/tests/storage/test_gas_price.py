"""Gas price on movements — the column the unique-gas-price heuristic reads.

The heuristic links a mixer deposit to a withdrawal by EXACT equality of a
manually set gas price (REACHING_THE_VASP.md §3, heuristic 3), which puts two
demands on storage: the value survives unrounded however large it is, and a
value once recorded is never quietly rewritten by a later re-parse.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cipherchain.core.models import (
    Address,
    Asset,
    AssetKind,
    Movement,
    MovementKind,
    Provenance,
    TxRef,
)
from cipherchain.storage.repositories import FactRepository

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
PROV = Provenance(provider="test", retrieved_at=NOW, payload_sha256="c" * 64)
ETH = Asset(chain="ethereum", kind=AssetKind.NATIVE, symbol="ETH", decimals=18)
# Past the BIGINT ceiling: a uint256 gas price is legal on chain, and a BIGINT
# column would have rejected the insert outright.
ABOVE_BIGINT = 2**70


async def store_one(repo: FactRepository, tx_hash: str) -> tuple[int, int]:
    """Store one movement; return (transaction_id, movement_id)."""
    movement = Movement(
        tx=TxRef(chain="ethereum", tx_hash=tx_hash, timestamp=NOW, block_number=100),
        asset=ETH,
        amount=1,
        kind=MovementKind.NATIVE,
        from_address=Address("ethereum", "0xaaa"),
        to_address=Address("ethereum", "0xbbb"),
        index=0,
        provenance=PROV,
    )
    tx_id, _ = await repo.store_movements(movement.tx, [movement])
    (stored,) = await repo.movements_for_transaction(tx_id)
    return tx_id, stored.id


class TestGasPriceBackfill:
    async def test_a_stored_movement_carries_no_gas_price_until_backfilled(
        self, session: AsyncSession
    ) -> None:
        """None means "not read", never "paid nothing" — the heuristic must skip
        nulls rather than match two of them, which would link a deposit to an
        arbitrary withdrawal and call it evidence."""
        repo = FactRepository(session)
        tx_id, _ = await store_one(repo, "0x01")
        assert [m.gas_price for m in await repo.movements_for_transaction(tx_id)] == [None]

    async def test_a_gas_price_above_the_bigint_ceiling_survives_the_round_trip(
        self, session: AsyncSession
    ) -> None:
        repo = FactRepository(session)
        tx_id, movement_id = await store_one(repo, "0x02")
        assert await repo.record_gas_prices({movement_id: ABOVE_BIGINT}) == 1
        (stored,) = await repo.movements_for_transaction(tx_id)
        assert stored.gas_price == ABOVE_BIGINT
        assert isinstance(stored.gas_price, int)

    async def test_re_running_the_backfill_changes_nothing(self, session: AsyncSession) -> None:
        """Write-once, like every other write in the fact plane. If two
        re-parses of one transaction ever disagreed, the second overwriting the
        first would be the worst outcome available: the heuristic reading this
        column concludes from exact equality."""
        repo = FactRepository(session)
        tx_id, movement_id = await store_one(repo, "0x03")
        assert await repo.record_gas_prices({movement_id: 20_000_000_000}) == 1
        assert await repo.record_gas_prices({movement_id: 99}) == 0
        (stored,) = await repo.movements_for_transaction(tx_id)
        assert stored.gas_price == 20_000_000_000

    async def test_backfilling_a_movement_that_is_not_stored_reports_nothing_done(
        self, session: AsyncSession
    ) -> None:
        """The count is what the backfill reports as coverage, so it counts rows
        actually changed rather than entries offered."""
        repo = FactRepository(session)
        assert await repo.record_gas_prices({}) == 0
        assert await repo.record_gas_prices({999_999: 1}) == 0

    async def test_a_negative_gas_price_is_refused(self, session: AsyncSession) -> None:
        """`movements.amount` has carried a >= 0 CHECK since the first
        migration; this column was added to the same table without one and took
        -5 without complaint. No chain has a negative gas price, so the value is
        a parse bug in the backfill — and since the heuristic reading this
        column concludes from EXACT equality, a sign error repeated across two
        re-parses reads as a match and links a deposit to an unrelated
        withdrawal."""
        repo = FactRepository(session)
        _, movement_id = await store_one(repo, "0x04")
        with pytest.raises(IntegrityError):
            await repo.record_gas_prices({movement_id: -5})


class TestAdapterSuppliedPricesReachTheDatabase:
    """The storage boundary is where this feature lived or died.

    The adapter resolves a price per transaction and stamps it on every
    movement, but ``store_movements`` builds its row dict field by field: a
    movement attribute with no key in that dict is dropped silently. Nothing
    raises, the column stays null exactly as it was before, and the mixer
    ladder's exact-equality rung simply never matches — an investigation that
    stops one link short with no error anywhere.
    """

    async def test_a_price_supplied_by_the_adapter_is_persisted(
        self, session: AsyncSession
    ) -> None:
        repo = FactRepository(session)
        movement = Movement(
            tx=TxRef(chain="ethereum", tx_hash="0xpriced", timestamp=NOW, block_number=100),
            asset=ETH,
            amount=1,
            kind=MovementKind.NATIVE,
            from_address=Address("ethereum", "0xaaa"),
            to_address=Address("ethereum", "0xbbb"),
            index=0,
            provenance=PROV,
            gas_price=31_500_000_000,
        )
        tx_id, _ = await repo.store_movements(movement.tx, [movement])

        assert [m.gas_price for m in await repo.movements_for_transaction(tx_id)] == [
            31_500_000_000
        ]

    async def test_a_literal_zero_is_stored_as_zero_and_not_as_unknown(
        self, session: AsyncSession
    ) -> None:
        """0 and None must stay distinguishable all the way down. A subsidised
        or zero-fee transaction really did pay nothing; "not read" is a
        different fact, and the rung that matches on price declines the first
        and must never treat two of the second as equal."""
        repo = FactRepository(session)
        movement = Movement(
            tx=TxRef(chain="ethereum", tx_hash="0xfree", timestamp=NOW, block_number=101),
            asset=ETH,
            amount=1,
            kind=MovementKind.NATIVE,
            from_address=Address("ethereum", "0xaaa"),
            to_address=Address("ethereum", "0xbbb"),
            index=0,
            provenance=PROV,
            gas_price=0,
        )
        tx_id, _ = await repo.store_movements(movement.tx, [movement])

        assert [m.gas_price for m in await repo.movements_for_transaction(tx_id)] == [0]

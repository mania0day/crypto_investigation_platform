"""The critical review finding (#1): movement identity must be vantage-stable
so re-normalizing the same tx from a different acquisition neither DROPS nor
DOUBLE-STORES transfers. These tests reproduce the exact failure scenarios."""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from cipherchain.core.models import (
    Address,
    Asset,
    AssetKind,
    Direction,
    Movement,
    MovementKind,
    Provenance,
    TxRef,
)
from cipherchain.storage.repositories import FactRepository

NOW = datetime(2026, 8, 7, tzinfo=UTC)
PROV = Provenance(provider="test", retrieved_at=NOW, payload_sha256="a" * 64)
USDC = Asset(chain="ethereum", kind=AssetKind.TOKEN, symbol="USDC", decimals=6, contract="0xusdc")
TX = TxRef(chain="ethereum", tx_hash="0xair", timestamp=NOW, block_number=1)


def token(sender: str, target: str, amount: int) -> Movement:
    # dedup_key matches what the EVM adapter produces: content-addressed, so the
    # same logical transfer keys identically regardless of vantage.
    return Movement(
        tx=TX,
        asset=USDC,
        amount=amount,
        kind=MovementKind.TOKEN,
        from_address=Address("ethereum", sender),
        to_address=Address("ethereum", target),
        index=0,
        provenance=PROV,
        dedup_key=f"token:{sender}:{target}:0xusdc",
    )


async def test_airdrop_two_recipients_no_drop(session: AsyncSession) -> None:
    """A->B and A->C in one tx. Expanding C then B (each a separate vantage
    that sees only its own transfer at index 0) must store BOTH, not drop one."""
    facts = FactRepository(session)
    # vantage 1: expanding C only sees A->C
    await facts.store_movements(TX, [token("0xa", "0xc", 500)])
    # vantage 2: expanding B only sees A->B (old code: index 0 collides -> dropped)
    await facts.store_movements(TX, [token("0xa", "0xb", 700)])

    b_id = await facts.get_or_create_address(Address("ethereum", "0xb"))
    c_id = await facts.get_or_create_address(Address("ethereum", "0xc"))
    b_in = await facts.movements_to_address(b_id)
    c_in = await facts.movements_to_address(c_id)
    assert [m.amount for m in b_in] == [700]  # B's inflow survives
    assert [m.amount for m in c_in] == [500]  # C's inflow survives


async def test_same_transfer_two_vantages_no_duplicate(session: AsyncSession) -> None:
    """The same logical transfer seen twice (e.g. via history then via a tx
    lookup) must dedup to ONE row, not double B's value share."""
    facts = FactRepository(session)
    _, first = await facts.store_movements(TX, [token("0xrouter", "0xb", 900)])
    _, second = await facts.store_movements(TX, [token("0xrouter", "0xb", 900)])
    assert first == 1
    assert second == 0  # idempotent — no duplicate

    b_id = await facts.get_or_create_address(Address("ethereum", "0xb"))
    incoming = await facts.movements_to_address(b_id)
    assert [m.amount for m in incoming] == [900]  # counted once

    counterparties = await _counterparties(facts, b_id, Direction.BACKWARD)
    assert counterparties == [("0xrouter", 900)]  # value share not doubled


async def _counterparties(facts, address_id, direction):
    from cipherchain.investigation.frontier import derive_counterparties

    incoming = await facts.movements_to_address(address_id)
    result = await derive_counterparties(facts, incoming, address_id, direction)
    out = []
    for cp in result:
        addr = await facts.get_address(cp.address_id)
        out.append((addr.value if addr else None, cp.value))
    return out

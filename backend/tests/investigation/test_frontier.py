"""Counterparty derivation is paradigm-uniform: account movements resolve
directly; UTXO halves resolve through the joining tx's opposite halves."""

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
from cipherchain.investigation.frontier import derive_counterparties
from cipherchain.storage.repositories import FactRepository

NOW = datetime(2026, 8, 7, tzinfo=UTC)
PROV = Provenance(provider="test", retrieved_at=NOW, payload_sha256="f" * 64)
BTC = Asset(chain="bitcoin", kind=AssetKind.NATIVE, symbol="BTC", decimals=8)
ETH = Asset(chain="ethereum", kind=AssetKind.NATIVE, symbol="ETH", decimals=18)


async def test_account_movements_resolve_directly(session: AsyncSession) -> None:
    facts = FactRepository(session)
    tx = TxRef(chain="ethereum", tx_hash="0xacc", timestamp=NOW)
    movement = Movement(
        tx=tx,
        asset=ETH,
        amount=500,
        kind=MovementKind.NATIVE,
        from_address=Address("ethereum", "0xsender"),
        to_address=Address("ethereum", "0xfocus"),
        index=0,
        provenance=PROV,
    )
    await facts.store_movements(tx, [movement])
    focus = await facts.get_or_create_address(Address("ethereum", "0xfocus"))
    sender = await facts.get_or_create_address(Address("ethereum", "0xsender"))

    incoming = await facts.movements_to_address(focus)
    backward = await derive_counterparties(facts, incoming, focus, Direction.BACKWARD)
    assert [(c.address_id, c.value) for c in backward] == [(sender, 500)]

    outgoing = await facts.movements_from_address(focus)
    assert await derive_counterparties(facts, outgoing, focus, Direction.FORWARD) == []


async def test_utxo_halves_resolve_through_the_transaction(session: AsyncSession) -> None:
    facts = FactRepository(session)
    tx = TxRef(chain="bitcoin", tx_hash="f00d", timestamp=NOW)
    halves = [
        Movement(
            tx=tx,
            asset=BTC,
            amount=6_000,
            kind=MovementKind.UTXO_INPUT,
            from_address=Address("bitcoin", "bc1_funder_a"),
            to_address=None,
            index=0,
            provenance=PROV,
        ),
        Movement(
            tx=tx,
            asset=BTC,
            amount=4_000,
            kind=MovementKind.UTXO_INPUT,
            from_address=Address("bitcoin", "bc1_funder_b"),
            to_address=None,
            index=1,
            provenance=PROV,
        ),
        Movement(
            tx=tx,
            asset=BTC,
            amount=9_000,
            kind=MovementKind.UTXO_OUTPUT,
            from_address=None,
            to_address=Address("bitcoin", "bc1_focus"),
            index=2,
            provenance=PROV,
        ),
    ]
    await facts.store_movements(tx, halves)
    focus = await facts.get_or_create_address(Address("bitcoin", "bc1_focus"))
    funder_a = await facts.get_or_create_address(Address("bitcoin", "bc1_funder_a"))
    funder_b = await facts.get_or_create_address(Address("bitcoin", "bc1_funder_b"))

    incoming = await facts.movements_to_address(focus)
    backward = await derive_counterparties(facts, incoming, focus, Direction.BACKWARD)
    # ranked by value desc; counterparties come from the tx's input halves
    assert [(c.address_id, c.value) for c in backward] == [(funder_a, 6_000), (funder_b, 4_000)]

    # forward from a funder: counterparties are the tx's output halves
    outgoing = await facts.movements_from_address(funder_a)
    forward = await derive_counterparties(facts, outgoing, funder_a, Direction.FORWARD)
    assert [(c.address_id, c.value) for c in forward] == [(focus, 9_000)]

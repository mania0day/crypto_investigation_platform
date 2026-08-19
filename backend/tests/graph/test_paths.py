"""Evidence refs may not be routed through a link the engine guessed.

``path_tx_hashes`` produces the refs on ``ONCHAIN_FACT`` evidence, whose whole
promise is that a reader can verify the value path themselves. At a mixer the
deposit-to-withdrawal link is severed by design and the engine SELECTS the
onward branch, so a hash list spanning that crossing is a false claim in the one
evidence kind that is meant to be checkable: the reader follows the hashes, hits
the pool, and finds the trail does not join up.
"""

from __future__ import annotations

import uuid
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
from cipherchain.graph.paths import path_tx_hashes
from cipherchain.storage.repositories import FactRepository, InvestigationRepository

NOW = datetime(2026, 8, 16, tzinfo=UTC)
ETH = Asset(chain="ethereum", kind=AssetKind.NATIVE, symbol="ETH", decimals=18)
PROV = Provenance(provider="test", retrieved_at=NOW, payload_sha256="0" * 64)
MIXER_EXIT = "mixer-exit-anonymity-set@1"


async def a_movement(facts: FactRepository, tx_hash: str) -> int:
    movement = Movement(
        tx=TxRef(chain="ethereum", tx_hash=tx_hash, timestamp=NOW, block_number=1),
        asset=ETH,
        amount=1,
        kind=MovementKind.NATIVE,
        from_address=Address("ethereum", f"{tx_hash}_from"),
        to_address=Address("ethereum", f"{tx_hash}_to"),
        index=0,
        provenance=PROV,
    )
    tx_id, _ = await facts.store_movements(movement.tx, [movement])
    (stored,) = await facts.movements_for_transaction(tx_id)
    return stored.id


async def a_node(
    session: AsyncSession,
    repo: InvestigationRepository,
    investigation_id: uuid.UUID,
    address: str,
    *,
    hop: int,
    speculative_basis: str | None = None,
) -> int:
    address_id = await FactRepository(session).get_or_create_address(Address("ethereum", address))
    node_id = await repo.add_address_node(
        investigation_id,
        address_id,
        direction=Direction.BACKWARD,
        hop_distance=hop,
        value_share=1,
        discovered_reason="find_prev_vasp",
        speculative_basis=speculative_basis,
    )
    assert node_id is not None
    return node_id


async def a_chain_across_a_mixer(
    session: AsyncSession,
) -> tuple[uuid.UUID, dict[str, int]]:
    """root → pool → (guessed) exit → beyond, each hop carrying a real movement.

    The crossing edge is real in the database — the engine records the
    withdrawal it chose — which is exactly why the guard cannot be left to the
    edge table to imply.
    """
    facts = FactRepository(session)
    root_address_id = await facts.get_or_create_address(Address("ethereum", "0xroot"))
    repo = InvestigationRepository(session)
    row = await repo.create(
        root_address_id=root_address_id,
        objectives=["find_prev_vasp"],
        budgets={"api_calls": 100, "seconds": 300, "max_depth": 6, "max_nodes": 500},
        engine_version="0.1.0",
        ruleset_version="2026-08-16",
    )
    nodes = {
        "root": await a_node(session, repo, row.id, "0xroot", hop=0),
        "pool": await a_node(session, repo, row.id, "0xpool", hop=1),
        "exit": await a_node(session, repo, row.id, "0xexit", hop=2, speculative_basis=MIXER_EXIT),
        "beyond": await a_node(
            session, repo, row.id, "0xbeyond", hop=3, speculative_basis=MIXER_EXIT
        ),
    }
    for src, dst, tx_hash in (
        ("root", "pool", "0xdeposit"),
        ("pool", "exit", "0xwithdrawal"),
        ("exit", "beyond", "0xonward"),
    ):
        await repo.add_edge(
            row.id,
            src_node_id=nodes[src],
            dst_node_id=nodes[dst],
            movement_id=await a_movement(facts, tx_hash),
        )
    return row.id, nodes


class TestAGuessedLinkCannotCarryEvidence:
    async def test_a_traced_path_is_returned_in_order(self, session: AsyncSession) -> None:
        """The ordinary case still works — the guard must not close the door on
        every path, only on the ones crossing a guess."""
        investigation_id, nodes = await a_chain_across_a_mixer(session)

        hashes = await path_tx_hashes(session, investigation_id, nodes["root"], nodes["pool"])

        assert hashes == ("0xdeposit",)

    async def test_no_path_is_routed_THROUGH_a_speculative_node(
        self, session: AsyncSession
    ) -> None:
        """The failure this guard exists for.

        ``0xbeyond`` is reachable in the edge table — every hop has a real
        movement id. But the middle hop is a withdrawal the engine PICKED out of
        an anonymity set, so returning these three hashes would assert a
        connected value path from the subject to an address that may belong to a
        stranger.
        """
        investigation_id, nodes = await a_chain_across_a_mixer(session)

        hashes = await path_tx_hashes(session, investigation_id, nodes["root"], nodes["beyond"])

        assert hashes == (), "refs were routed through a mixer crossing"

    async def test_a_speculative_node_may_still_be_the_destination(
        self, session: AsyncSession
    ) -> None:
        """A guess terminates a path; it does not carry one onward.

        The engine words findings on these nodes to say the hashes do not form a
        connected path, so the caveat travels with them. Refusing the path
        outright would strip a real, citable withdrawal hash out of a finding
        that is already correctly labelled.
        """
        investigation_id, nodes = await a_chain_across_a_mixer(session)

        hashes = await path_tx_hashes(session, investigation_id, nodes["root"], nodes["exit"])

        assert hashes == ("0xdeposit", "0xwithdrawal")

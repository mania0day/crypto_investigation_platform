"""Fact store + investigation overlay behavior against real Postgres."""

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from cipherchain.core.models import (
    Address,
    Asset,
    AssetKind,
    Direction,
    Evidence,
    EvidenceKind,
    Finding,
    FindingKind,
    Movement,
    MovementKind,
    Provenance,
    TxRef,
)
from cipherchain.storage.repositories import FactRepository, InvestigationRepository

NOW = datetime(2026, 8, 7, 12, 0, 0, tzinfo=UTC)
PROV = Provenance(provider="test", retrieved_at=NOW, payload_sha256="c" * 64)
ETH = Asset(chain="ethereum", kind=AssetKind.NATIVE, symbol="ETH", decimals=18)
UINT256_MAX = 2**256 - 1


def eth_movement(tx_hash: str, src: str, dst: str, amount: int, when: datetime) -> Movement:
    return Movement(
        tx=TxRef(chain="ethereum", tx_hash=tx_hash, timestamp=when, block_number=100),
        asset=ETH,
        amount=amount,
        kind=MovementKind.NATIVE,
        from_address=Address("ethereum", src),
        to_address=Address("ethereum", dst),
        index=0,
        provenance=PROV,
    )


class TestFactRepository:
    async def test_get_or_create_address_idempotent(self, session: AsyncSession) -> None:
        repo = FactRepository(session)
        a = Address("ethereum", "0xabc")
        first = await repo.get_or_create_address(a)
        second = await repo.get_or_create_address(a)
        assert first == second
        assert await repo.get_address(first) == a

    async def test_native_asset_nulls_not_distinct(self, session: AsyncSession) -> None:
        repo = FactRepository(session)
        first = await repo.get_or_create_asset(ETH)
        second = await repo.get_or_create_asset(ETH)
        assert first == second

    async def test_store_movements_idempotent_and_uint256_safe(self, session: AsyncSession) -> None:
        repo = FactRepository(session)
        movement = eth_movement("0xdead", "0xaaa", "0xbbb", UINT256_MAX, NOW)
        tx_id, inserted = await repo.store_movements(movement.tx, [movement], raw_sha256="d" * 64)
        assert inserted == 1
        tx_id2, inserted2 = await repo.store_movements(movement.tx, [movement])
        assert tx_id2 == tx_id
        assert inserted2 == 0  # idempotent re-normalization
        stored = await repo.movements_for_transaction(tx_id)
        assert len(stored) == 1
        assert stored[0].amount == UINT256_MAX
        assert stored[0].timestamp == NOW

    async def test_traversal_queries_direction_and_time(self, session: AsyncSession) -> None:
        repo = FactRepository(session)
        earlier = NOW - timedelta(days=2)
        m_old = eth_movement("0x01", "0xfunder", "0xroot", 100, earlier)
        m_new = eth_movement("0x02", "0xroot", "0xcashout", 90, NOW)
        await repo.store_movements(m_old.tx, [m_old])
        await repo.store_movements(m_new.tx, [m_new])
        root_id = await repo.get_or_create_address(Address("ethereum", "0xroot"))

        incoming = await repo.movements_to_address(root_id)
        assert [m.tx_hash for m in incoming] == ["0x01"]
        outgoing = await repo.movements_from_address(root_id)
        assert [m.tx_hash for m in outgoing] == ["0x02"]
        # time-respecting: nothing incoming after cutoff before the funding tx
        assert await repo.movements_to_address(root_id, before=earlier - timedelta(days=1)) == []

    async def test_ordering_does_not_depend_on_insertion_order(self, session: AsyncSession) -> None:
        """Ties must break on CHAIN data, not on the database's row ids.

        `MovementRow.id` is an Identity assigned at insert time, so ordering by
        it makes results depend on the sequence an investigation happened to
        ingest transactions in. Sweep matching consumes candidates as it pairs,
        so one reordered tie changes every later pairing — the same movements
        would yield different findings on a re-run.
        """
        repo = FactRepository(session)
        # Same timestamp, inserted in the OPPOSITE order to their tx hashes.
        late = eth_movement("0xbbb", "0xpayer", "0xsubject", 10, NOW)
        early = eth_movement("0xaaa", "0xpayer", "0xsubject", 10, NOW)
        await repo.store_movements(late.tx, [late])
        await repo.store_movements(early.tx, [early])
        subject = await repo.get_or_create_address(Address("ethereum", "0xsubject"))

        incoming = await repo.movements_to_address(subject)
        assert [m.tx_hash for m in incoming] == ["0xaaa", "0xbbb"], (
            "tie-break must follow tx_hash, not the order rows happened to be inserted"
        )

    async def test_analysis_reads_both_directions_over_one_window(
        self, session: AsyncSession
    ) -> None:
        """Traversal reads the two directions in opposite orders on purpose.

        For ANALYSIS that is wrong: on a busy address it hands a detector the
        newest receipts and the OLDEST sends — two windows that need not
        overlap, so a sweep detector finds pairs that never happened and misses
        the ones that did.
        """
        repo = FactRepository(session)
        old, new = NOW - timedelta(days=30), NOW
        for index, (tx, stamp) in enumerate((f"0xin{i}", new) for i in range(3)):
            movement = eth_movement(tx, f"0xsender{index}", "0xsubject", 100, stamp)
            await repo.store_movements(movement.tx, [movement])
        for index, stamp in enumerate((old, old, new)):
            movement = eth_movement(f"0xout{index}", "0xsubject", f"0xdest{index}", 90, stamp)
            await repo.store_movements(movement.tx, [movement])
        subject = await repo.get_or_create_address(Address("ethereum", "0xsubject"))

        # Traversal keeps its earliest-first outgoing order (REVIEW_FINDINGS #7).
        traversal = await repo.movements_from_address(subject, limit=2)
        assert [m.timestamp for m in traversal] == [old, old]

        # Analysis gets a comparable window: both sides newest-first.
        incoming, outgoing = await repo.movements_around_address(subject, limit=2)
        assert all(m.timestamp == new for m in incoming)
        assert outgoing[0].timestamp == new, (
            "analysis must see the RECENT sends alongside the recent receipts"
        )


class TestInvestigationRepository:
    async def _new_investigation(
        self, session: AsyncSession
    ) -> tuple[int, InvestigationRepository]:
        facts = FactRepository(session)
        root_id = await facts.get_or_create_address(Address("ethereum", "0xroot"))
        repo = InvestigationRepository(session)
        row = await repo.create(
            root_address_id=root_id,
            objectives=["find_prev_vasp", "find_next_vasp"],
            budgets={"api_calls": 100, "seconds": 300, "max_depth": 4, "max_nodes": 500},
            engine_version="0.1.0",
            ruleset_version="2026-08-07",
        )
        self.investigation_id = row.id
        return root_id, repo

    async def test_lifecycle_and_status(self, session: AsyncSession) -> None:
        _, repo = await self._new_investigation(session)
        await repo.set_status(self.investigation_id, "running")
        row = await repo.get(self.investigation_id)
        assert row is not None and row.status == "running"
        await repo.update_spent(self.investigation_id, {"api_calls": 7})
        await session.refresh(row)
        assert row.spent == {"api_calls": 7}

    async def test_frontier_dedup_and_priority_order(self, session: AsyncSession) -> None:
        _, repo = await self._new_investigation(session)
        facts = FactRepository(session)
        low = await facts.get_or_create_address(Address("ethereum", "0xlow"))
        high = await facts.get_or_create_address(Address("ethereum", "0xhigh"))

        first = await repo.add_address_node(
            self.investigation_id,
            low,
            direction=Direction.BACKWARD,
            hop_distance=2,
            value_share=10,
            discovered_reason="find_prev_vasp",
        )
        duplicate = await repo.add_address_node(
            self.investigation_id,
            low,
            direction=Direction.BACKWARD,
            hop_distance=2,
            value_share=10,
            discovered_reason="find_prev_vasp",
        )
        assert first is not None
        assert duplicate is None  # checkpointed frontier is idempotent
        await repo.add_address_node(
            self.investigation_id,
            high,
            direction=Direction.BACKWARD,
            hop_distance=3,
            value_share=1000,
            discovered_reason="find_prev_vasp",
        )
        claimed = await repo.claim_frontier(self.investigation_id, limit=10)
        # Nearest-first: the nearer node is claimed even though the farther one
        # carries 100x the value share. The product's answer is measured in hops,
        # so the search spends its budget in that order (Ruling 1).
        assert [n.address_id for n in claimed] == [low, high]

        await repo.set_node_state(claimed[0].id, "expanded")
        remaining = await repo.claim_frontier(self.investigation_id, limit=10)
        assert [n.address_id for n in remaining] == [high]  # state survives = resumable

    async def test_value_share_ranks_within_a_hop_level(self, session: AsyncSession) -> None:
        """Value share still orders siblings — it just no longer outranks distance."""
        _, repo = await self._new_investigation(session)
        facts = FactRepository(session)
        small = await facts.get_or_create_address(Address("ethereum", "0xsmall"))
        big = await facts.get_or_create_address(Address("ethereum", "0xbig"))
        for address_id, share in ((small, 10), (big, 1000)):
            await repo.add_address_node(
                self.investigation_id,
                address_id,
                direction=Direction.BACKWARD,
                hop_distance=2,
                value_share=share,
                discovered_reason="find_prev_vasp",
            )
        claimed = await repo.claim_frontier(self.investigation_id, limit=10)
        assert [n.address_id for n in claimed] == [big, small]

    async def test_direction_is_part_of_node_identity(self, session: AsyncSession) -> None:
        """An address reached both ways holds two nodes, not one.

        Before this, the second objective's trace died at the first objective's
        node — the engine reported "trace exhausted" for an endpoint it had
        already stored (REVIEW_FINDINGS.md #4).
        """
        _, repo = await self._new_investigation(session)
        facts = FactRepository(session)
        both_ways = await facts.get_or_create_address(Address("ethereum", "0xbothways"))

        backward = await repo.add_address_node(
            self.investigation_id,
            both_ways,
            direction=Direction.BACKWARD,
            hop_distance=1,
            value_share=100,
            discovered_reason="find_prev_vasp",
        )
        forward = await repo.add_address_node(
            self.investigation_id,
            both_ways,
            direction=Direction.FORWARD,
            hop_distance=1,
            value_share=100,
            discovered_reason="find_next_vasp",
        )
        assert backward is not None
        assert forward is not None
        assert backward != forward, "each objective must hold its own node"

        # Re-discovery within one direction is still idempotent.
        assert (
            await repo.add_address_node(
                self.investigation_id,
                both_ways,
                direction=Direction.FORWARD,
                hop_distance=1,
                value_share=100,
                discovered_reason="find_next_vasp",
            )
            is None
        )

        by_direction = await repo.get_address_node(
            self.investigation_id, both_ways, Direction.FORWARD
        )
        assert by_direction is not None and by_direction.id == forward

    async def test_findings_round_trip_taxonomy_intact(self, session: AsyncSession) -> None:
        _, repo = await self._new_investigation(session)
        facts = FactRepository(session)
        subject = Address("ethereum", "0xexchange")
        subject_id = await facts.get_or_create_address(subject)
        finding = Finding(
            kind=FindingKind.VASP_ENDPOINT,
            subject=subject,
            summary="nearest previous VASP",
            confidence=0.9,
            direction=Direction.BACKWARD,
            evidence=(
                Evidence(
                    kind=EvidenceKind.ONCHAIN_FACT,
                    summary="funding path exists",
                    refs=("0x01", "0x02"),
                ),
                Evidence(
                    kind=EvidenceKind.THIRD_PARTY_CLAIM,
                    summary="labeled as exchange",
                    source="ofac-sdn@2026-08-01",
                    source_date=datetime(2026, 8, 1, tzinfo=UTC),
                    confidence=0.95,
                ),
                Evidence(
                    kind=EvidenceKind.HEURISTIC_INFERENCE,
                    summary="deposit pattern",
                    heuristic="deposit-address@1",
                    confidence=0.7,
                ),
            ),
        )
        await repo.add_finding(self.investigation_id, finding, subject_address_id=subject_id)
        await session.commit()

        restored = await repo.list_findings(self.investigation_id)
        assert restored == [finding]  # full domain equality, taxonomy preserved

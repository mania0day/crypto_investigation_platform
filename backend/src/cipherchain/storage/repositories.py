"""Repositories — the only SQL in the system.

Each repository owns one storage plane:

- :class:`FactRepository` writes/reads the global immutable fact store.
  All writes are idempotent upserts: re-normalizing a transaction that is
  already stored changes nothing (vision §4, immutable records).
- :class:`InvestigationRepository` owns the per-investigation overlay,
  including the checkpointed frontier.
- :class:`LabelRepository` owns the intel lifecycle's rows
  (LABEL_INTELLIGENCE.md §4): label claims and their append-only audit.
- :class:`VaspMetadataRepository` owns the per-OPERATOR facts a filing
  needs, keyed by entity rather than by address.
- :class:`ApiKeyRepository` owns the credentials for the write surface.

Repositories translate between core domain objects and rows; SQLAlchemy
types never escape this module (callers see core models and thin read
models).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import cast, func, literal, or_, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

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
    TxRef,
)
from cipherchain.storage.tables import (
    AddressRow,
    ApiKeyRow,
    AssetRow,
    EdgeRow,
    EvidenceRow,
    FindingRow,
    InvestigationRow,
    LabelEventRow,
    LabelRow,
    MovementRow,
    NodeRow,
    TransactionRow,
    VaspMetadataRow,
)


@dataclass(frozen=True, slots=True)
class AssetFacts:
    """Read model for an asset: enough to judge whether it can carry evidence."""

    id: int
    chain: str
    kind: str
    contract: str | None
    symbol: str


@dataclass(frozen=True, slots=True)
class VaspFindingOnNode:
    """A VASP finding plus the traversal facts of the node it was filed against.

    Named fields rather than a tuple because the pair grew into a quadruple and
    the two new members are booleans-with-consequences: a caller that unpacks
    positionally and gets ``speculative`` in the wrong slot reports a guessed
    endpoint as a traced one, which is the exact failure the field exists to
    prevent. It is not ``RankedFinding`` because that type lives in
    ``cipherchain.investigation``, whose package imports the engine, which imports
    this module; the domain composes it at the edge.
    """

    finding: Finding
    hop: int
    #: The path to this node crossed a link the engine SELECTED rather than
    #: witnessed (``nodes.speculative``). Sticky and inherited by descendants.
    speculative: bool = False
    #: The node's own words for why (``nodes.speculative_basis``), so the
    #: weakness a reader is shown is the run's actual reasoning.
    speculative_basis: str | None = None


@dataclass(frozen=True, slots=True)
class StoredMovement:
    """Read model for traversal: a movement plus its tx identity."""

    id: int
    transaction_id: int
    chain: str
    tx_hash: str
    from_address_id: int | None
    to_address_id: int | None
    kind: str
    asset_id: int
    amount: int
    timestamp: datetime
    # None means "never read", never "paid nothing": UTXO chains have no gas
    # price and rows written before the column existed are backfilled lazily.
    # The unique-gas-price mixer heuristic must skip nulls rather than treat
    # two of them as a match — two unknowns matching would link a deposit to an
    # arbitrary withdrawal and name it evidence.
    gas_price: int | None = None


@dataclass(frozen=True, slots=True)
class GraphNode:
    """Read model for drawing one traversal node.

    Carries the traversal facts a picture must not omit: how far out it sits,
    which objective reached it, whether its history was read in full, and why
    it stopped. A renderer that drops ``history_truncated`` or
    ``terminal_reason`` would draw a closed branch as an answered one, and one
    that drops ``speculative`` would draw a guess as a traced path.
    """

    id: int
    chain: str
    address: str
    direction: str | None
    hop_distance: int
    value_share: int | None
    state: str
    history_truncated: bool
    terminal_reason: str | None
    discovered_reason: str
    # Defaulted so the two speculation fields travel together and a caller can
    # never construct half of the pair; every read below sets both.
    speculative: bool = False
    speculative_basis: str | None = None
    # How many of this address's counterparties the supernode guard reached and
    # never followed. A picture that omits it draws a hub with sixty ways out as
    # a hub with twenty, which is the same overclaim as dropping
    # ``history_truncated``.
    counterparties_dropped: int | None = None


@dataclass(frozen=True, slots=True)
class GraphEdge:
    """Read model for drawing one traversal edge.

    ``kind`` separates an observed movement from a ``bridge`` link, which is
    inferred rather than witnessed as a single transfer. Bridge edges carry no
    movement, hence the optional asset fields.
    """

    id: int
    src_node_id: int
    dst_node_id: int
    kind: str
    movement_id: int | None
    amount: int | None
    asset_symbol: str | None
    asset_decimals: int | None
    asset_kind: str | None
    asset_contract: str | None
    tx_hash: str | None
    timestamp: datetime | None


class FactRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_or_create_address(self, address: Address) -> int:
        stmt = (
            pg_insert(AddressRow)
            .values(chain=address.chain, address=address.value)
            .on_conflict_do_nothing(constraint="uq_addresses_identity")
            .returning(AddressRow.id)
        )
        created = (await self._session.execute(stmt)).scalar_one_or_none()
        if created is not None:
            return created
        existing = await self._session.execute(
            select(AddressRow.id).where(
                AddressRow.chain == address.chain, AddressRow.address == address.value
            )
        )
        return existing.scalar_one()

    async def get_address(self, address_id: int) -> Address | None:
        row = await self._session.get(AddressRow, address_id)
        if row is None:
            return None
        return Address(chain=row.chain, value=row.address)

    async def asset_facts(self, asset_ids: Collection[int]) -> dict[int, AssetFacts]:
        """Resolve the assets a set of movements is denominated in."""
        if not asset_ids:
            return {}
        result = await self._session.execute(
            select(
                AssetRow.id, AssetRow.chain, AssetRow.kind, AssetRow.contract, AssetRow.symbol
            ).where(AssetRow.id.in_(set(asset_ids)))
        )
        return {
            row.id: AssetFacts(
                id=row.id,
                chain=row.chain,
                kind=row.kind,
                contract=row.contract,
                symbol=row.symbol,
            )
            for row in result
        }

    async def get_or_create_asset(self, asset: Asset) -> int:
        stmt = (
            pg_insert(AssetRow)
            .values(
                chain=asset.chain,
                kind=str(asset.kind),
                contract=asset.contract,
                symbol=asset.symbol,
                decimals=asset.decimals,
            )
            .on_conflict_do_nothing(constraint="uq_assets_identity")
            .returning(AssetRow.id)
        )
        created = (await self._session.execute(stmt)).scalar_one_or_none()
        if created is not None:
            return created
        conditions = [
            AssetRow.chain == asset.chain,
            AssetRow.kind == str(asset.kind),
            AssetRow.contract.is_(None)
            if asset.contract is None
            else AssetRow.contract == asset.contract,
        ]
        existing = await self._session.execute(select(AssetRow.id).where(*conditions))
        return existing.scalar_one()

    async def upsert_transaction(self, tx: TxRef, raw_sha256: str | None = None) -> int:
        stmt = (
            pg_insert(TransactionRow)
            .values(
                chain=tx.chain,
                tx_hash=tx.tx_hash,
                block_number=tx.block_number,
                timestamp=tx.timestamp,
                raw_sha256=raw_sha256,
            )
            .on_conflict_do_nothing(constraint="uq_transactions_identity")
            .returning(TransactionRow.id)
        )
        created = (await self._session.execute(stmt)).scalar_one_or_none()
        if created is not None:
            return created
        existing = await self._session.execute(
            select(TransactionRow.id).where(
                TransactionRow.chain == tx.chain, TransactionRow.tx_hash == tx.tx_hash
            )
        )
        return existing.scalar_one()

    async def store_movements(
        self, tx: TxRef, movements: Sequence[Movement], raw_sha256: str | None = None
    ) -> tuple[int, int]:
        """Persist a normalized transaction's movements idempotently.

        Returns (transaction_id, movements_newly_inserted).
        """
        tx_id = await self.upsert_transaction(tx, raw_sha256)
        address_ids: dict[Address, int] = {}
        asset_ids: dict[Asset, int] = {}
        rows: list[dict[str, Any]] = []
        for movement in movements:
            if movement.tx.chain != tx.chain or movement.tx.tx_hash != tx.tx_hash:
                raise ValueError("movement belongs to a different transaction")
            for endpoint in (movement.from_address, movement.to_address):
                if endpoint is not None and endpoint not in address_ids:
                    address_ids[endpoint] = await self.get_or_create_address(endpoint)
            if movement.asset not in asset_ids:
                asset_ids[movement.asset] = await self.get_or_create_asset(movement.asset)
            rows.append(
                {
                    "transaction_id": tx_id,
                    "from_address_id": (
                        None
                        if movement.from_address is None
                        else address_ids[movement.from_address]
                    ),
                    "to_address_id": (
                        None if movement.to_address is None else address_ids[movement.to_address]
                    ),
                    "kind": str(movement.kind),
                    "asset_id": asset_ids[movement.asset],
                    "amount": movement.amount,
                    # None stays None: it means "not read", never "paid
                    # nothing". The insert below is on_conflict_do_nothing, so a
                    # row already stored without a price is NOT updated by a
                    # later insert carrying one — `record_gas_prices`, whose
                    # write-once `WHERE gas_price IS NULL` leaves adapter values
                    # alone, remains the path for those.
                    "gas_price": movement.gas_price,
                    "index_in_tx": movement.index,
                    # Adapters supply a vantage-stable key; fall back to the
                    # positional key only when absent (safe only for
                    # always-full-tx sources).
                    "dedup_key": movement.dedup_key
                    if movement.dedup_key is not None
                    else f"{movement.kind}:{movement.index}",
                    "timestamp": movement.tx.timestamp,
                    "provider": movement.provenance.provider,
                    "retrieved_at": movement.provenance.retrieved_at,
                    "payload_sha256": movement.provenance.payload_sha256,
                }
            )
        if not rows:
            return tx_id, 0
        stmt = (
            pg_insert(MovementRow)
            .values(rows)
            .on_conflict_do_nothing(constraint="uq_movements_identity")
            .returning(MovementRow.id)
        )
        inserted = (await self._session.execute(stmt)).scalars().all()
        return tx_id, len(inserted)

    async def _movement_rows(
        self, *conditions: Any, limit: int, newest_first: bool = True
    ) -> list[StoredMovement]:
        time_order = MovementRow.timestamp.desc() if newest_first else MovementRow.timestamp.asc()
        stmt = (
            select(MovementRow, TransactionRow.chain, TransactionRow.tx_hash)
            .join(TransactionRow, MovementRow.transaction_id == TransactionRow.id)
            .where(*conditions)
            # Ties break on CHAIN data, never on MovementRow.id. `id` is a
            # database Identity assigned at insert time, so ordering by it makes
            # the result depend on the sequence in which an investigation
            # happened to ingest transactions. Sweep matching consumes each
            # candidate as it pairs, so one reordered tie changes every later
            # pairing — the same movements would produce different findings on a
            # re-run, breaking reproducibility.
            .order_by(time_order, TransactionRow.tx_hash.asc(), MovementRow.index_in_tx.asc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return [
            StoredMovement(
                id=row.id,
                transaction_id=row.transaction_id,
                chain=chain,
                tx_hash=tx_hash,
                from_address_id=row.from_address_id,
                to_address_id=row.to_address_id,
                kind=row.kind,
                asset_id=row.asset_id,
                amount=row.amount,
                timestamp=row.timestamp,
                gas_price=row.gas_price,
            )
            for row, chain, tx_hash in result.all()
        ]

    async def record_gas_prices(self, gas_prices: Mapping[int, int]) -> int:
        """Backfill gas price onto stored movements. Returns rows changed.

        Write-ONCE: a movement that already carries a gas price is left alone,
        so re-running the backfill over the same cached payloads changes
        nothing and reports 0 — the same idempotence every other write in this
        plane has (vision §4, immutable records). If two re-parses of one
        transaction ever disagreed, the second silently overwriting the first
        would be the worst available outcome, because the heuristic that reads
        this column concludes from EXACT equality.

        Statement per movement rather than one bulk CASE: this runs offline
        over cached bytes, and counting the rows each statement actually
        returned is the only way to report honestly how much of the store the
        backfill reached. RETURNING rather than ``rowcount``, matching the rest
        of this module, so nothing depends on a driver cursor attribute.
        """
        updated = 0
        for movement_id, price in gas_prices.items():
            result = await self._session.execute(
                update(MovementRow)
                .where(MovementRow.id == movement_id, MovementRow.gas_price.is_(None))
                .values(gas_price=price)
                .returning(MovementRow.id)
            )
            updated += len(result.scalars().all())
        return updated

    async def movements_to_address(
        self, address_id: int, *, before: datetime | None = None, limit: int = 100
    ) -> list[StoredMovement]:
        """Incoming movements — the backward-expansion query."""
        conditions: list[Any] = [MovementRow.to_address_id == address_id]
        if before is not None:
            conditions.append(MovementRow.timestamp <= before)
        return await self._movement_rows(*conditions, limit=limit)

    async def movements_from_address(
        self,
        address_id: int,
        *,
        after: datetime | None = None,
        limit: int = 100,
        newest_first: bool = False,
    ) -> list[StoredMovement]:
        """Outgoing movements — the forward-expansion query.

        Ordered EARLIEST-first by default: forward tracing follows the cash-out,
        and the immediate post-arrival hops are the ones that matter. Newest-first
        (the backward default) would truncate them away at a busy address
        (REVIEW_FINDINGS.md #7).

        Analysis wants the opposite of traversal here — see
        :meth:`movements_around_address`.
        """
        conditions: list[Any] = [MovementRow.from_address_id == address_id]
        if after is not None:
            conditions.append(MovementRow.timestamp >= after)
        return await self._movement_rows(*conditions, limit=limit, newest_first=newest_first)

    async def movements_around_address(
        self, address_id: int, *, limit: int
    ) -> tuple[list[StoredMovement], list[StoredMovement]]:
        """(incoming, outgoing) over a COMPARABLE window, for analysis.

        Traversal deliberately reads the two directions in opposite orders:
        backward wants the newest inflows, forward wants the earliest cash-out.
        That is right for expanding a graph and wrong for judging behaviour —
        on an address with more than ``limit`` movements a side, it hands a
        detector the newest receipts and the OLDEST sends, two windows that need
        not overlap at all. A sweep is a receipt followed by a forward, so a
        detector comparing disjoint windows finds pairs that never happened and
        misses the ones that did.

        Both sides are read newest-first so the windows describe the same recent
        period. Truncation of the side a node actually expands is reported
        through that node's ``history_truncated`` flag and the run's coverage
        statement (the engine flags a full expansion query). A cut on the OTHER
        side costs a detector context and never removes a branch from the
        trace, so it is not a coverage claim.
        """
        incoming = await self.movements_to_address(address_id, limit=limit)
        outgoing = await self.movements_from_address(address_id, limit=limit, newest_first=True)
        return incoming, outgoing

    async def movements_for_transaction(self, transaction_id: int) -> list[StoredMovement]:
        """All halves of one tx — UTXO counterparty resolution."""
        return await self._movement_rows(MovementRow.transaction_id == transaction_id, limit=10_000)


class InvestigationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        root_address_id: int,
        objectives: Sequence[str],
        budgets: dict[str, Any],
        engine_version: str,
        ruleset_version: str,
    ) -> InvestigationRow:
        row = InvestigationRow(
            root_address_id=root_address_id,
            objectives=list(objectives),
            budgets=budgets,
            spent={},
            engine_version=engine_version,
            ruleset_version=ruleset_version,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get(self, investigation_id: uuid.UUID) -> InvestigationRow | None:
        return await self._session.get(InvestigationRow, investigation_id)

    async def set_status(
        self, investigation_id: uuid.UUID, status: str, *, error: str | None = None
    ) -> None:
        await self._session.execute(
            update(InvestigationRow)
            .where(InvestigationRow.id == investigation_id)
            .values(status=status, error=error, updated_at=datetime.now(UTC))
        )

    async def claim_for_resume(
        self, investigation_id: uuid.UUID, *, budgets: dict[str, Any]
    ) -> bool:
        """Take a run that stopped on a budget and hand it back to the engine.

        One statement, guarded on ``status = 'partial'``, because the guard IS
        the concurrency control. Two operators resuming the same investigation a
        second apart would otherwise both read 'partial', both launch, and both
        loops would claim the same frontier rows — ``claim_frontier`` selects
        without locking — so the same addresses would be fetched twice, charged
        twice to the budget, and their findings filed twice. Here the second
        caller's UPDATE matches no row and it is told the run is already going.

        Only 'partial' is resumable, and that is enforced here rather than
        (only) at the edge: 'completed' means the frontier ran dry and the
        objectives were answered, so a "resume" would re-file terminals for a
        question already closed, and 'failed' means the engine raised — that
        needs diagnosis, not another lap.

        The new budgets are written in the same statement. The engine reads them
        from this row on entry and carries forward everything already spent
        (``seed_spent``), so resuming on the OLD budgets would exhaust on the
        first check and produce a second partial result identical to the first.
        """
        result = await self._session.execute(
            update(InvestigationRow)
            .where(InvestigationRow.id == investigation_id, InvestigationRow.status == "partial")
            .values(budgets=budgets, status="running", error=None, updated_at=datetime.now(UTC))
            .returning(InvestigationRow.id)
        )
        return result.scalar_one_or_none() is not None

    async def update_spent(self, investigation_id: uuid.UUID, spent: dict[str, Any]) -> None:
        await self._session.execute(
            update(InvestigationRow)
            .where(InvestigationRow.id == investigation_id)
            .values(spent=spent, updated_at=datetime.now(UTC))
        )

    @staticmethod
    def _checked_basis(basis: str) -> str:
        """The heuristic id a speculative node must carry, or raise.

        Both writers below go through here, because a basis is only useful if
        it NAMES something: the CHECK in the DB can only test for NULL, and
        ``''`` and ``'   '`` are both non-null, so a flag that explains nothing
        would satisfy it. Stored stripped so that two spellings of one
        heuristic id cannot appear in a report as two different guesses.
        """
        basis = basis.strip()
        if not basis:
            raise ValueError("a speculative node must name the heuristic that proposed it")
        return basis

    async def add_address_node(
        self,
        investigation_id: uuid.UUID,
        address_id: int,
        *,
        direction: Direction | None,
        hop_distance: int,
        value_share: int | None,
        discovered_reason: str,
        state: str = "frontier",
        speculative_basis: str | None = None,
    ) -> int | None:
        """Insert a frontier node; None when the node already exists.

        Speculation is passed as its BASIS, not as a flag, and the flag is
        derived: there is then no way to write a guessed node that cannot say
        which heuristic guessed it, and no way to record a heuristic's proposal
        on a node the graph will draw as traced. The DB holds the same
        equivalence (``ck_nodes_speculative_has_basis``).

        A blank basis is refused rather than derived into a flag. Passing ``''``
        here used to produce exactly the node this design exists to make
        unrepresentable — ``speculative`` true, explanation empty — because the
        equivalence CHECK only tests for NULL and ``''`` is not NULL. This is
        the path the docstring below calls the safe one, so it has to hold the
        same line ``mark_node_speculative`` does.

        Set at INSERT rather than by a follow-up update, because a crash
        between the two would leave a mixer-derived branch checkpointed as a
        clean one — and a resumed run would then trust it.
        """
        basis = None if speculative_basis is None else self._checked_basis(speculative_basis)
        stmt = (
            pg_insert(NodeRow)
            .values(
                investigation_id=investigation_id,
                kind="address",
                address_id=address_id,
                transaction_id=None,
                direction=None if direction is None else str(direction),
                hop_distance=hop_distance,
                value_share=value_share,
                state=state,
                discovered_reason=discovered_reason,
                speculative=basis is not None,
                speculative_basis=basis,
            )
            .on_conflict_do_nothing(constraint="uq_nodes_identity")
            .returning(NodeRow.id)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def mark_node_speculative(self, node_id: int, *, basis: str) -> None:
        """Record that this node was reached by a heuristic, not by a movement.

        ``basis`` is mandatory — it is the heuristic id (``mixer-exit-address-
        match@1``) the report cites when it states the branch's weakness.
        Descendants of a mixer crossing inherit their ancestor's basis: the
        guess they rest on is the ancestor's, and naming it is what lets a
        reader judge the whole branch rather than just its first hop.

        Raises when ``node_id`` matches nothing. This is the propagation path —
        it walks a subtree marking descendants of a mixer crossing — and a
        no-op on a stale or wrong id leaves that descendant flagged as traced
        while the caller believes it was marked. Every other outcome of this
        module is recoverable; that one puts a guess into a regulator's
        document wearing the clothes of an observed movement, so it is raised
        rather than returned as a boolean nobody is obliged to read.

        One-way. Nothing clears the flag, because nothing downstream of a
        mixer stops being downstream of it (REACHING_THE_VASP.md §3).
        """
        marked = await self._session.execute(
            update(NodeRow)
            .where(NodeRow.id == node_id)
            .values(speculative=True, speculative_basis=self._checked_basis(basis))
            .returning(NodeRow.id)
        )
        if marked.scalar_one_or_none() is None:
            raise LookupError(f"no node {node_id} to mark speculative")

    async def get_address_node(
        self, investigation_id: uuid.UUID, address_id: int, direction: Direction | None = None
    ) -> NodeRow | None:
        """The node an address occupies for one objective's trace.

        Direction is part of node identity, so an address reached both ways has
        one node per direction; asking without a direction asks for the
        direction-less root node.
        """
        result = await self._session.execute(
            select(NodeRow).where(
                NodeRow.investigation_id == investigation_id,
                NodeRow.kind == "address",
                NodeRow.address_id == address_id,
                NodeRow.direction.is_(None)
                if direction is None
                else NodeRow.direction == str(direction),
            )
        )
        return result.scalar_one_or_none()

    async def has_processed_sibling(
        self, investigation_id: uuid.UUID, address_id: int, *, exclude_node_id: int
    ) -> bool:
        """Has this address already been processed for the other objective?

        Behavioural detectors read an address's movement pattern, which does not
        depend on which objective's trace arrived. Running them once per node
        would file every pattern twice for an address reached both ways.
        """
        result = await self._session.execute(
            select(NodeRow.id)
            .where(
                NodeRow.investigation_id == investigation_id,
                NodeRow.kind == "address",
                NodeRow.address_id == address_id,
                NodeRow.id != exclude_node_id,
                NodeRow.state.in_(("expanded", "terminal")),
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def claim_frontier(self, investigation_id: uuid.UUID, limit: int) -> list[NodeRow]:
        """Clean first, then nearest: speculative asc, hop asc, value desc, id.

        The product's answer is phrased in hops ("nearest VASP, N hops away"),
        so the search claims in the order the goal is measured in. Under this
        ordering a node's first-discovery hop IS its minimum distance: its
        parent sat at hop-1 and was claimed before anything deeper, so no
        shorter path can appear later. Value share still ranks siblings within
        a hop level, which is where it affects order without affecting the
        meaning of "nearest" (Ruling 1, NEXT_MILESTONE_DECISIONS.md).

        ``speculative`` sorts AHEAD of hop distance, so every clean branch in
        the whole graph is exhausted before one mixer candidate is expanded,
        however near that candidate sits. A budget spent on a guess while a
        traced path remained unexplored would answer the question worse and
        less defensibly at the same time (REACHING_THE_VASP.md §3). Until a
        mixer heuristic files its first candidate every node is clean and this
        term changes nothing.
        """
        result = await self._session.execute(
            select(NodeRow)
            .where(NodeRow.investigation_id == investigation_id, NodeRow.state == "frontier")
            .order_by(
                NodeRow.speculative.asc(),
                NodeRow.hop_distance.asc(),
                NodeRow.value_share.desc().nulls_last(),
                NodeRow.id.asc(),
            )
            .limit(limit)
        )
        return list(result.scalars().all())

    async def count_frontier(self, investigation_id: uuid.UUID) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(NodeRow)
            .where(NodeRow.investigation_id == investigation_id, NodeRow.state == "frontier")
        )
        return int(result.scalar_one())

    async def count_nodes(self, investigation_id: uuid.UUID) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(NodeRow)
            .where(NodeRow.investigation_id == investigation_id)
        )
        return int(result.scalar_one())

    async def mark_history_truncated(self, node_id: int) -> None:
        """Record that this address was read only in part.

        Set by three different limits — a page with a next cursor, an
        acquisition feed no provider could serve (``HistoryPage.gaps``), and an
        expansion query that came back full — because all three mean the same
        thing to a reader: transactions exist for this address that this run
        never examined. Idempotent, so an address that hits two of them is one
        partially-read address rather than two.
        """
        await self._session.execute(
            update(NodeRow).where(NodeRow.id == node_id).values(history_truncated=True)
        )

    async def count_truncated_histories(self, investigation_id: uuid.UUID) -> int:
        """How many addresses were read only partially, as a durable count.

        Derived by query rather than from a run-local counter, so a resumed run
        reports the same coverage the original one did.
        """
        result = await self._session.execute(
            select(func.count())
            .select_from(NodeRow)
            .where(
                NodeRow.investigation_id == investigation_id,
                NodeRow.history_truncated.is_(True),
            )
        )
        return int(result.scalar_one())

    async def mark_expansion_capped(self, node_id: int, dropped: int) -> None:
        """Record that this address had more counterparties than were followed.

        Written where the guard fires rather than accumulated in the tracker,
        because a resumed run rebuilds its tracker from zero and would then
        report a clean expansion over an address the first run only half
        followed. ``dropped`` of zero is not written at all — the column's
        CHECK refuses it, and "capped nothing" is not a cap.
        """
        if dropped <= 0:
            return
        await self._session.execute(
            update(NodeRow).where(NodeRow.id == node_id).values(counterparties_dropped=dropped)
        )

    async def count_capped_expansions(self, investigation_id: uuid.UUID) -> tuple[int, int]:
        """(addresses whose expansion was capped, counterparties dropped in all).

        Both numbers in one query and one return, because a report that printed
        one without the other invites the two readings a coverage figure must
        not have: "one address was capped" says nothing about whether it lost
        two branches or two hundred.
        """
        result = await self._session.execute(
            select(func.count(), func.coalesce(func.sum(NodeRow.counterparties_dropped), 0))
            .select_from(NodeRow)
            .where(
                NodeRow.investigation_id == investigation_id,
                NodeRow.counterparties_dropped.is_not(None),
            )
        )
        nodes, dropped = result.one()
        return int(nodes), int(dropped)

    async def mark_feed_unavailable(self, node_id: int, code: str) -> None:
        """Record WHICH acquisition feed no provider could serve for this address.

        ``mark_history_truncated`` already records THAT the address was read only
        in part, and deliberately merges three different limits to say it. This
        records the one thing that union cannot: what is actually missing. A
        reader deciding whether the gap could have hidden their answer needs to
        know that the token feed died and the native feed did not — an address
        whose ETH history is complete and whose USDT history is absent is a very
        different piece of evidence from one whose page was simply cut short.

        Appends, and never duplicates: an address can lose two feeds, and a
        resumed run can re-read an address it already recorded. Idempotent
        writes matter here because the alternative is a coverage figure that
        grows every time a run is resumed.
        """
        if not code:
            # An unnamed gap would satisfy every "is a feed missing?" test
            # downstream while printing nothing a reader could act on. The
            # column's CHECK refuses the empty array; this refuses the empty
            # name that would fill it.
            raise ValueError("a feed gap must name the feed that was lost")
        await self._session.execute(
            update(NodeRow)
            .where(
                NodeRow.id == node_id,
                # SQL, not read-modify-write: two feeds are recorded for the same
                # node microseconds apart, and a Python-side append would read
                # both rows before either wrote and lose one of them.
                or_(
                    NodeRow.feeds_unavailable.is_(None),
                    ~NodeRow.feeds_unavailable.contains([code]),
                ),
            )
            .values(
                feeds_unavailable=func.coalesce(
                    NodeRow.feeds_unavailable, cast(literal("[]"), JSONB)
                ).concat(cast(literal(json.dumps([code])), JSONB))
            )
        )

    async def count_nodes_missing_feeds(
        self, investigation_id: uuid.UUID
    ) -> tuple[int, tuple[str, ...]]:
        """(addresses that lost at least one feed, the distinct feed codes).

        Both in one call for the reason ``count_capped_expansions`` returns a
        pair: "three addresses were read through a dead feed" does not tell a
        reader whether the missing rows were token transfers or internal ones,
        and only the second half says whether the gap could hide a VASP.
        """
        result = await self._session.execute(
            select(NodeRow.feeds_unavailable).where(
                NodeRow.investigation_id == investigation_id,
                NodeRow.feeds_unavailable.is_not(None),
            )
        )
        nodes = 0
        codes: set[str] = set()
        for (recorded,) in result.all():
            if not recorded:
                continue
            nodes += 1
            codes.update(str(code) for code in recorded)
        return nodes, tuple(sorted(codes))

    async def count_nodes_terminated_for(self, investigation_id: uuid.UUID, reason: str) -> int:
        """Nodes closed for one specific reason.

        Read by reason rather than inferred from `state` + `hop_distance`: five
        situations set state='terminal', and a VASP found exactly at max_depth
        is indistinguishable from one cut off by it.
        """
        result = await self._session.execute(
            select(func.count())
            .select_from(NodeRow)
            .where(
                NodeRow.investigation_id == investigation_id,
                NodeRow.terminal_reason == reason,
            )
        )
        return int(result.scalar_one())

    async def set_node_state(self, node_id: int, state: str, *, reason: str | None = None) -> None:
        values: dict[str, object] = {"state": state}
        if reason is not None:
            values["terminal_reason"] = reason
        await self._session.execute(update(NodeRow).where(NodeRow.id == node_id).values(**values))

    async def add_edge(
        self,
        investigation_id: uuid.UUID,
        *,
        src_node_id: int,
        dst_node_id: int,
        movement_id: int | None,
        kind: str = "movement",
    ) -> None:
        stmt = (
            pg_insert(EdgeRow)
            .values(
                investigation_id=investigation_id,
                src_node_id=src_node_id,
                dst_node_id=dst_node_id,
                movement_id=movement_id,
                kind=kind,
            )
            .on_conflict_do_nothing(constraint="uq_edges_identity")
        )
        await self._session.execute(stmt)

    async def add_finding(
        self, investigation_id: uuid.UUID, finding: Finding, *, subject_address_id: int
    ) -> int:
        row = FindingRow(
            investigation_id=investigation_id,
            kind=str(finding.kind),
            subject_address_id=subject_address_id,
            direction=None if finding.direction is None else str(finding.direction),
            summary=finding.summary,
            confidence=finding.confidence,
        )
        self._session.add(row)
        await self._session.flush()
        for item in finding.evidence:
            self._session.add(
                EvidenceRow(
                    finding_id=row.id,
                    kind=str(item.kind),
                    summary=item.summary,
                    refs=list(item.refs),
                    source=item.source,
                    source_date=item.source_date,
                    heuristic=item.heuristic,
                    confidence=item.confidence,
                )
            )
        await self._session.flush()
        return row.id

    async def list_findings(self, investigation_id: uuid.UUID) -> list[Finding]:
        """Reconstruct domain findings, evidence taxonomy intact."""
        finding_rows = (
            (
                await self._session.execute(
                    select(FindingRow, AddressRow)
                    .join(AddressRow, FindingRow.subject_address_id == AddressRow.id)
                    .where(FindingRow.investigation_id == investigation_id)
                    .order_by(FindingRow.id.asc())
                )
            )
            .tuples()
            .all()
        )
        if not finding_rows:
            return []
        evidence_rows = (
            (
                await self._session.execute(
                    select(EvidenceRow)
                    .where(EvidenceRow.finding_id.in_([f.id for f, _ in finding_rows]))
                    .order_by(EvidenceRow.id.asc())
                )
            )
            .scalars()
            .all()
        )
        by_finding: dict[int, list[Evidence]] = {}
        for ev in evidence_rows:
            by_finding.setdefault(ev.finding_id, []).append(
                Evidence(
                    kind=EvidenceKind(ev.kind),
                    summary=ev.summary,
                    refs=tuple(ev.refs),
                    source=ev.source,
                    source_date=ev.source_date,
                    heuristic=ev.heuristic,
                    confidence=ev.confidence,
                )
            )
        return [
            Finding(
                kind=FindingKind(f.kind),
                subject=Address(chain=addr.chain, value=addr.address),
                summary=f.summary,
                confidence=f.confidence,
                evidence=tuple(by_finding.get(f.id, [])),
                direction=None if f.direction is None else Direction(f.direction),
            )
            for f, addr in finding_rows
        ]

    async def unnamed_service_endpoints(self, investigation_id: uuid.UUID) -> list[tuple[str, str]]:
        """(chain, address) for every VASP endpoint this run could not NAME.

        Structural, not prose-based: an endpoint is "unnamed" when no ACTIVE
        label exists for its (chain, address), which is exactly the condition
        under which the engine filed a behavioural finding instead of a sourced
        one. Matching the heuristic's summary text would work today and break
        the day someone rewords it — ``engine.is_speculative_finding`` is
        already flagged in this file as that kind of stopgap.

        This is the worklist for explorer-tag enrichment, and its size is the
        reason enrichment is affordable: on a 1,849-node trace it returned 22.
        """
        active_label = (
            select(literal(1))
            .where(
                LabelRow.chain == AddressRow.chain,
                LabelRow.address == AddressRow.address,
                LabelRow.status == "active",
            )
            .correlate(AddressRow)
            .exists()
        )
        rows = await self._session.execute(
            select(AddressRow.chain, AddressRow.address)
            .join(FindingRow, FindingRow.subject_address_id == AddressRow.id)
            .where(
                FindingRow.investigation_id == investigation_id,
                FindingRow.kind == str(FindingKind.VASP_ENDPOINT),
                ~active_label,
            )
            .distinct()
        )
        return [(chain, address) for chain, address in rows.all()]

    async def vasp_findings_with_hops(self, investigation_id: uuid.UUID) -> list[VaspFindingOnNode]:
        """VASP findings paired with the traversal facts of the node they were filed against.

        Returned as a storage-local read model rather than the domain's
        ``RankedFinding``: that type lives in ``cipherchain.investigation``, whose
        package imports the engine, which imports this module. The composition
        happens at the edge.

        Hop belongs to the traversal, not to the conclusion, so it is joined in
        here rather than stored on the finding. Selecting "nearest" from
        insertion order stopped being correct the moment labels began resolving
        at discovery: a hop-2 sourced label can now be recorded before a hop-1
        behavioural inference.

        ``speculative`` is joined for a harder reason than hop. It is a property
        of the *path*, and a finding carries no trace of it: a mixer-exit branch
        can reach an address a sourced label names perfectly well, and the label
        stays true while the path to it remains a guess. Without this column
        crossing the boundary, the answer layer cannot tell the two apart and a
        selected endpoint prints in the same slot as a traced one
        (REACHING_THE_VASP.md §3). ``engine.is_speculative_finding`` parses the
        same fact back out of an evidence sentence and is explicitly a stopgap;
        this is the durable route it defers to.

        The join is exact — a VASP finding's ``direction`` is copied from the
        node it was filed against, and node identity is (address, direction).
        """
        findings = [f for f in await self.list_findings(investigation_id) if f.direction]
        vasp = [f for f in findings if f.kind is FindingKind.VASP_ENDPOINT]
        if not vasp:
            return []
        rows = (
            (
                await self._session.execute(
                    select(
                        AddressRow.address,
                        NodeRow.direction,
                        NodeRow.hop_distance,
                        NodeRow.speculative,
                        NodeRow.speculative_basis,
                    )
                    .join(NodeRow, NodeRow.address_id == AddressRow.id)
                    .where(
                        NodeRow.investigation_id == investigation_id,
                        NodeRow.kind == "address",
                        NodeRow.direction.is_not(None),
                    )
                )
            )
            .tuples()
            .all()
        )
        nodes = {
            (address, direction): (hop, speculative, basis)
            for address, direction, hop, speculative, basis in rows
        }
        ranked: list[VaspFindingOnNode] = []
        for finding in vasp:
            node = nodes.get((finding.subject.value, str(finding.direction)))
            if node is None:  # pragma: no cover — a finding always has its node
                continue
            hop, speculative, basis = node
            ranked.append(
                VaspFindingOnNode(
                    finding=finding,
                    hop=hop,
                    speculative=bool(speculative),
                    speculative_basis=basis,
                )
            )
        return ranked

    async def graph_nodes(
        self,
        investigation_id: uuid.UUID,
        *,
        limit: int | None = None,
        per_level: int | None = None,
    ) -> list[GraphNode]:
        """Address nodes of the traversal, nearest first.

        Ordered hop ascending then value_share descending — the SAME order the
        frontier claims in, so a truncated read keeps the nodes the engine
        itself considered most significant rather than an arbitrary page.

        ``per_level`` caps each (hop, direction) group separately, and that is
        the difference between a picture with depth and one without. A flat cap
        is spent entirely on the nearest hop whenever the first hop fans out
        wide: measured on a live trace reaching hops -2..+2, a flat 120 returned
        hops -1..+1 only and silently dropped all 202 nodes at hop 2. Budgeting
        per level keeps every hop the trace actually reached on screen.

        Value share is a fine rule for ordinary nodes and the wrong one for the
        node a reader came to see. On investigation ba0783b9 the headline answer
        named a VASP at hop 2 backward whose node ranked 22nd of 96 in its
        level, so a ``per_level`` of 20 dropped the single address the answer was
        about: the picture could not show the reader what they were reading
        about. Two classes of node are therefore returned outside the quota — an
        address some source has made a claim about, and an address a
        ``vasp_endpoint`` finding was filed against.

        A pinned node comes with the chain of nodes back toward the root, which
        is the difference between showing the answer and showing a dot. Edges
        are drawn only when both their endpoints are on screen, so a pin whose
        parent lost its own quota slot has no line to anything: ba0783b9's
        endpoint is at hop 2 backward and its hop-1 parent carries neither a
        label nor a finding, so the address the report was about would arrive
        unattached to the trace that reached it.
        """
        base = (
            select(NodeRow, AddressRow)
            .join(AddressRow, NodeRow.address_id == AddressRow.id)
            .where(
                NodeRow.investigation_id == investigation_id,
                NodeRow.kind == "address",
            )
        )
        if per_level is not None:
            # Both tests are EXISTS rather than joins because both can match
            # more than once: a label's identity is (chain, address, source), so
            # two sources agreeing about one address is two rows, and an address
            # can carry a VASP finding in each direction. A join would return
            # that node two or three times and the renderer would draw it twice.
            # Labels are keyed by (chain, address) — there is no address_id on
            # that table, hence the join back through AddressRow here.
            labelled = (
                select(literal(1))
                .where(
                    LabelRow.chain == AddressRow.chain,
                    LabelRow.address == AddressRow.address,
                    # Only 'active' claims attribute or name anywhere else in
                    # the system (LABEL_INTELLIGENCE.md §4). Without this a
                    # RETIRED claim — one its source withdrew — still bought its
                    # address a permanent slot in the picture, and spent budget
                    # the deeper hops needed to be drawn at all.
                    LabelRow.status == "active",
                )
                .correlate(AddressRow)
                .exists()
            )
            answer_endpoint = (
                select(literal(1))
                .where(
                    FindingRow.investigation_id == investigation_id,
                    FindingRow.kind == str(FindingKind.VASP_ENDPOINT),
                    FindingRow.subject_address_id == NodeRow.address_id,
                )
                .correlate(NodeRow)
                .exists()
            )
            flagged = (
                select(
                    NodeRow.id.label("id"),
                    NodeRow.hop_distance.label("hop_distance"),
                    NodeRow.direction.label("direction"),
                    NodeRow.value_share.label("value_share"),
                    or_(labelled, answer_endpoint).label("pinned"),
                )
                .join(AddressRow, NodeRow.address_id == AddressRow.id)
                .where(
                    NodeRow.investigation_id == investigation_id,
                    NodeRow.kind == "address",
                )
                .subquery()
            )
            by_value = (
                flagged.c.value_share.desc().nullslast(),
                flagged.c.id.asc(),
            )
            # Two row numbers, because one cannot answer both questions.
            # ``rank`` is position in the whole level and decides the QUOTA.
            # ``tier`` is position among that level's own kind — pins ranked
            # against pins, ordinary nodes against ordinary nodes — and decides
            # the ORDER. Ranking the pins separately is what lets them be
            # interleaved rather than hoisted; see the order_by below.
            ranked = select(
                flagged.c.id,
                flagged.c.hop_distance,
                flagged.c.pinned,
                func.row_number()
                .over(
                    partition_by=(flagged.c.hop_distance, flagged.c.direction),
                    order_by=by_value,
                )
                .label("rank"),
                func.row_number()
                .over(
                    partition_by=(
                        flagged.c.hop_distance,
                        flagged.c.direction,
                        flagged.c.pinned,
                    ),
                    order_by=by_value,
                )
                .label("tier"),
            ).subquery()
            # Every node on the way from a pin back toward the root, because a
            # pin nobody can reach is not an answer. ``graph_edges`` keeps only
            # edges with BOTH endpoints on screen, so a pinned node whose parent
            # lost its quota slot is drawn as a dot floating off the graph —
            # ba0783b9's endpoint sits at hop 2 behind an unlabelled hop-1
            # parent, so that is the target case, not a corner one.
            #
            # Only edges from a STRICTLY shallower hop are followed. That is
            # what "toward the root" means, and it is also what terminates the
            # walk: hop distance is a non-negative integer and drops by at least
            # one per step. The edge table genuinely contains cycles — a
            # counterparty leading back to an address already reached is stored
            # like any other edge — so an unguarded walk would not terminate.
            chain = (
                select(
                    ranked.c.id.label("node_id"),
                    ranked.c.hop_distance.label("hop_distance"),
                    ranked.c.tier.label("tier"),
                )
                .where(ranked.c.pinned)
                .cte("pin_chain", recursive=True)
            )
            ancestor = aliased(NodeRow, name="ancestor")
            chain = chain.union(
                select(ancestor.id, ancestor.hop_distance, chain.c.tier)
                .join(EdgeRow, EdgeRow.src_node_id == ancestor.id)
                .join(chain, chain.c.node_id == EdgeRow.dst_node_id)
                .where(
                    EdgeRow.investigation_id == investigation_id,
                    ancestor.hop_distance < chain.c.hop_distance,
                )
            )
            # MIN, so a node's key never sorts after anything it leads to: a
            # chain member inherits the tier of the EARLIEST pin below it. With
            # a plain tier an ancestor could fall outside the global ``limit``
            # that admitted its own pin, and the dot would float again for the
            # callers who ask for fewer nodes. Ties break on hop ascending and
            # an ancestor is strictly shallower, so a path is always read
            # root-first and no limit can cut it in the middle.
            supporting = (
                select(
                    chain.c.node_id.label("node_id"),
                    func.min(chain.c.tier).label("pin_tier"),
                )
                .group_by(chain.c.node_id)
                .subquery()
            )
            base = (
                base.join(ranked, NodeRow.id == ranked.c.id)
                .outerjoin(supporting, supporting.c.node_id == NodeRow.id)
                .where(
                    or_(
                        ranked.c.rank <= per_level,
                        ranked.c.pinned,
                        supporting.c.node_id.is_not(None),
                    )
                )
            )
            # Tier-major, so an overall ``limit`` degrades EVENLY across hops:
            # it takes the first node of every level and of both kinds, then
            # the second of each, and so on. Ordering by hop first would let the
            # global cap re-introduce the very depth loss ``per_level`` exists
            # to prevent — and so, measurably, did sorting every pin ahead of
            # every ordinary node. At the endpoint's own defaults (limit 240,
            # per_level 20) a trace with 250 labelled addresses at hop 1 spent
            # the whole budget in that one hop and returned
            # {1: 240, 2: 0, 3: 0, 4: 0}; the same data interleaved returns
            # {1: 180, 2: 20, 3: 20, 4: 20}. Interleaving also keeps the top
            # node of every level in the first tier, so no number of low-value
            # pins can evict the biggest counterparty of a deeper hop.
            stmt = base.order_by(
                func.least(
                    ranked.c.tier, func.coalesce(supporting.c.pin_tier, ranked.c.tier)
                ).asc(),
                NodeRow.hop_distance.asc(),
                NodeRow.value_share.desc().nullslast(),
                NodeRow.id.asc(),
            )
        else:
            stmt = base.order_by(
                NodeRow.hop_distance.asc(),
                NodeRow.value_share.desc().nullslast(),
                NodeRow.id.asc(),
            )
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = (await self._session.execute(stmt)).tuples().all()
        return [
            GraphNode(
                id=node.id,
                chain=addr.chain,
                address=addr.address,
                direction=node.direction,
                hop_distance=node.hop_distance,
                value_share=node.value_share,
                state=node.state,
                history_truncated=node.history_truncated,
                terminal_reason=node.terminal_reason,
                discovered_reason=node.discovered_reason,
                speculative=node.speculative,
                speculative_basis=node.speculative_basis,
                counterparties_dropped=node.counterparties_dropped,
            )
            for node, addr in rows
        ]

    async def count_graph_nodes(self, investigation_id: uuid.UUID) -> int:
        """Total address nodes, so a limited read can say what it left out."""
        result = await self._session.execute(
            select(func.count())
            .select_from(NodeRow)
            .where(
                NodeRow.investigation_id == investigation_id,
                NodeRow.kind == "address",
            )
        )
        return int(result.scalar_one())

    async def arriving_movement_ids(self, node_id: int) -> set[int]:
        """Movement ids on the edges pointing INTO one node.

        "How did the trace get here?", asked of a single node. Separate from
        ``graph_edges`` because that method exists to DRAW the graph: it carries
        three outer joins and builds an asset symbol and a tx hash for every
        edge in the investigation, and the one caller here — the mixer ladder's
        anchor — discards all of it to look up a handful of ids. On a pool node
        that is the whole edge table read to answer a question about one row.
        """
        rows = await self._session.execute(
            select(EdgeRow.movement_id).where(
                EdgeRow.dst_node_id == node_id,
                EdgeRow.movement_id.is_not(None),
            )
        )
        return {movement_id for (movement_id,) in rows.all() if movement_id is not None}

    async def graph_edges(
        self, investigation_id: uuid.UUID, *, node_ids: Collection[int] | None = None
    ) -> list[GraphEdge]:
        """Edges of the traversal, with the movement each one stands for.

        ``node_ids`` restricts the result to edges whose BOTH endpoints are in
        the set — a dangling edge would draw a line to a node that is not on
        screen. Bridge edges have no movement, so the asset join is outer.
        """
        stmt = (
            select(EdgeRow, MovementRow, AssetRow, TransactionRow)
            .outerjoin(MovementRow, EdgeRow.movement_id == MovementRow.id)
            .outerjoin(AssetRow, MovementRow.asset_id == AssetRow.id)
            .outerjoin(TransactionRow, MovementRow.transaction_id == TransactionRow.id)
            .where(EdgeRow.investigation_id == investigation_id)
            .order_by(EdgeRow.id.asc())
        )
        if node_ids is not None:
            keep = set(node_ids)
            if not keep:
                return []
            stmt = stmt.where(
                EdgeRow.src_node_id.in_(keep),
                EdgeRow.dst_node_id.in_(keep),
            )
        rows = (await self._session.execute(stmt)).tuples().all()
        return [
            GraphEdge(
                id=edge.id,
                src_node_id=edge.src_node_id,
                dst_node_id=edge.dst_node_id,
                kind=edge.kind,
                movement_id=edge.movement_id,
                amount=None if movement is None else movement.amount,
                asset_symbol=None if asset is None else asset.symbol,
                asset_decimals=None if asset is None else asset.decimals,
                asset_kind=None if asset is None else asset.kind,
                asset_contract=None if asset is None else asset.contract,
                tx_hash=None if tx is None else tx.tx_hash,
                timestamp=None if movement is None else movement.timestamp,
            )
            for edge, movement, asset, tx in rows
        ]


def asset_kind_from_row(kind: str) -> AssetKind:
    return AssetKind(kind)


@dataclass(frozen=True, slots=True)
class StoredLabel:
    """Read model for one label claim, all fields as stored.

    Plain strings rather than ``LabelRecord``/``AddressRole`` — those live in
    modules that import back through ``cipherchain.investigation``, and this module
    must not close that cycle (same inversion as ``vasp_findings_with_hops``).
    The intel domain composes the domain types.
    """

    id: int
    chain: str
    address: str
    entity: str
    category: str
    role: str
    confidence: float
    status: str
    method: str
    source: str
    source_date: datetime | None
    retrieved_at: datetime
    corroborated_by: str | None
    evidence_url: str | None
    reporter: str | None


@dataclass(frozen=True, slots=True)
class StoredLabelEvent:
    id: int
    occurred_at: datetime
    kind: str
    label_id: int
    reason: str
    actor: str


def _stored_label(row: LabelRow) -> StoredLabel:
    return StoredLabel(
        id=row.id,
        chain=row.chain,
        address=row.address,
        entity=row.entity,
        category=row.category,
        role=row.role,
        confidence=row.confidence,
        status=row.status,
        method=row.method,
        source=row.source,
        source_date=row.source_date,
        retrieved_at=row.retrieved_at,
        corroborated_by=row.corroborated_by,
        evidence_url=row.evidence_url,
        reporter=row.reporter,
    )


class LabelRepository:
    """The intel lifecycle's storage. Rows in, StoredLabel out, events appended.

    Deliberately mechanism, not policy: which method makes a claim ``active``
    on arrival, what corroboration requires, when retirement happens — all of
    that belongs to the intel domain. The one rule enforced HERE is that
    :meth:`upsert_claim` never changes ``status`` on an existing row, because
    a re-harvest quietly resurrecting a retired label (or activating a pending
    one) would be a lifecycle transition with no event and no decision behind
    it. Transitions go through :meth:`set_status`, explicitly.

    Single-writer by design (the harvester is one worker); the upsert is
    read-then-write, not a race-proof ON CONFLICT.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_claim(
        self,
        *,
        chain: str,
        address: str,
        entity: str,
        category: str,
        role: str,
        confidence: float,
        status: str,
        method: str,
        source: str,
        retrieved_at: datetime,
        source_date: datetime | None = None,
        evidence_url: str | None = None,
        reporter: str | None = None,
    ) -> tuple[int, str]:
        """Insert or refresh one source's claim. Returns ``(label_id, outcome)``.

        Outcome is ``'added'`` | ``'updated'`` | ``'unchanged'`` so the caller
        can decide what deserves an event — an unchanged re-harvest bumps
        ``retrieved_at`` (the claim was re-confirmed at this time) but is not
        news.
        """
        existing = (
            await self._session.execute(
                select(LabelRow).where(
                    LabelRow.chain == chain,
                    LabelRow.address == address,
                    LabelRow.source == source,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            row = LabelRow(
                chain=chain,
                address=address,
                entity=entity,
                category=category,
                role=role,
                confidence=confidence,
                status=status,
                method=method,
                source=source,
                source_date=source_date,
                retrieved_at=retrieved_at,
                evidence_url=evidence_url,
                reporter=reporter,
            )
            self._session.add(row)
            await self._session.flush()
            return row.id, "added"

        # method refreshes WITH the claim: a source can gain or lose its
        # signatures between harvests, and a stored method that outlives the
        # verification it describes overstates provenance — the exact thing
        # the RFC calls the product. (Found by review: it was omitted here,
        # which froze every claim's evidence class at first sight, silently.)
        changed = (
            existing.entity != entity
            or existing.category != category
            or existing.role != role
            or existing.confidence != confidence
            or existing.method != method
            or existing.source_date != source_date
            or existing.evidence_url != evidence_url
        )
        existing.entity = entity
        existing.category = category
        existing.role = role
        existing.confidence = confidence
        existing.method = method
        existing.source_date = source_date
        existing.evidence_url = evidence_url
        existing.retrieved_at = retrieved_at
        # status, corroborated_by, reporter: untouched on purpose (docstring).
        await self._session.flush()
        return existing.id, "updated" if changed else "unchanged"

    async def set_status(
        self,
        label_id: int,
        status: str,
        *,
        corroborated_by: str | None = None,
        clear_corroboration: bool = False,
    ) -> None:
        """``clear_corroboration`` exists for demotion: a row whose basis
        stopped holding must not keep citing the corroborator that once
        agreed with its OLD content — a dangling citation is worse than none.
        """
        values: dict[str, Any] = {"status": status}
        if clear_corroboration:
            values["corroborated_by"] = None
        elif corroborated_by is not None:
            values["corroborated_by"] = corroborated_by
        await self._session.execute(
            update(LabelRow).where(LabelRow.id == label_id).values(**values)
        )

    async def get_label(self, label_id: int) -> StoredLabel | None:
        row = await self._session.get(LabelRow, label_id)
        return None if row is None else _stored_label(row)

    async def claims_for(self, chain: str, address: str) -> list[StoredLabel]:
        """Every source's claim about one address, whatever its status."""
        rows = (
            (
                await self._session.execute(
                    select(LabelRow)
                    .where(LabelRow.chain == chain, LabelRow.address == address)
                    .order_by(LabelRow.confidence.desc(), LabelRow.source.asc())
                )
            )
            .scalars()
            .all()
        )
        return [_stored_label(r) for r in rows]

    async def active_labels(self) -> list[StoredLabel]:
        """The attributor's load: active rows and nothing else, ever."""
        rows = (
            (
                await self._session.execute(
                    select(LabelRow).where(LabelRow.status == "active").order_by(LabelRow.id.asc())
                )
            )
            .scalars()
            .all()
        )
        return [_stored_label(r) for r in rows]

    async def latest_event_id(self) -> int:
        """A watermark for "has the label store changed since I read it?".

        The attributor is an in-memory index, so a harvest that lands in this
        table is invisible to a running server until something notices. Polling
        the labels themselves would mean re-reading 75,000 rows to discover that
        none of them moved; ``label_events`` is append-only and its id is
        monotonic, so one indexed max() answers the question instead.

        Zero means "no events yet", which is a real state (a store nobody has
        imported into) and not an error.
        """
        result = await self._session.execute(select(func.max(LabelEventRow.id)))
        return int(result.scalar() or 0)

    async def pending_labels_for(
        self, chain: str, addresses: Sequence[str]
    ) -> dict[str, list[StoredLabel]]:
        """Pending claims about these addresses, keyed by address.

        The read side of the unverified-lead channel. Kept separate from
        :meth:`active_labels` on purpose and used by nothing that attributes:
        the attributor loads active rows and only active rows, so a name that
        arrives here can be SHOWN to an investigator without ever becoming a
        thing the engine can cite, rank, or answer an objective with.

        Empty input returns an empty mapping rather than scanning the table —
        a graph page with no suspected endpoints must not read 75,000 rows.
        """
        wanted = list(dict.fromkeys(addresses))
        if not wanted:
            return {}
        rows = (
            (
                await self._session.execute(
                    select(LabelRow)
                    .where(
                        LabelRow.chain == chain,
                        LabelRow.address.in_(wanted),
                        LabelRow.status == "pending",
                    )
                    .order_by(LabelRow.confidence.desc(), LabelRow.source.asc())
                )
            )
            .scalars()
            .all()
        )
        grouped: dict[str, list[StoredLabel]] = {}
        for row in rows:
            grouped.setdefault(row.address, []).append(_stored_label(row))
        return grouped

    async def pending_labels(self) -> list[StoredLabel]:
        """The corroboration bot's worklist, oldest claim first."""
        rows = (
            (
                await self._session.execute(
                    select(LabelRow).where(LabelRow.status == "pending").order_by(LabelRow.id.asc())
                )
            )
            .scalars()
            .all()
        )
        return [_stored_label(r) for r in rows]

    async def add_event(self, *, label_id: int, kind: str, reason: str, actor: str) -> int:
        row = LabelEventRow(label_id=label_id, kind=kind, reason=reason, actor=actor)
        self._session.add(row)
        await self._session.flush()
        return row.id

    async def events_after(self, cursor: int, *, limit: int = 100) -> list[StoredLabelEvent]:
        """Events with id > cursor, oldest first — the UI's poll loop."""
        rows = (
            (
                await self._session.execute(
                    select(LabelEventRow)
                    .where(LabelEventRow.id > cursor)
                    .order_by(LabelEventRow.id.asc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [
            StoredLabelEvent(
                id=r.id,
                occurred_at=r.occurred_at,
                kind=r.kind,
                label_id=r.label_id,
                reason=r.reason,
                actor=r.actor,
            )
            for r in rows
        ]


@dataclass(frozen=True, slots=True)
class StoredVaspMetadata:
    """Read model for one operator's filing facts, all fields as stored.

    Every descriptive field is optional and ``source``/``source_date`` are not:
    a consumer must always be able to say where a jurisdiction claim came from,
    and must always be able to render a partially-known operator without
    inventing the rest.
    """

    entity: str
    jurisdiction: str | None
    legal_entity: str | None
    kyc_regime: str | None
    kyc_since: date | None
    le_request_channel: str | None
    source: str
    source_date: date


class VaspMetadataRepository:
    """Per-OPERATOR facts, keyed by entity — what a name needs to become a filing.

    Mechanism only. Which entity stem a label's ``entity`` reduces to, and
    whether a report is allowed to print an unmetadata'd name, are decisions for
    the layers above; this one stores and returns rows.

    Reads return ``None`` rather than an empty record for an unknown entity,
    because "we hold no metadata for Binance" and "we hold metadata saying
    nothing about Binance" must not render identically — the first is a
    coverage gap the report should state, the second cannot occur (provenance
    is mandatory, so a row always says something about where it came from).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(
        self,
        *,
        entity: str,
        source: str,
        source_date: date,
        jurisdiction: str | None = None,
        legal_entity: str | None = None,
        kyc_regime: str | None = None,
        kyc_since: date | None = None,
        le_request_channel: str | None = None,
    ) -> int:
        """Record (or refresh) one entity's metadata. Returns the row id.

        Refresh REPLACES, including with nulls. Unlike a label — where a second
        source's claim is corroboration and gets its own row — one operator has
        one set of filing facts, and a re-record is a correction. Merging the
        new record over the old would let a field that a source has since
        removed (a request channel that no longer exists, say) survive
        indefinitely because nothing ever restated it.
        """
        insert = pg_insert(VaspMetadataRow).values(
            entity=entity,
            jurisdiction=jurisdiction,
            legal_entity=legal_entity,
            kyc_regime=kyc_regime,
            kyc_since=kyc_since,
            le_request_channel=le_request_channel,
            source=source,
            source_date=source_date,
        )
        stmt = insert.on_conflict_do_update(
            constraint="uq_vasp_metadata_entity",
            set_={
                "jurisdiction": insert.excluded.jurisdiction,
                "legal_entity": insert.excluded.legal_entity,
                "kyc_regime": insert.excluded.kyc_regime,
                "kyc_since": insert.excluded.kyc_since,
                "le_request_channel": insert.excluded.le_request_channel,
                "source": insert.excluded.source,
                "source_date": insert.excluded.source_date,
            },
        ).returning(VaspMetadataRow.id)
        return int((await self._session.execute(stmt)).scalar_one())

    async def for_entity(self, entity: str) -> StoredVaspMetadata | None:
        """The filing facts for the entity a label names, or None.

        ``entity`` is the join key to :class:`LabelRow`'s own ``entity`` — the
        stem, without any role qualifier a label may carry.
        """
        row = (
            await self._session.execute(
                select(VaspMetadataRow).where(VaspMetadataRow.entity == entity)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return StoredVaspMetadata(
            entity=row.entity,
            jurisdiction=row.jurisdiction,
            legal_entity=row.legal_entity,
            kyc_regime=row.kyc_regime,
            kyc_since=row.kyc_since,
            le_request_channel=row.le_request_channel,
            source=row.source,
            source_date=row.source_date,
        )


@dataclass(frozen=True, slots=True)
class StoredApiKey:
    """Read model for one credential. Carries the hash, never a key.

    The digest is here because verification needs it and nothing else does. It
    must not travel any further: the auth layer maps this into its own listing
    model, which has no digest field at all, so an admin listing — printed,
    logged, mailed to whoever asked who has access — cannot carry credential
    material by accident.
    """

    id: int
    key_id: str
    key_hash: str
    scopes: str
    label: str | None
    created_at: datetime
    revoked_at: datetime | None

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None


class ApiKeyRepository:
    """Credentials for the write surface: create, resolve, list, revoke.

    Hashing and scope grammar belong to the auth layer, not here — this
    repository never sees a key, only the id and the digest of one.

    It is also the ONLY implementation of the ``api_keys`` table. ``api/auth.py``
    carried a second one for a while (its own INSERT/UPDATE/SELECT over these
    rows), and two implementations of a credential store is a security bug with
    a delay fuse: the day one of them learns something the other does not — that
    a revoked row must never authenticate, that revocation must not move an
    existing timestamp — the drift *is* the vulnerability, and it is invisible
    because both halves still pass their own tests. The auth layer now composes
    these methods instead.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self, *, key_id: str, key_hash: str, scopes: str, label: str | None = None
    ) -> int:
        """Store a new credential. Returns the row id.

        No upsert: a duplicate ``key_id`` raises rather than replacing the hash,
        because silently rebinding an id to a new secret would revoke the old
        key with no revocation recorded, and every log line already written
        against that id would then name the wrong holder.
        """
        row = ApiKeyRow(key_id=key_id, key_hash=key_hash, scopes=scopes, label=label)
        self._session.add(row)
        await self._session.flush()
        return row.id

    async def by_key_id(self, key_id: str) -> StoredApiKey | None:
        """Resolve the public half of a credential — revoked keys INCLUDED.

        Filtering revoked rows out here would leave the caller unable to
        distinguish a revoked key from an unknown one. Both are refused, but
        only one of them is a security event worth alerting on, and that
        judgement belongs to the auth layer.
        """
        row = (
            await self._session.execute(select(ApiKeyRow).where(ApiKeyRow.key_id == key_id))
        ).scalar_one_or_none()
        if row is None:
            return None
        return StoredApiKey(
            id=row.id,
            key_id=row.key_id,
            key_hash=row.key_hash,
            scopes=row.scopes,
            label=row.label,
            created_at=row.created_at,
            revoked_at=row.revoked_at,
        )

    async def list_keys(self) -> list[StoredApiKey]:
        """Every issued credential, revoked ones INCLUDED.

        An audit needs the dead keys too: "which key authorized this
        investigation" is asked after the incident, by which time the key that
        did it has usually been revoked. Ordered by creation so two operators
        reading the listing on different hosts read the same list.
        """
        rows = (
            await self._session.execute(
                select(ApiKeyRow).order_by(ApiKeyRow.created_at, ApiKeyRow.key_id)
            )
        ).scalars()
        return [
            StoredApiKey(
                id=row.id,
                key_id=row.key_id,
                key_hash=row.key_hash,
                scopes=row.scopes,
                label=row.label,
                created_at=row.created_at,
                revoked_at=row.revoked_at,
            )
            for row in rows
        ]

    async def revoke(self, key_id: str, *, at: datetime | None = None) -> bool:
        """Stamp a key revoked. Returns whether THIS call revoked it.

        Guarded by ``revoked_at IS NULL``, so revoking twice does not move the
        timestamp. The first revocation is the moment the key stopped being
        valid, and an incident review reads that moment to decide which
        requests were made with a live credential — a later call overwriting it
        would move the boundary and quietly exonerate the wrong requests.

        The row is never deleted: an investigation authorized by this key must
        stay attributable after the key is gone.
        """
        result = await self._session.execute(
            update(ApiKeyRow)
            .where(ApiKeyRow.key_id == key_id, ApiKeyRow.revoked_at.is_(None))
            .values(revoked_at=at if at is not None else datetime.now(UTC))
            .returning(ApiKeyRow.id)
        )
        return result.scalar_one_or_none() is not None

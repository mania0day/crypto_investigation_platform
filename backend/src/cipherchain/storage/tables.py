"""SQLAlchemy models for the approved schema (STORAGE_SCHEMA.md, frozen).

Column-level invariants that the DB itself can hold (uniqueness, enum
CHECKs, node shape) live here; taxonomy validity is enforced by the core
domain models before rows are built.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Double,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}


class BigAmount(TypeDecorator[int]):
    """NUMERIC(78,0) surfaced as Python int (uint256-safe; floats never)."""

    impl = Numeric(78, 0)
    cache_ok = True

    def process_bind_param(self, value: int | None, dialect: Any) -> int | None:
        return None if value is None else int(value)

    def process_result_value(self, value: Any, dialect: Any) -> int | None:
        return None if value is None else int(value)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# ─────────────────────────── global immutable fact store ───────────────────────────


class AddressRow(Base):
    __tablename__ = "addresses"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    chain: Mapped[str] = mapped_column(Text)
    address: Mapped[str] = mapped_column(Text)

    __table_args__ = (UniqueConstraint("chain", "address", name="uq_addresses_identity"),)


class TransactionRow(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    chain: Mapped[str] = mapped_column(Text)
    tx_hash: Mapped[str] = mapped_column(Text)
    block_number: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    raw_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (UniqueConstraint("chain", "tx_hash", name="uq_transactions_identity"),)


class AssetRow(Base):
    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    chain: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    contract: Mapped[str | None] = mapped_column(Text, nullable=True)
    symbol: Mapped[str] = mapped_column(Text)
    decimals: Mapped[int] = mapped_column(Integer)

    __table_args__ = (
        UniqueConstraint(
            "chain",
            "kind",
            "contract",
            name="uq_assets_identity",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint("kind IN ('native','token')", name="kind_values"),
    )


class MovementRow(Base):
    """One canonical Movement (schema ruling Q2: table mirrors the model)."""

    __tablename__ = "movements"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    transaction_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("transactions.id"))
    from_address_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("addresses.id"), nullable=True
    )
    to_address_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("addresses.id"), nullable=True
    )
    kind: Mapped[str] = mapped_column(Text)
    asset_id: Mapped[int] = mapped_column(Integer, ForeignKey("assets.id"))
    amount: Mapped[int] = mapped_column(BigAmount)
    index_in_tx: Mapped[int] = mapped_column(Integer)
    # Vantage-stable identity within the tx (adapter-supplied). Identity is
    # (transaction_id, dedup_key), NOT the positional index, so re-normalizing
    # the same tx from a different acquisition dedups instead of dropping or
    # double-storing movements (REVIEW_FINDINGS.md #1).
    dedup_key: Mapped[str] = mapped_column(Text)
    # Denormalized from transactions: traversal filters by (address, time)
    # without a join — the engine's hottest query path.
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    provider: Mapped[str] = mapped_column(Text)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload_sha256: Mapped[str] = mapped_column(String(64))
    # The fee price the sender chose, in the chain's smallest unit. Kept for
    # the unique-gas-price mixer heuristic (REACHING_THE_VASP.md §3, heuristic
    # 3): a manually set pre-EIP-1559 gas price repeated on both sides of a
    # pool links a deposit to a withdrawal by EXACT equality, so the value has
    # to survive storage unrounded — hence BigAmount (NUMERIC 78,0) and not
    # BIGINT. Gas price is a uint256 on EVM chains and a value past 2^63 would
    # fail the insert outright, losing the fact we store it to compare.
    #
    # Nullable, and it will stay largely null: UTXO chains have no gas price at
    # all, and rows written before this column existed are backfilled by
    # re-parsing the cached raw payload rather than re-fetched (the raw bytes
    # are already in provider_cache). A null therefore means "not read", never
    # "paid nothing" — the heuristic must skip nulls rather than match them.
    gas_price: Mapped[int | None] = mapped_column(BigAmount, nullable=True)

    __table_args__ = (
        UniqueConstraint("transaction_id", "dedup_key", name="uq_movements_identity"),
        CheckConstraint(
            "kind IN ('native','token','internal','utxo_input','utxo_output')",
            name="kind_values",
        ),
        CheckConstraint("amount >= 0", name="amount_nonnegative"),
        # Same stance as amount, for the same reason and one more. No chain has
        # a negative gas price, so a negative here is a parse bug in the
        # backfill rather than a fact; and because the heuristic that reads this
        # column concludes from EXACT equality, a sign error shared by two
        # re-parses would read as a match and link an unrelated deposit to an
        # unrelated withdrawal. Cheaper to refuse the write than to explain the
        # link later.
        CheckConstraint("gas_price IS NULL OR gas_price >= 0", name="gas_price_nonnegative"),
        Index("ix_movements_from_time", "from_address_id", "timestamp"),
        Index("ix_movements_to_time", "to_address_id", "timestamp"),
    )


class ProviderCacheRow(Base):
    __tablename__ = "provider_cache"

    cache_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    chain: Mapped[str] = mapped_column(Text)
    capability: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload_sha256: Mapped[str] = mapped_column(String(64))
    raw: Mapped[bytes] = mapped_column(LargeBinary)
    payload_json: Mapped[bytes] = mapped_column(LargeBinary)

    __table_args__ = (Index("ix_provider_cache_chain_capability", "chain", "capability"),)


# ─────────────────────────── per-investigation overlay ───────────────────────────


class InvestigationRow(Base):
    __tablename__ = "investigations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    root_address_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("addresses.id"))
    objectives: Mapped[list[str]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text, default="created")
    budgets: Mapped[dict[str, Any]] = mapped_column(JSONB)
    spent: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    engine_version: Mapped[str] = mapped_column(Text)
    ruleset_version: Mapped[str] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "status IN ('created','running','paused','completed','partial','failed')",
            name="status_values",
        ),
    )


class NodeRow(Base):
    """Two-layer graph node AND the checkpointed frontier (resumability)."""

    __tablename__ = "nodes"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    investigation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("investigations.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(Text)
    address_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("addresses.id"), nullable=True
    )
    transaction_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("transactions.id"), nullable=True
    )
    direction: Mapped[str | None] = mapped_column(Text, nullable=True)
    hop_distance: Mapped[int] = mapped_column(Integer)
    value_share: Mapped[int | None] = mapped_column(BigAmount, nullable=True)
    state: Mapped[str] = mapped_column(Text, default="frontier")
    # Did this address have more history than the trace read? Recorded as a
    # durable fact rather than a run-local counter, so a resumed run cannot
    # print a clean answer over a branch that was cut (Ruling 2 + Ruling 4).
    history_truncated: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # The OTHER way an address is read only in part: its history was read in
    # full, but it had more counterparties than the supernode guard follows, so
    # the tail of them was reached and never expanded. NULL means the expansion
    # was not capped; a number is how many were dropped.
    #
    # Durable for the same reason `history_truncated` is: coverage is derived by
    # query so a resumed run reports the gaps the first run had. Kept apart from
    # `history_truncated` because the two say different things to a reader —
    # "we did not read all of this address's transactions" versus "we read them
    # all and then declined to follow most of where the money went".
    counterparties_dropped: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # WHICH acquisition feeds no provider could serve for this address, as the
    # stable ``FeedGap.code`` strings ("feed_unavailable:token_transfers").
    # NULL means every feed the adapter reads answered.
    #
    # Separate from `history_truncated` because that flag is deliberately the
    # union of three different limits and therefore cannot answer the question a
    # reader actually asks here: WHAT is missing. "This address was read only in
    # part" and "the token feed was dead, so an inbound USDT payment from an
    # exchange is absent while every ETH transfer is present" carry completely
    # different consequences for the same trace, and only the second one tells a
    # reader whether the gap could have hidden the answer they are looking for.
    #
    # Per-node, and durable, for the same reason the two columns above are: a
    # resumed run rebuilds its tracker from zero, so a feed lost during the
    # first pass would otherwise be reported as clean coverage by the second.
    # Per-node specifically is what lets the report say the feed was unavailable
    # FOR THIS ADDRESS rather than somewhere in the run.
    feeds_unavailable: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    # WHY a node stopped. Five different situations set state='terminal', and
    # only one of them ("we ran out of depth") is a coverage gap — without this
    # the report cannot tell a closed branch from an answered one.
    terminal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    discovered_reason: Mapped[str] = mapped_column(Text)
    # This node was reached by a mixer-exit heuristic GUESSING which withdrawal
    # belongs to which deposit — not by following a movement. It exists so a
    # speculative branch can never be drawn or reported as a traced one, which
    # is the whole safety property of following a mixer at all: the branch may
    # belong to an unrelated party, and the report has to say so.
    #
    # A column and not a new `state`, deliberately: speculation is orthogonal
    # to the node's lifecycle. A speculative node still moves
    # frontier -> expanded -> terminal, so folding it into `state` would erase
    # the question "is this branch a guess?" the moment the node was expanded —
    # exactly when the report starts asking.
    #
    # Sticky and inherited (REACHING_THE_VASP.md §3): every descendant of a
    # mixer crossing carries it, because no amount of clean tracing after the
    # crossing launders the guess underneath it.
    speculative: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # WHICH heuristic proposed it, e.g. 'mixer-exit-address-match@1'. An
    # inherited-speculative descendant carries its ancestor's basis, so the
    # report can always name the specific guess a branch rests on rather than
    # saying only that one was made. Held to the flag by a CHECK below: a flag
    # nobody can explain is not usable in a document that goes to a regulator.
    speculative_basis: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "investigation_id",
            "kind",
            "address_id",
            "transaction_id",
            # An address reachable BOTH backward and forward occupies two
            # distinct trace positions, one per objective. Without direction in
            # the identity the second objective's trace dies at the first
            # objective's node (REVIEW_FINDINGS.md #4).
            "direction",
            name="uq_nodes_identity",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(
            "(kind = 'address' AND address_id IS NOT NULL AND transaction_id IS NULL)"
            " OR (kind = 'transaction' AND transaction_id IS NOT NULL AND address_id IS NULL)",
            name="kind_shape",
        ),
        # Unchanged by the speculative work: 'speculative' is NOT a state.
        # A guessed node runs the same lifecycle as a traced one, and adding a
        # sixth value here would have made the flag unreadable the moment the
        # node was expanded (see the `speculative` column).
        CheckConstraint(
            "state IN ('frontier','expanded','terminal','excluded','pinned')",
            name="state_values",
        ),
        CheckConstraint(
            "direction IS NULL OR direction IN ('backward','forward')", name="direction_values"
        ),
        # The flag and its explanation stand or fall together, in both
        # directions. A speculative node with no basis is a guess nobody can
        # attribute to a heuristic; a basis on a node NOT marked speculative is
        # worse — it would be drawn as traced while a heuristic actually
        # proposed it. Enforced in the DB because the propagation path (every
        # descendant of a mixer crossing inherits both) is the easy place to
        # carry one field and forget the other.
        CheckConstraint(
            "speculative = (speculative_basis IS NOT NULL)", name="speculative_has_basis"
        ),
        # And the basis has to NAME something. The equivalence above can only
        # test for NULL, and '' is not NULL, so on its own it accepts a node
        # flagged as a guess whose explanation is empty — the precise state the
        # pair exists to make unrepresentable. btrim, because a basis of spaces
        # is an empty one that happens to survive a truth test.
        CheckConstraint(
            "speculative_basis IS NULL OR btrim(speculative_basis) <> ''",
            name="speculative_basis_not_blank",
        ),
        # A cap that dropped nothing is not a cap, and a NEGATIVE one is an
        # arithmetic slip in the engine that would subtract from a coverage
        # total and make a gap read smaller than it was.
        CheckConstraint(
            "counterparties_dropped IS NULL OR counterparties_dropped > 0",
            name="counterparties_dropped_positive",
        ),
        # An empty array is not "no gap": it is a writer that recorded a lost
        # feed and failed to say which, and downstream that counts as an address
        # with a gap whose name nobody can print. NULL is the way to say nothing
        # was lost.
        CheckConstraint(
            "feeds_unavailable IS NULL OR jsonb_array_length(feeds_unavailable) > 0",
            name="feeds_unavailable_non_empty",
        ),
        Index("ix_nodes_frontier", "investigation_id", "state"),
    )


class EdgeRow(Base):
    __tablename__ = "edges"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    investigation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("investigations.id", ondelete="CASCADE")
    )
    src_node_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("nodes.id"))
    dst_node_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("nodes.id"))
    movement_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("movements.id"), nullable=True
    )
    kind: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint(
            "investigation_id",
            "src_node_id",
            "dst_node_id",
            "movement_id",
            name="uq_edges_identity",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint("kind IN ('movement','bridge')", name="kind_values"),
    )


class FindingRow(Base):
    __tablename__ = "findings"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    investigation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("investigations.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(Text)
    subject_address_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("addresses.id"), nullable=True
    )
    direction: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Double)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "kind IN ('vasp_endpoint','sanctioned_address','mixer_interaction',"
            "'bridge_crossing','sweep_pattern','obfuscation_pattern','terminal')",
            name="kind_values",
        ),
    )


class EvidenceRow(Base):
    __tablename__ = "evidence"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    finding_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("findings.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    refs: Mapped[list[str]] = mapped_column(JSONB, default=list)
    source: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heuristic: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Double, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "kind IN ('onchain_fact','heuristic_inference','third_party_claim',"
            "'engine_observation')",
            name="kind_values",
        ),
    )


class LabelRow(Base):
    """One source's claim about one address — the unit of the intel lifecycle.

    A label is a claim, not ground truth (labels/README.md), so the row's
    identity is (chain, address, source): re-harvesting a source updates its
    own claim in place, and corroboration appears as a SECOND source's row —
    never as one row quietly strengthened. Only ``status='active'`` rows may
    ever reach the attributor; pending and retired rows never attribute and
    never name (LABEL_INTELLIGENCE.md §4).

    Addresses arrive already normalized (analysis.attribution.labels
    ``normalize_address``); this layer sits below the module that owns
    normalization and must not import it — the path back through
    ``cipherchain.investigation`` is the circular import broken once already
    (``vasp_findings_with_hops``).
    """

    __tablename__ = "labels"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    chain: Mapped[str] = mapped_column(Text)
    address: Mapped[str] = mapped_column(Text)
    entity: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text, default="unknown", server_default="unknown")
    confidence: Mapped[float] = mapped_column(Double)
    status: Mapped[str] = mapped_column(Text, default="pending", server_default="pending")
    # HOW the claim was verified, which is a different fact from WHO made it:
    # 'signature' survives its source disappearing; 'community' does not.
    method: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    source_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retrieved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Set on promotion: WHICH independent source agreed. A promotion without
    # this recorded is a promotion nobody can audit.
    corroborated_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Community reports only: the API key that submitted it. Every report is
    # attributable (LABEL_INTELLIGENCE.md §6).
    reporter: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("chain", "address", "source", name="uq_labels_claim"),
        CheckConstraint(
            "category IN ('vasp','sanctioned','mixer','infrastructure')",
            name="category_values",
        ),
        CheckConstraint("role IN ('deposit','operational','unknown')", name="role_values"),
        CheckConstraint("status IN ('pending','active','retired')", name="status_values"),
        CheckConstraint(
            "method IN ('signature','first_party_published','licensed_dataset','community')",
            name="method_values",
        ),
        # STRICTLY inside (0, 1), mirroring LabelRecord and Evidence: a claim is
        # never certainty. The DB holds the same line the domain does, so a raw
        # INSERT cannot smuggle in what the constructor rejects.
        CheckConstraint("confidence > 0 AND confidence < 1", name="confidence_open_interval"),
        Index("ix_labels_lookup", "chain", "address"),
        Index("ix_labels_status", "status"),
    )


class LabelEventRow(Base):
    """Append-only audit of the label lifecycle: the UI feed AND the record.

    ``id`` is the cursor (``GET /intel/events?after=``). No ``ondelete`` on the
    FK on purpose: labels retire, they are never deleted, and a cascade would
    let deleting a label silently delete its history — the one thing an audit
    table exists to prevent.
    """

    __tablename__ = "label_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    kind: Mapped[str] = mapped_column(Text)
    label_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("labels.id"))
    reason: Mapped[str] = mapped_column(Text)
    # Who caused it: a harvester source name, or 'report:<key-id>'. Named actor
    # rather than source to avoid colliding with labels.source in queries.
    actor: Mapped[str] = mapped_column(Text)

    __table_args__ = (
        # 'demoted' is the active->pending edge: an earned activation whose
        # basis stopped holding (content changed, method downgraded, or the
        # corroborating source retired). Review proved the lifecycle needs the
        # edge — without it a promotion was a permanent stamp.
        CheckConstraint(
            "kind IN ('added','updated','promoted','demoted','retired')",
            name="kind_values",
        ),
        Index("ix_label_events_label", "label_id"),
    )


class VaspMetadataRow(Base):
    """What turns a NAME into a filing (REACHING_THE_VASP.md §6.2).

    A trace that ends at "Binance" is not yet actionable. Large exchanges run a
    different legal entity per region, and a request sent to the wrong one costs
    an investigator weeks. This table holds the operator facts the request
    itself needs: the legal entity to address it to, the jurisdiction whose
    authority has reach, whether KYC records will exist for the period traced,
    and the channel where an officer actually files.

    Keyed by ``entity`` and not by address, because it describes the OPERATOR,
    not the wallet — one operator owns thousands of labelled addresses and the
    filing facts are identical for all of them. The join to :class:`LabelRow` is
    a plain string comparison on ``entity``; no foreign key is possible, since a
    label's identity is (chain, address, source) and the same entity recurs in
    as many label rows as it has addresses. Note that a label's ``entity`` may
    carry a qualifier ("Acme Exchange (operational address)") — this table
    stores the STEM, and the caller joins on the stem.

    Provenance is mandatory and the description is not, which is the asymmetry
    that keeps this table honest. ``source``/``source_date`` describe how the
    RECORD was obtained and are always knowable by whoever entered it; the
    descriptive fields describe the operator and may genuinely be unknown. An
    entity we know only the name of reports the name alone — a NOT NULL on
    ``jurisdiction`` would have pushed someone into guessing one, and a guessed
    jurisdiction sends the subpoena to the wrong regulator.
    """

    __tablename__ = "vasp_metadata"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    entity: Mapped[str] = mapped_column(Text)
    jurisdiction: Mapped[str | None] = mapped_column(Text, nullable=True)
    legal_entity: Mapped[str | None] = mapped_column(Text, nullable=True)
    kyc_regime: Mapped[str | None] = mapped_column(Text, nullable=True)
    # A DATE, not a year or a prose string, because the question it answers is
    # a comparison: were the transactions we traced inside the period this
    # operator was collecting identity documents? An officer filing for records
    # from before this date is filing for records that do not exist.
    kyc_since: Mapped[date | None] = mapped_column(Date, nullable=True)
    le_request_channel: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(Text)
    # DATE, unlike labels.source_date's timestamp. A label's date is the instant
    # a feed published a claim; this one is the date printed on a document —
    # a terms page, a licence register entry — and storing a day as an instant
    # invents a time nobody read off the source.
    source_date: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        # One row per entity: metadata is a description of an operator, not a
        # claim about an address, so two rows would be a contradiction rather
        # than the corroboration that a second LABEL row represents.
        UniqueConstraint("entity", name="uq_vasp_metadata_entity"),
    )


# ─────────────────────────── operator credentials ───────────────────────────


class ApiKeyRow(Base):
    """A credential for the write surface — the hash, never the key.

    Only ``key_hash`` is stored, so a dump of this table cannot be replayed as
    access. ``key_id`` is the public half the caller presents and the only half
    that may appear in a log line or in ``labels.reporter``.

    Revocation is a nullable timestamp rather than a DELETE or a boolean, for
    two reasons that both come from the audit trail. A key that authorized an
    investigation must stay resolvable forever, or the record of who ran that
    investigation points at a row that no longer exists. And "when did this key
    stop being valid" is precisely what an audit asks after a leak — a boolean
    answers "whether", which is the less useful half.
    """

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    key_id: Mapped[str] = mapped_column(Text)
    key_hash: Mapped[str] = mapped_column(Text)
    # A delimited string, not an array or JSONB: the set of scopes is small and
    # the auth layer owns its grammar. Storage keeps it opaque so that adding a
    # scope never becomes a migration.
    scopes: Mapped[str] = mapped_column(Text)
    # Operator-facing name ("harvester", "case-team-3"). Optional because a key
    # is usable without one; useful because a revocation decision is made by
    # reading this column.
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (UniqueConstraint("key_id", name="uq_api_keys_key_id"),)


class HarvestRunRow(Base):
    """One harvest cycle, recorded so somebody can see whether it is happening.

    The harvester is a process that starts, works, and exits (``harvest.sh``
    argues that case: not a resident loop, and not a thread inside the API, or
    "restart the API" and "skip a harvest" become the same action). That design
    is right and it has one cost — when the API is asked "are the labels being
    refreshed?", there is no running thing to ask. So the cycle writes here
    instead: a row at the start, the same row closed at the end.

    ``finished_at IS NULL`` therefore means *in flight*, which is exactly what
    the dashboard's sync box shows. It also means a cycle that was killed
    mid-run leaves a row that says "running" forever — the same stranding that
    an interrupted investigation suffers. That is not papered over here by a
    heartbeat column; the reader resolves it, because only the reader knows
    what "too long" is (:data:`STALLED_AFTER_SECONDS`). A killed run is a real
    event and the operator should see it named, not silently rewritten.

    ``sources`` is the per-source outcome list, stored as the report produced
    it. Denormalised on purpose: this is an operational log read as a whole
    row, never joined against, and freezing the shape here would turn adding a
    source into a migration.
    """

    __tablename__ = "harvest_runs"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Mirrors the scheduler's exit code, which is the cron mail and so is
    # already the vocabulary an operator has: 0 ok, 1 failed, 3 stale. A run
    # that both failed and went stale is 'failed', for the same reason the
    # exit code prefers 1 — a source that is down is the more urgent of the two.
    status: Mapped[str] = mapped_column(Text, default="running", server_default="running")
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sources: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    # Set when reconcile blew up, or when the process itself raised. Separate
    # from a source's own error, which lives in ``sources``.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Which machine ran it. On a 24/7 server this is noise; the moment somebody
    # also runs a cycle from a laptop it is the first question asked.
    host: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('running','ok','failed','stale')",
            name="status_values",
        ),
        # The reader always wants the newest run, and the "is one in flight"
        # check is a partial scan of the open ones.
        Index("ix_harvest_runs_started", "started_at"),
    )

"""speculative branches, gas price, VASP filing facts, and API keys

Four changes, all of them prerequisites for REACHING_THE_VASP.md's build order
(§8 steps 1, 2b, 5 and 8):

- ``nodes.speculative`` / ``nodes.speculative_basis`` — a node reached by a
  mixer-exit heuristic is a guess about which withdrawal belongs to which
  deposit. Without the flag such a branch is indistinguishable from a traced
  one in the graph, in the frontier order and in the report, which is the one
  outcome that makes following a mixer unsafe to do at all.
- ``movements.gas_price`` — the unique-gas-price heuristic links a deposit to a
  withdrawal by exact equality of a manually set, pre-EIP-1559 gas price. The
  fact store had no gas fields at all. Existing rows are backfilled by
  re-parsing cached raw payloads, so no re-fetch is required.
- ``vasp_metadata`` — a report naming "Binance" does not tell an officer WHICH
  Binance. The legal entity, its jurisdiction and the law-enforcement request
  channel are what turn a name into a filing.
- ``api_keys`` — the write surface (and the resume endpoint behind it) needs
  credentials that can be attributed and revoked without being deletable.

Revision ID: b5c8e2a91f34
Revises: f2a9c6e1b4d7
Create Date: 2026-08-16 10:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b5c8e2a91f34"
down_revision = "f2a9c6e1b4d7"
branch_labels = None
depends_on = None

# NUMERIC(78,0), matching movements.amount: gas price is a uint256 in wei and a
# BIGINT column would reject the very values the heuristic exists to compare.
_GAS_PRICE = sa.Numeric(precision=78, scale=0)


def upgrade() -> None:
    op.add_column("movements", sa.Column("gas_price", _GAS_PRICE, nullable=True))
    # Matches movements.amount's own non-negative CHECK. No chain has a negative
    # gas price, so a negative value is a backfill parse bug, and the heuristic
    # reading this column concludes from exact equality — a sign error repeated
    # across two re-parses would read as a match.
    op.create_check_constraint(
        op.f("ck_movements_gas_price_nonnegative"),
        "movements",
        "gas_price IS NULL OR gas_price >= 0",
    )

    op.add_column(
        "nodes",
        sa.Column("speculative", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column("nodes", sa.Column("speculative_basis", sa.Text(), nullable=True))
    # Existing rows are all (false, NULL), which satisfies the equivalence, so
    # the constraint can be created straight after the columns.
    # op.f() marks the name as final: without it the metadata naming convention
    # is applied a second time, yielding ck_nodes_ck_nodes_speculative_has_basis.
    op.create_check_constraint(
        op.f("ck_nodes_speculative_has_basis"),
        "nodes",
        "speculative = (speculative_basis IS NOT NULL)",
    )
    # The equivalence above can only test for NULL, and '' is not NULL, so
    # alone it accepts a node flagged as a guess with an empty explanation —
    # the exact state the pair exists to make unrepresentable.
    op.create_check_constraint(
        op.f("ck_nodes_speculative_basis_not_blank"),
        "nodes",
        "speculative_basis IS NULL OR btrim(speculative_basis) <> ''",
    )

    op.create_table(
        "vasp_metadata",
        sa.Column("id", sa.Integer(), sa.Identity(always=False), nullable=False),
        sa.Column("entity", sa.Text(), nullable=False),
        # Descriptive fields are nullable on purpose: an entity we know only the
        # name of must be recordable without anyone guessing a jurisdiction.
        sa.Column("jurisdiction", sa.Text(), nullable=True),
        sa.Column("legal_entity", sa.Text(), nullable=True),
        sa.Column("kyc_regime", sa.Text(), nullable=True),
        sa.Column("kyc_since", sa.Date(), nullable=True),
        sa.Column("le_request_channel", sa.Text(), nullable=True),
        # Provenance is not: how the record was obtained is always knowable by
        # whoever entered it, and this carries the evidentiary weight of a label.
        # A DATE, because what it records is the date printed on a document.
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_date", sa.Date(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vasp_metadata")),
        sa.UniqueConstraint("entity", name="uq_vasp_metadata_entity"),
    )

    op.create_table(
        "api_keys",
        sa.Column("id", sa.Integer(), sa.Identity(always=False), nullable=False),
        sa.Column("key_id", sa.Text(), nullable=False),
        sa.Column("key_hash", sa.Text(), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_keys")),
        sa.UniqueConstraint("key_id", name="uq_api_keys_key_id"),
    )


def downgrade() -> None:
    op.drop_table("api_keys")
    op.drop_table("vasp_metadata")

    # Dropping `speculative` where speculative nodes exist does not merely lose
    # a column — it PROMOTES every guessed branch to a traced one, in the graph
    # and in any report generated afterwards. That is the exact failure the flag
    # was added to prevent, so refuse rather than produce it silently. Same
    # stance as d7b3c02f5a19's evidence-kind narrowing.
    conn = op.get_bind()
    speculative = conn.execute(
        sa.text("SELECT count(*) FROM nodes WHERE speculative")
    ).scalar_one()
    if speculative:
        raise RuntimeError(
            f"{speculative} speculative node(s) exist; downgrading would redraw "
            "mixer-derived branches as traced ones"
        )
    # op.f() marks the name as final — see upgrade().
    op.drop_constraint(op.f("ck_nodes_speculative_basis_not_blank"), "nodes", type_="check")
    op.drop_constraint(op.f("ck_nodes_speculative_has_basis"), "nodes", type_="check")
    op.drop_column("nodes", "speculative_basis")
    op.drop_column("nodes", "speculative")

    op.drop_constraint(op.f("ck_movements_gas_price_nonnegative"), "movements", type_="check")
    op.drop_column("movements", "gas_price")

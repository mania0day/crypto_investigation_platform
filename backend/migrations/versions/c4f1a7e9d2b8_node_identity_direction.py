"""node identity includes direction

An address reachable both backward and forward occupies two distinct trace
positions, one per objective. Without ``direction`` in the identity the second
objective's trace terminates at the first objective's node, and the engine
reports "trace exhausted" for an endpoint it already found
(REVIEW_FINDINGS.md #4, NEXT_MILESTONE_DECISIONS.md Ruling 1).

Revision ID: c4f1a7e9d2b8
Revises: a2dba3c20143
Create Date: 2026-08-09
"""

from __future__ import annotations

from alembic import op

revision = "c4f1a7e9d2b8"
down_revision = "a2dba3c20143"
branch_labels = None
depends_on = None

_COLUMNS_WITH_DIRECTION = [
    "investigation_id",
    "kind",
    "address_id",
    "transaction_id",
    "direction",
]
_COLUMNS_WITHOUT_DIRECTION = _COLUMNS_WITH_DIRECTION[:-1]


def upgrade() -> None:
    op.drop_constraint("uq_nodes_identity", "nodes", type_="unique")
    op.create_unique_constraint(
        "uq_nodes_identity",
        "nodes",
        _COLUMNS_WITH_DIRECTION,
        postgresql_nulls_not_distinct=True,
    )


def downgrade() -> None:
    # Narrowing the identity can collide: an address held as both a backward and
    # a forward node has two rows that the old constraint considers one. Drop the
    # later duplicate before restoring it, keeping the first-discovered node.
    op.execute(
        """
        DELETE FROM nodes a
        USING nodes b
        WHERE a.investigation_id = b.investigation_id
          AND a.kind = b.kind
          AND a.address_id IS NOT DISTINCT FROM b.address_id
          AND a.transaction_id IS NOT DISTINCT FROM b.transaction_id
          AND a.id > b.id
        """
    )
    op.drop_constraint("uq_nodes_identity", "nodes", type_="unique")
    op.create_unique_constraint(
        "uq_nodes_identity",
        "nodes",
        _COLUMNS_WITHOUT_DIRECTION,
        postgresql_nulls_not_distinct=True,
    )

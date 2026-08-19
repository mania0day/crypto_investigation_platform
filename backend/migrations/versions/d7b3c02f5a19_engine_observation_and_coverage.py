"""engine_observation evidence kind, and truncated-history as a durable fact

Ruling 4: statements about CipherChain's own run (frontier exhausted, budget spent,
history truncated) stop wearing the ``onchain_fact`` stamp, which is reserved
for what anyone can verify against the chain.

Ruling 2: pagination limits are accepted as real, so the incompleteness they
cause must be recorded per node and queryable — not a run-local counter that a
resumed run would silently lose.

Revision ID: d7b3c02f5a19
Revises: c4f1a7e9d2b8
Create Date: 2026-08-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d7b3c02f5a19"
down_revision = "c4f1a7e9d2b8"
branch_labels = None
depends_on = None

_OLD_EVIDENCE_KINDS = "kind IN ('onchain_fact','heuristic_inference','third_party_claim')"
_NEW_EVIDENCE_KINDS = (
    "kind IN ('onchain_fact','heuristic_inference','third_party_claim','engine_observation')"
)


def upgrade() -> None:
    op.add_column(
        "nodes",
        sa.Column(
            "history_truncated",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column("nodes", sa.Column("terminal_reason", sa.Text(), nullable=True))
    # op.f() marks the name as final: without it the metadata naming convention
    # is applied a second time, yielding ck_evidence_ck_evidence_kind_values.
    op.drop_constraint(op.f("ck_evidence_kind_values"), "evidence", type_="check")
    op.create_check_constraint(op.f("ck_evidence_kind_values"), "evidence", _NEW_EVIDENCE_KINDS)


def downgrade() -> None:
    # Evidence rows are immutable investigation records, so narrowing the kinds
    # cannot silently rewrite them: refuse rather than corrupt the record.
    conn = op.get_bind()
    remaining = conn.execute(
        sa.text("SELECT count(*) FROM evidence WHERE kind = 'engine_observation'")
    ).scalar_one()
    if remaining:
        raise RuntimeError(
            f"{remaining} engine_observation evidence row(s) exist; "
            "downgrading would violate the immutable-record invariant"
        )
    # op.f() marks the name as final: without it the metadata naming convention
    # is applied a second time, yielding ck_evidence_ck_evidence_kind_values.
    op.drop_constraint(op.f("ck_evidence_kind_values"), "evidence", type_="check")
    op.create_check_constraint(op.f("ck_evidence_kind_values"), "evidence", _OLD_EVIDENCE_KINDS)
    op.drop_column("nodes", "terminal_reason")
    op.drop_column("nodes", "history_truncated")

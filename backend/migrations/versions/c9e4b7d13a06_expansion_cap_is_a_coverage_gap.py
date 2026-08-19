"""nodes.counterparties_dropped — the supernode cap becomes a recorded gap

The engine partially expands a high-degree address: it follows the N largest
counterparties by value and abandons the rest. That decision was written down
only in the prose of a finding, so every counter that decides whether a run was
COMPLETE — the engine's own coverage sentence, ``TraversalCoverage.complete``,
the report's coverage figures, the API — read zero and said the trace had read
everything it reached. A run that dropped forty counterparties printed "no
address was left partially read" directly above a caveat card saying "40 were
reached but never explored".

A column and not a run-local counter, for the reason ``history_truncated``
carries the same shape: coverage is derived by query so that a RESUMED run
reports the same gaps the first run did. A counter living only in the tracker
is lost the moment the process stops, which is precisely when a partial run
gets resumed and re-reported.

NULL means "this address's expansion was not capped". A number means "this many
counterparties were reached and never followed" — so both questions a reader
asks (how many addresses were capped, how much was dropped in total) are one
aggregate away, without a second column to keep in step.

Revision ID: c9e4b7d13a06
Revises: b5c8e2a91f34
Create Date: 2026-08-16 21:30:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c9e4b7d13a06"
down_revision = "b5c8e2a91f34"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("nodes", sa.Column("counterparties_dropped", sa.Integer(), nullable=True))
    # A cap that dropped nothing is not a cap, and a NEGATIVE one is an
    # arithmetic bug in the engine that would subtract from a coverage total
    # and make a gap look smaller than it is. Both are unrepresentable.
    # op.f() marks the name as final: without it the metadata naming convention
    # is applied a second time (see b5c8e2a91f34).
    op.create_check_constraint(
        op.f("ck_nodes_counterparties_dropped_positive"),
        "nodes",
        "counterparties_dropped IS NULL OR counterparties_dropped > 0",
    )


def downgrade() -> None:
    # Dropping this column does not merely lose a number: it re-promotes every
    # partially expanded address to a fully expanded one, and every report
    # generated afterwards claims coverage the run never had. Same stance as
    # b5c8e2a91f34's refusal to drop `speculative`.
    conn = op.get_bind()
    capped = conn.execute(
        sa.text("SELECT count(*) FROM nodes WHERE counterparties_dropped IS NOT NULL")
    ).scalar_one()
    if capped:
        raise RuntimeError(
            f"{capped} node(s) record a capped expansion; downgrading would report "
            "those partial expansions as complete coverage"
        )
    op.drop_constraint(
        op.f("ck_nodes_counterparties_dropped_positive"), "nodes", type_="check"
    )
    op.drop_column("nodes", "counterparties_dropped")

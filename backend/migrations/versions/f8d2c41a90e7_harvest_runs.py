"""harvest_runs — the sync cycle leaves a record of itself

The harvester is a process that starts, works and exits, by an argued design
decision (``scripts/harvest.sh``: not a resident loop, and not a thread inside
the API, or "restart the API" and "skip a harvest" become the same action).

That leaves the API with nothing to ask when a dashboard wants to show whether
labels are being refreshed. Deriving it from ``labels.retrieved_at`` was the
tempting shortcut and it is wrong in the exact case that matters: a cycle whose
every source failed touches no label at all, so the newest ``retrieved_at``
still reads as yesterday's healthy run. Silence and success look identical.

So the cycle records itself. A row is opened before the first source is
contacted and closed after reconcile, which makes ``finished_at IS NULL`` mean
in-flight and makes a total failure a row that exists and says 'failed'.

No heartbeat column. A killed cycle leaves a row saying 'running' forever, and
that is deliberately left for the reader to resolve — see ``harvest/runs.py``.
A heartbeat would let the writer decide what "too long" is, which is a property
of the deployment, and would quietly overwrite the fact that a run was killed.

Revision ID: f8d2c41a90e7
Revises: e3a6f0c81d45
Create Date: 2026-08-19 18:20:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f8d2c41a90e7"
down_revision = "e3a6f0c81d45"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "harvest_runs",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), server_default="running", nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column(
            "sources",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("host", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_harvest_runs")),
        # Mirrors the scheduler's exit codes, which are already the operator's
        # vocabulary via cron mail: 0 ok, 1 failed, 3 stale.
        sa.CheckConstraint(
            "status IN ('running','ok','failed','stale')",
            name=op.f("ck_harvest_runs_status_values"),
        ),
    )
    op.create_index(op.f("ix_harvest_runs_started"), "harvest_runs", ["started_at"])


def downgrade() -> None:
    # Unlike the schema downgrades that refuse (c9e4b7d13a06, e3a6f0c81d45),
    # this one is allowed to drop. Those columns carried facts about coverage
    # that a report would otherwise overstate; this table carries the harvester's
    # own operational log. Losing it costs the sync panel its history and costs
    # no investigation anything — no label, evidence row or finding references it.
    op.drop_index(op.f("ix_harvest_runs_started"), table_name="harvest_runs")
    op.drop_table("harvest_runs")

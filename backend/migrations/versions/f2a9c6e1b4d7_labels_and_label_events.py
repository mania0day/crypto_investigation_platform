"""labels and label_events

The intel lifecycle's storage (LABEL_INTELLIGENCE.md §4): labels move from
startup-loaded files to rows with a status only the domain may advance, plus
an append-only audit of every add / update / promotion / retirement.

Revision ID: f2a9c6e1b4d7
Revises: d7b3c02f5a19
Create Date: 2026-08-11 19:40:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f2a9c6e1b4d7"
down_revision = "d7b3c02f5a19"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "labels",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("chain", sa.Text(), nullable=False),
        sa.Column("address", sa.Text(), nullable=False),
        sa.Column("entity", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), server_default="unknown", nullable=False),
        sa.Column("confidence", sa.Double(), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "retrieved_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("corroborated_by", sa.Text(), nullable=True),
        sa.Column("evidence_url", sa.Text(), nullable=True),
        sa.Column("reporter", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "category IN ('vasp','sanctioned','mixer','infrastructure')",
            name=op.f("ck_labels_category_values"),
        ),
        sa.CheckConstraint(
            "role IN ('deposit','operational','unknown')",
            name=op.f("ck_labels_role_values"),
        ),
        sa.CheckConstraint(
            "status IN ('pending','active','retired')",
            name=op.f("ck_labels_status_values"),
        ),
        sa.CheckConstraint(
            "method IN ('signature','first_party_published','licensed_dataset','community')",
            name=op.f("ck_labels_method_values"),
        ),
        sa.CheckConstraint(
            "confidence > 0 AND confidence < 1",
            name=op.f("ck_labels_confidence_open_interval"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_labels")),
        sa.UniqueConstraint("chain", "address", "source", name="uq_labels_claim"),
    )
    op.create_index("ix_labels_lookup", "labels", ["chain", "address"])
    op.create_index("ix_labels_status", "labels", ["status"])

    op.create_table(
        "label_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("label_id", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('added','updated','promoted','demoted','retired')",
            name=op.f("ck_label_events_kind_values"),
        ),
        # No ondelete: labels retire rather than delete, and audit history must
        # never be deletable as a side effect of anything.
        sa.ForeignKeyConstraint(["label_id"], ["labels.id"], name=op.f("fk_label_events_label_id")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_label_events")),
    )
    op.create_index("ix_label_events_label", "label_events", ["label_id"])


def downgrade() -> None:
    op.drop_index("ix_label_events_label", table_name="label_events")
    op.drop_table("label_events")
    op.drop_index("ix_labels_status", table_name="labels")
    op.drop_index("ix_labels_lookup", table_name="labels")
    op.drop_table("labels")

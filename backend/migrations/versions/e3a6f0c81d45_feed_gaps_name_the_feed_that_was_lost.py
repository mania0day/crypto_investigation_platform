"""nodes.feeds_unavailable — a lost feed names itself

An EVM address is read through three acquisition feeds (native transfers, token
transfers, contract-delivered internal value). Losing one of them is the normal
way this system degrades once the keyed provider quotas are spent: the fallback
tier keeps the trace moving instead of ending it, at the cost of that feed's
rows. ``HistoryPage.gaps`` already carried the loss out of the adapter and the
engine already recorded it — but only by setting ``history_truncated``, which is
deliberately the union of three unrelated limits.

That union is the wrong resolution for this one. "This address was read only in
part" and "the TOKEN feed was dead for this address, so every ETH transfer is
present and an inbound USDT payment from an exchange is not" are read very
differently by someone deciding whether the gap could have hidden their answer —
and the second sentence was unrecoverable from the record. The feed's identity
existed only in a log line, which no report and no API response ever reads.

So the codes are stored per node, as ``FeedGap.code`` strings. Per node, because
"token transfers were unavailable FOR THIS ADDRESS" is the sentence a reader can
act on; a run-level flag would say only that something somewhere was missing.
Durable rather than run-local, for the reason the two neighbouring columns give:
a resumed run rebuilds its tracker from zero and would otherwise print clean
coverage over a first pass that lost a feed.

NULL means every feed answered. Revision ID: e3a6f0c81d45
Revises: c9e4b7d13a06
Create Date: 2026-08-16 22:40:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e3a6f0c81d45"
down_revision = "c9e4b7d13a06"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "nodes",
        sa.Column("feeds_unavailable", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    # An empty array is not "no gap" — it is a writer that recorded a gap and
    # forgot to say which one, which would read downstream as full coverage.
    # Unrepresentable, so the ambiguity cannot reach a report.
    # op.f() marks the name as final: without it the metadata naming convention
    # is applied a second time (see b5c8e2a91f34).
    op.create_check_constraint(
        op.f("ck_nodes_feeds_unavailable_non_empty"),
        "nodes",
        "feeds_unavailable IS NULL OR jsonb_array_length(feeds_unavailable) > 0",
    )


def downgrade() -> None:
    # Same stance as c9e4b7d13a06 and b5c8e2a91f34: dropping this column does
    # not lose a label, it silently re-promotes every address that was read
    # through a dead feed into one that was read in full, and every report
    # generated afterwards claims coverage the run never had.
    conn = op.get_bind()
    missing = conn.execute(
        sa.text("SELECT count(*) FROM nodes WHERE feeds_unavailable IS NOT NULL")
    ).scalar_one()
    if missing:
        raise RuntimeError(
            f"{missing} node(s) record an unavailable acquisition feed; downgrading would "
            "report those partially-read addresses as complete coverage"
        )
    op.drop_constraint(op.f("ck_nodes_feeds_unavailable_non_empty"), "nodes", type_="check")
    op.drop_column("nodes", "feeds_unavailable")

"""Add the outbox claim lease.

Adds the three lease columns a worker sets when it claims a message, plus an index supporting the
"reclaim an expired lease" half of the claim query.

Before this, workers read due rows with a plain SELECT, so two workers would both pick the same
row and both send it. A claim has to be atomic and durable before the outbound call.

No data migration is needed. Existing rows keep NULL lease columns, which is exactly the state of
an unclaimed message. The new 'processing' status value needs no schema change either: status is a
plain VARCHAR(16) with no CHECK constraint, and 'processing' fits.

This migration is reversible: downgrade drops the index and the three columns. Any message that is
'processing' at that moment would be left with a status no downgraded worker claims, so drain the
outbox before downgrading.

Revision ID: 62a497342e27
Revises: c99732eb0a66
Create Date: 2026-09-29 05:04:40.647185
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "62a497342e27"
down_revision: str | None = "c99732eb0a66"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("outbox_messages", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "claimed_by",
                sa.String(length=64),
                nullable=True,
                comment="Worker identifier holding the current lease.",
            )
        )
        batch_op.add_column(sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(
            sa.Column(
                "lease_expires_at",
                sa.DateTime(timezone=True),
                nullable=True,
                comment="After this moment another worker may reclaim the row.",
            )
        )
        batch_op.create_index(
            "ix_outbox_status_lease", ["status", "lease_expires_at"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("outbox_messages", schema=None) as batch_op:
        batch_op.drop_index("ix_outbox_status_lease")
        batch_op.drop_column("lease_expires_at")
        batch_op.drop_column("claimed_at")
        batch_op.drop_column("claimed_by")

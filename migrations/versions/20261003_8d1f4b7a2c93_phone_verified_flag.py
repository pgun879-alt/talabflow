"""Add the "phone number verified" flags.

Adds ``orders.contact_phone_verified`` and ``conversation_states.draft_phone_verified``.

A contact number is now validated against its country's numbering plan, which proves it is a
number that can exist -- not that it belongs to the person who typed it. The only case in which
that is known is when the customer shares their own contact card through the messaging app, and
that fact is worth keeping: staff can tell a number the messaging provider vouched for from one
that was typed.

Both columns are NOT NULL with a server default of false, so every existing row becomes
"not verified". That is the truthful value: before this migration nothing was verified.

Existing phone numbers are left exactly as they were stored. Rewriting them into the new
canonical form would mean guessing a country for every local-format number already in the table,
and a wrong guess silently corrupts a customer's contact number. New orders are stored as E.164;
order search matches both forms.

This migration is reversible: downgrade drops the two columns and loses the flags.

Revision ID: 8d1f4b7a2c93
Revises: 62a497342e27
Create Date: 2026-10-03 09:12:27.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8d1f4b7a2c93"
down_revision: str | None = "62a497342e27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "contact_phone_verified",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
                comment="True only when the customer shared their own contact through the "
                "messaging app, so the number is the one registered to the account that placed "
                "the order.",
            )
        )
    with op.batch_alter_table("conversation_states", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "draft_phone_verified", sa.Boolean(), server_default=sa.false(), nullable=False
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("conversation_states", schema=None) as batch_op:
        batch_op.drop_column("draft_phone_verified")
    with op.batch_alter_table("orders", schema=None) as batch_op:
        batch_op.drop_column("contact_phone_verified")

"""per-account inventory value threshold

Revision ID: 5e2b7d9c4a13
Revises: 3c7a5e9d81f4
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "5e2b7d9c4a13"
down_revision = "3c7a5e9d81f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Zero, so no existing account's analytics screen moves. This column only
    # ever *removes* cards from a reported figure, and doing that to a seller
    # who never asked would look exactly like inventory going missing.
    #
    # `server_default` is not optional on a NOT NULL column added to a table
    # with rows, and autogenerate does not add one. Kept rather than dropped
    # afterwards for the same reason as `price_floor`: this is written by a
    # control a seller may never open, so the database answering for an
    # untouched row is the point.
    op.add_column(
        "users",
        sa.Column("value_threshold", sa.Float(), nullable=False, server_default="0.0"),
    )


def downgrade() -> None:
    op.drop_column("users", "value_threshold")

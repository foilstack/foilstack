"""per-account price floor

Revision ID: 3c7a5e9d81f4
Revises: b4e7c1a92f60
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "3c7a5e9d81f4"
down_revision = "b4e7c1a92f60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The floor used to be a module constant, so every account that exists at
    # this point has been pricing against 0.35 whether it wanted to or not.
    # Backfilling anything else would change what those sellers' exports say
    # without them asking, so the default here is the constant it replaces:
    # nobody's prices move until somebody sets their own.
    #
    # `server_default` is not optional on a NOT NULL column added to a table
    # that already has rows, and autogenerate does not add one. It is kept
    # rather than dropped afterwards because this column is written by a form
    # a seller may never open, so the database being able to answer for a row
    # nobody has touched is the point rather than a migration artefact.
    op.add_column(
        "users",
        sa.Column("price_floor", sa.Float(), nullable=False, server_default="0.35"),
    )


def downgrade() -> None:
    op.drop_column("users", "price_floor")

"""scan size in bytes

Revision ID: 6842dd86917f
Revises: d46268462eb9
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "6842dd86917f"
down_revision = "d46268462eb9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `server_default` is not optional here, and autogenerate does not add it.
    # A NOT NULL column added to a table that already has rows needs a value
    # for those rows, and without one this fails on every deployment that has
    # ever imported anything — which is all of them.
    #
    # Scans imported before this migration therefore report zero bytes and do
    # not count against a quota. Backfilling would mean a migration that stats
    # the filesystem, and a migration that depends on the data directory being
    # mounted is one that breaks when it is run from anywhere else. The
    # undercount is bounded, shrinks as old scans are discarded, and is the
    # cheaper of the two mistakes.
    op.add_column(
        "scans",
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("scans", "size_bytes")

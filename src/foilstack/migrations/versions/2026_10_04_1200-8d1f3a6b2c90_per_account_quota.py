"""per-account storage quota

Revision ID: 8d1f3a6b2c90
Revises: 5e2b7d9c4a13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "8d1f3a6b2c90"
down_revision = "5e2b7d9c4a13"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable, so no server_default: every existing account reads NULL and
    # keeps following FOILSTACK_MAX_ACCOUNT_MB exactly as it did before.
    op.add_column("users", sa.Column("max_account_mb", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "max_account_mb")

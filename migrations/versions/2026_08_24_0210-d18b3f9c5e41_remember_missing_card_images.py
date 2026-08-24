"""remember which card images upstream does not have

Revision ID: d18b3f9c5e41
Revises: c73f8a1d4b62
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d18b3f9c5e41"
down_revision = "c73f8a1d4b62"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable: NULL means "never asked", which is the honest state for every
    # row that exists when this runs. No backfill — the next `embed` fills it
    # in as it goes, and one more run of the behaviour this replaces is a
    # cheaper migration than one that makes 2,900 network requests.
    op.add_column("cards", sa.Column("image_missing_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("cards", "image_missing_at")

"""the card a person picked

Revision ID: c73f8a1d4b62
Revises: b91c4e2f7a10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c73f8a1d4b62"
down_revision = "b91c4e2f7a10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable, so no `server_default` is needed and no backfill happens: a
    # scan nobody has corrected has not chosen anything, and NULL says exactly
    # that. The lesson of `scans.size_bytes` was about NOT NULL columns.
    op.add_column("scans", sa.Column("chosen_card_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_scans_chosen_card_id",
        "scans",
        "cards",
        ["chosen_card_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_scans_chosen_card_id", "scans", type_="foreignkey")
    op.drop_column("scans", "chosen_card_id")

"""same game / same set, and where a batch put a scan

Revision ID: 7f2a91c4d3be
Revises: d18b3f9c5e41
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "7f2a91c4d3be"
down_revision = "d18b3f9c5e41"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NOT NULL on a table that already has rows, so both flags need a
    # `server_default` to get past the existing imports — dropped straight
    # afterwards, because the default belongs to the application and a column
    # default left behind is a second place it could be decided.
    op.add_column(
        "import_jobs",
        sa.Column("same_game", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "import_jobs",
        sa.Column("same_set", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.alter_column("import_jobs", "same_game", server_default=None)
    op.alter_column("import_jobs", "same_set", server_default=None)

    # Nullable, and NULL is the honest value: a job run before this existed
    # settled on no cohort, and a job run with both flags off still does.
    op.add_column("import_jobs", sa.Column("cohort_game", sa.Text(), nullable=True))
    op.add_column("import_jobs", sa.Column("cohort_set", sa.Text(), nullable=True))

    op.add_column("scans", sa.Column("cohort_card_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_scans_cohort_card_id",
        "scans",
        "cards",
        ["cohort_card_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_scans_cohort_card_id", "scans", type_="foreignkey")
    op.drop_column("scans", "cohort_card_id")
    op.drop_column("import_jobs", "cohort_set")
    op.drop_column("import_jobs", "cohort_game")
    op.drop_column("import_jobs", "same_set")
    op.drop_column("import_jobs", "same_game")

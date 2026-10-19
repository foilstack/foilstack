"""a storage label on each import job

Revision ID: 3c7e9a2f5b18
Revises: 8d1f3a6b2c90
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "3c7e9a2f5b18"
down_revision = "8d1f3a6b2c90"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("import_jobs", sa.Column("label", sa.Text(), nullable=True))
    # Existing archives are labelled with their own name, minus `.zip`, because
    # that is what sellers were already using it for — and it is exactly what
    # the import screen now offers a new upload. Loose uploads are left blank:
    # "37 images" or "IMG_0412.jpg" is a name, not a place.
    op.execute(
        "UPDATE import_jobs SET label = NULLIF(regexp_replace(filename, '\\.zip$', '', 'i'), '') "
        "WHERE filename ~* '\\.zip$'"
    )


def downgrade() -> None:
    op.drop_column("import_jobs", "label")

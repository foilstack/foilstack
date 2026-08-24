"""pgvector extension

Its own revision, before any table that uses it. `CREATE EXTENSION` is not
transactional in the way the rest of a migration is on every Postgres build,
and putting it in the same revision as the schema means a partial failure
leaves a database that neither has the extension nor admits it.

The image is `pgvector/pgvector:pg17`, which ships the extension; this only
enables it in *this* database.

Revision ID: 04a130322022
Revises:
"""

from __future__ import annotations

from alembic import op

revision = "04a130322022"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")


def downgrade() -> None:
    # Deliberately not dropped. Anything else in this database that uses a
    # vector column would go with it, and stepping one migration back should
    # not be able to do that.
    pass

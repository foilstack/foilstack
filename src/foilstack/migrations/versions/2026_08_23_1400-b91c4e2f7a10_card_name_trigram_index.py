"""card name trigram index

Revision ID: b91c4e2f7a10
Revises: 6842dd86917f
"""

from __future__ import annotations

from alembic import op

revision = "b91c4e2f7a10"
down_revision = "6842dd86917f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Correcting a bad match means searching the catalogue by name, and the
    # obvious `name ILIKE '%q%'` is a sequential scan: 34ms over 147k cards,
    # and linear in a catalogue that only grows. A trigram GIN index makes that
    # an index scan and keeps the picker usable at half a million rows.
    #
    # `pg_trgm` ships with Postgres itself — it is a contrib module, not a
    # third-party extension, and the image this stack already runs has it. The
    # same is true of the `vector` extension the baseline creates, so a
    # deployment that got this far can create this one.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_cards_name_trgm ON cards USING gin (name gin_trgm_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_cards_name_trgm")
    # The extension is left alone. Something else may have come to depend on
    # it, and dropping a shared extension to undo one index is not a trade a
    # downgrade should make on its own.

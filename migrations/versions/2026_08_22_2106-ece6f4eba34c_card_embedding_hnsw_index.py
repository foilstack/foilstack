"""card embedding hnsw index

Split from the baseline on purpose. The baseline creates the table and no
index, so recall can be measured exactly first; this adds the approximation
once there is a ceiling to judge it against. An HNSW index is a lossy
structure — it trades a little recall for not reading the whole table — and
"a little" is only knowable against the exact answer.

Without it, every scan reads all of `card_embeddings`. That is fine at a few
hundred cards and untenable at a few hundred thousand: 233k vectors at 1024
half-precision dimensions is 477 MB touched per query, against a default
`shared_buffers` of 128 MB. It works until two people scan at once.

BUILD NOTE: a parallel maintenance build maps the *whole* of
`maintenance_work_mem` into dynamic shared memory, so the requirement is
`/dev/shm >= maintenance_work_mem`, not simply "more than Docker's 64 MB
default". Exceeding it is an error, not a fallback. `shm_size: 1gb` is set on
the postgres service in docker-compose.yml; keep the budget below it or drop
the workers to zero.

Revision ID: ece6f4eba34c
Revises: 327c4a89b5f1
"""

from __future__ import annotations

from alembic import op


revision = "ece6f4eba34c"
down_revision = "327c4a89b5f1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Local to this transaction. The server default (64 MB) would spill the
    # graph to disk and turn half a minute into a long wait. 768MB sits under
    # the container's 1 GB /dev/shm so the parallel path stays available —
    # read the BUILD NOTE above before raising either number.
    op.execute("SET maintenance_work_mem = '768MB'")
    op.execute("SET max_parallel_maintenance_workers = 4")
    op.execute(
        """
        CREATE INDEX ix_card_embeddings_hnsw ON card_embeddings
        USING hnsw (embedding halfvec_cosine_ops)
        WITH (m = 16, ef_construction = 128)
        """
    )


def downgrade() -> None:
    # Safe at any time and loses no data: without it the same queries fall back
    # to an exact sequential scan, which is slower and strictly more accurate.
    op.execute("DROP INDEX IF EXISTS ix_card_embeddings_hnsw")

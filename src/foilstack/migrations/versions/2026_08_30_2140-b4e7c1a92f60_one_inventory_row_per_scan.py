"""one inventory row per scan

One row in `inventory` is one physical card, and there is one physical card
behind a scan. Nothing in the schema said so, and `_confirm` inserted without
looking — so a double-click, a retried POST, or the same `scan_id` twice in one
bulk commit turned one photographed card into two rows that counted, priced and
exported as stock the seller did not have.

The application now refuses the replay itself. This index is the half of the
guard that survives concurrency: two requests that both read before either
wrote cannot both insert.

Partial, on non-null `scan_id` only. A row whose scan was deleted keeps
`scan_id` NULL — `SET NULL` on the foreign key — and any number of those may
exist without saying anything about how many cards there are.

The check below refuses the migration rather than choosing which of a pair of
duplicate rows was the real card. That is a question about someone's inventory
and it is not one a migration gets to answer quietly.

Revision ID: b4e7c1a92f60
Revises: 90f5ed7888a8
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b4e7c1a92f60"
down_revision = "90f5ed7888a8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    duplicates = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT scan_id, count(*) AS n FROM inventory "
                "WHERE scan_id IS NOT NULL GROUP BY scan_id HAVING count(*) > 1 "
                "ORDER BY n DESC LIMIT 10"
            )
        )
        .fetchall()
    )
    if duplicates:
        listed = ", ".join(f"scan {row.scan_id} ({row.n} rows)" for row in duplicates)
        raise RuntimeError(
            "inventory already holds more than one row for the same scan: "
            f"{listed}. Each of those is one physical card photographed once. "
            "Delete the extra rows — the card page shows what each is priced "
            "at — and run this migration again."
        )

    op.create_index(
        "uq_inventory_scan_id",
        "inventory",
        ["scan_id"],
        unique=True,
        postgresql_where=sa.text("scan_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_inventory_scan_id", table_name="inventory")

"""Snapshots stored on change by a background pass.

Revision ID: 0005
Revises: 0004

* ``snapshot_pass``: one row per revision a background pass finished (see
  ``ais.snapshots``). Nothing is backfilled: older revisions are computed on demand.
* ``ix_host_snapshot_name_rev`` on ``(host_name, revision)`` for "latest row at or
  before R" per host.
* Removes stored rows identical to the same host's previous row. They were written one
  per host per exported revision; under the new lookup a removed row's revision is simply
  computed on demand (it was never passed), and every later lookup finds the identical
  earlier row instead. On the 100k-host benchmark this is most of ``host_snapshot``.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

# Rows equal to the same host's previous row. Window functions: SQLite >= 3.25, PostgreSQL.
DEDUP_SQL = """
DELETE FROM host_snapshot WHERE id IN (
    SELECT id FROM (
        SELECT id, resolved_hash,
               LAG(resolved_hash) OVER (PARTITION BY host_name ORDER BY revision) AS prev
        FROM host_snapshot
    ) ranked
    WHERE prev = resolved_hash
)
"""


def upgrade() -> None:
    op.create_table(
        "snapshot_pass",
        sa.Column("revision", sa.Integer(), sa.ForeignKey("revision.id", name="fk_snapshot_pass_revision"), primary_key=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hosts_checked", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rows_written", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("seconds", sa.Float(), nullable=False, server_default="0"),
    )
    op.create_index("ix_host_snapshot_name_rev", "host_snapshot", ["host_name", "revision"])
    op.execute(DEDUP_SQL)


def downgrade() -> None:
    op.drop_index("ix_host_snapshot_name_rev", table_name="host_snapshot")
    op.drop_table("snapshot_pass")

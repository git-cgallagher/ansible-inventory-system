"""Node lookup for pickers: trigram indexes on node name and path (PostgreSQL).

Revision ID: 0008
Revises: 0007

``GET /api/v1/nodes?q=`` (``ais.nodesearch``, issue #27) matches ``name`` and ``path`` with
``ILIKE`` prefix and substring patterns. A btree serves neither a case-insensitive nor a
substring match, so on PostgreSQL this adds ``pg_trgm`` (a trusted extension since
PostgreSQL 13, like ``ltree`` in 0002: the database owner may create it) and two GIN indexes,
``ix_node_name_trgm`` and ``ix_node_path_trgm``, over every row (a lookup at a past revision
needs closed rows too). Like ``path_ltree`` they are migration-only, not in the ORM model
(SQLite has no trigram index); ``tests/test_migrations.py`` skips them in the drift check.

On SQLite (development only) this is a no-op: the lookup scans the node table, which is
fine at development sizes.
"""
from __future__ import annotations

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        op.execute("CREATE INDEX ix_node_name_trgm ON node USING GIN (name gin_trgm_ops)")
        op.execute("CREATE INDEX ix_node_path_trgm ON node USING GIN (path gin_trgm_ops)")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_node_path_trgm")
        op.execute("DROP INDEX IF EXISTS ix_node_name_trgm")

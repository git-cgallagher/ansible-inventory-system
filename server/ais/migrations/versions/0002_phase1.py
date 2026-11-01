"""Phase 1: temporal nodes, users, API tokens, host snapshots, ltree index.

Revision ID: 0002
Revises: 0001

* ``node.created_rev`` / ``node.deleted_rev`` make nodes temporal (NULL
  ``created_rev`` = always existed). The unique constraints on ``path`` and
  ``(parent_id, name)`` become partial unique indexes over live rows only, so a
  deleted node can be recreated under the same name.
* ``user_account``, ``api_token``, ``host_snapshot`` are new.
* On PostgreSQL, ``node.path_ltree`` is a stored generated ``ltree`` column
  (``text2ltree(path)``) with a GiST index. The resolver keeps using ``path``
  (text prefix matching), so semantics are unchanged; the column exists for
  ``<@`` subtree queries. Not present on SQLite.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_LIVE = sa.text("deleted_rev IS NULL")
_NAMING = {
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
}


def upgrade() -> None:
    with op.batch_alter_table("node", naming_convention=_NAMING) as b:
        b.drop_constraint("uq_node_parent_name", type_="unique")
        b.drop_constraint("uq_node_path", type_="unique")
        b.add_column(sa.Column("created_rev", sa.Integer, sa.ForeignKey("revision.id", name="fk_node_created_rev"), nullable=True))
        b.add_column(sa.Column("deleted_rev", sa.Integer, sa.ForeignKey("revision.id", name="fk_node_deleted_rev"), nullable=True))
    op.create_index("uq_node_parent_name_live", "node", ["parent_id", "name"], unique=True, sqlite_where=_LIVE, postgresql_where=_LIVE)
    op.create_index("uq_node_path_live", "node", ["path"], unique=True, sqlite_where=_LIVE, postgresql_where=_LIVE)

    op.create_table(
        "user_account",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("display_name", sa.String(255)),
        sa.Column("auth_source", sa.String(16), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("password_hash", sa.String(255)),
        sa.Column("is_active", sa.Boolean, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "api_token",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("user_account.id", ondelete="CASCADE", name="fk_apitoken_user"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("prefix", sa.String(16), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("role", sa.String(16)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("last_used", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "host_snapshot",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("host_node_id", sa.Integer, sa.ForeignKey("node.id", ondelete="CASCADE", name="fk_snapshot_node"), nullable=False),
        sa.Column("host_name", sa.String(255), nullable=False),
        sa.Column("revision", sa.Integer, sa.ForeignKey("revision.id", name="fk_snapshot_revision"), nullable=False),
        sa.Column("resolved", sa.JSON, nullable=False),
        sa.Column("resolved_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("host_node_id", "revision", name="uq_snapshot_host_rev"),
    )
    op.create_index("ix_host_snapshot_host_name", "host_snapshot", ["host_name"])

    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS ltree")
        op.execute("ALTER TABLE node ADD COLUMN path_ltree ltree GENERATED ALWAYS AS (text2ltree(path)) STORED")
        op.execute("CREATE INDEX ix_node_path_ltree ON node USING GIST (path_ltree)")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_node_path_ltree")
        op.execute("ALTER TABLE node DROP COLUMN IF EXISTS path_ltree")
    op.drop_index("ix_host_snapshot_host_name", table_name="host_snapshot")
    op.drop_table("host_snapshot")
    op.drop_table("api_token")
    op.drop_table("user_account")
    op.drop_index("uq_node_path_live", table_name="node")
    op.drop_index("uq_node_parent_name_live", table_name="node")
    with op.batch_alter_table("node", naming_convention=_NAMING) as b:
        b.drop_column("deleted_rev")
        b.drop_column("created_rev")
        b.create_unique_constraint("uq_node_parent_name", ["parent_id", "name"])
        b.create_unique_constraint("uq_node_path", ["path"])

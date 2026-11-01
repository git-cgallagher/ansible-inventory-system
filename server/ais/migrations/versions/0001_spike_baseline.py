"""Phase-0 spike schema (what ``create_all`` produced before Alembic).

Revision ID: 0001
Revises: None
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "revision",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("message", sa.Text),
    )
    op.create_table(
        "node",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("parent_id", sa.Integer, sa.ForeignKey("node.id", ondelete="CASCADE", name="fk_node_parent")),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("inventory_name", sa.String(255)),
        sa.Column("path", sa.Text, nullable=False),
        sa.Column("depth", sa.Integer, nullable=False),
        sa.Column("meta", sa.JSON),
        sa.UniqueConstraint("parent_id", "name", name="uq_node_parent_name"),
        sa.UniqueConstraint("path", name="uq_node_path"),
    )
    op.create_index("ix_node_path_prefix", "node", ["path"])
    op.create_table(
        "variable_definition",
        sa.Column("name", sa.String(255), primary_key=True),
        sa.Column("merge_strategy", sa.String(16), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("json_schema", sa.JSON),
    )
    op.create_table(
        "node_variable",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("node_id", sa.Integer, sa.ForeignKey("node.id", ondelete="CASCADE", name="fk_nodevar_node"), nullable=False),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("value", sa.JSON),
        sa.Column("created_rev", sa.Integer, sa.ForeignKey("revision.id", name="fk_nodevar_created"), nullable=False),
        sa.Column("deleted_rev", sa.Integer, sa.ForeignKey("revision.id", name="fk_nodevar_deleted")),
        sa.CheckConstraint("deleted_rev IS NULL OR deleted_rev > created_rev", name="ck_nodevar_revs"),
    )
    op.create_index("ix_nodevar_node_key_live", "node_variable", ["node_id", "key", "deleted_rev"])
    op.create_table(
        "tag",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(255), nullable=False, unique=True),
        sa.Column("description", sa.Text),
    )
    op.create_table(
        "tag_member",
        sa.Column("tag_id", sa.Integer, sa.ForeignKey("tag.id", ondelete="CASCADE", name="fk_tagmember_tag"), primary_key=True),
        sa.Column("node_id", sa.Integer, sa.ForeignKey("node.id", ondelete="CASCADE", name="fk_tagmember_node"), primary_key=True),
    )


def downgrade() -> None:
    for t in ("tag_member", "tag", "node_variable", "variable_definition", "node", "revision"):
        op.drop_table(t)

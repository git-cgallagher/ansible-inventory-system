"""Profiles: named bundles of variables attached to nodes (issue #22).

Revision ID: 0009
Revises: 0008

* ``profile``: a profile's existence and description, temporal (``created_rev`` /
  ``deleted_rev``), at most one live row per name (``uq_profile_name_live``, partial).
* ``profile_variable``: a profile's variables, temporal like ``node_variable``, keyed by the
  profile's **name** (the ``profile`` row is reopened when its description changes).
* ``profile_attachment``: profile ``profile`` attached to node ``node_id`` at ``position``
  (dense, 0 = lowest precedence), temporal; a node holds a profile at most once while live.
* ``audit_key``: the variables an audit row changed when there are several (``profile.*``
  rows), for ``GET /audit?key=``.

New tables only, so no batch rebuild on SQLite (and no foreign-key hazard of one).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

_LIVE = sa.text("deleted_rev IS NULL")


def _id() -> sa.Column:
    return sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True)


def upgrade() -> None:
    op.create_table(
        "profile",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_rev", sa.Integer(), sa.ForeignKey("revision.id", name="fk_profile_created_rev"), nullable=False),
        sa.Column("deleted_rev", sa.Integer(), sa.ForeignKey("revision.id", name="fk_profile_deleted_rev"), nullable=True),
        sa.CheckConstraint("deleted_rev IS NULL OR deleted_rev > created_rev", name="ck_profile_revs"),
    )
    op.create_index("uq_profile_name_live", "profile", ["name"], unique=True, sqlite_where=_LIVE, postgresql_where=_LIVE)
    op.create_index("ix_profile_name", "profile", ["name", "deleted_rev"])

    op.create_table(
        "profile_variable",
        _id(),
        sa.Column("profile", sa.String(255), nullable=False),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("created_rev", sa.Integer(), sa.ForeignKey("revision.id", name="fk_profvar_created_rev"), nullable=False),
        sa.Column("deleted_rev", sa.Integer(), sa.ForeignKey("revision.id", name="fk_profvar_deleted_rev"), nullable=True),
        sa.CheckConstraint("deleted_rev IS NULL OR deleted_rev > created_rev", name="ck_profvar_revs"),
    )
    op.create_index("ix_profvar_profile_key_live", "profile_variable", ["profile", "key", "deleted_rev"])
    op.create_index("ix_profvar_key_live", "profile_variable", ["key", "deleted_rev"])

    op.create_table(
        "profile_attachment",
        _id(),
        sa.Column("node_id", sa.Integer(), sa.ForeignKey("node.id", ondelete="CASCADE", name="fk_profatt_node"), nullable=False),
        sa.Column("profile", sa.String(255), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_rev", sa.Integer(), sa.ForeignKey("revision.id", name="fk_profatt_created_rev"), nullable=False),
        sa.Column("deleted_rev", sa.Integer(), sa.ForeignKey("revision.id", name="fk_profatt_deleted_rev"), nullable=True),
        sa.CheckConstraint("deleted_rev IS NULL OR deleted_rev > created_rev", name="ck_profatt_revs"),
    )
    op.create_index("uq_profatt_node_profile_live", "profile_attachment", ["node_id", "profile"], unique=True, sqlite_where=_LIVE, postgresql_where=_LIVE)
    op.create_index("ix_profatt_node_live", "profile_attachment", ["node_id", "deleted_rev"])
    op.create_index("ix_profatt_profile_live", "profile_attachment", ["profile", "deleted_rev"])

    op.create_table(
        "audit_key",
        sa.Column(
            "audit_id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            sa.ForeignKey("audit_log.id", ondelete="CASCADE", name="fk_audit_key_audit"),
            primary_key=True,
        ),
        sa.Column("key", sa.String(255), primary_key=True),
    )
    op.create_index("ix_audit_key_key", "audit_key", ["key", "audit_id"])


def downgrade() -> None:
    op.drop_index("ix_audit_key_key", table_name="audit_key")
    op.drop_table("audit_key")
    for name in ("ix_profatt_profile_live", "ix_profatt_node_live", "uq_profatt_node_profile_live"):
        op.drop_index(name, table_name="profile_attachment")
    op.drop_table("profile_attachment")
    for name in ("ix_profvar_key_live", "ix_profvar_profile_key_live"):
        op.drop_index(name, table_name="profile_variable")
    op.drop_table("profile_variable")
    for name in ("ix_profile_name", "uq_profile_name_live"):
        op.drop_index(name, table_name="profile")
    op.drop_table("profile")

"""Phase 2: audit log.

Revision ID: 0003
Revises: 0002

``audit_log`` records one row per effective write or auth event, in the same
transaction as the change (``before``/``after`` are ``jsonb`` on PostgreSQL, ``json``
elsewhere). ``revision`` links inventory writes to their ``Revision``.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(32), nullable=False),
        sa.Column("target_id", sa.String(512), nullable=False),
        sa.Column("before", sa.JSON().with_variant(postgresql.JSONB, "postgresql")),
        sa.Column("after", sa.JSON().with_variant(postgresql.JSONB, "postgresql")),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("request_id", sa.String(64)),
        sa.Column("revision", sa.Integer, sa.ForeignKey("revision.id", name="fk_audit_revision")),
    )
    op.create_index("ix_audit_ts", "audit_log", ["ts"])
    op.create_index("ix_audit_actor", "audit_log", ["actor"])
    op.create_index("ix_audit_action", "audit_log", ["action"])
    op.create_index("ix_audit_target", "audit_log", ["target_type", "target_id"])


def downgrade() -> None:
    op.drop_index("ix_audit_target", table_name="audit_log")
    op.drop_index("ix_audit_action", table_name="audit_log")
    op.drop_index("ix_audit_actor", table_name="audit_log")
    op.drop_index("ix_audit_ts", table_name="audit_log")
    op.drop_table("audit_log")

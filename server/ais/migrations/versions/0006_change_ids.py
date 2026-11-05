"""Change sets and revert.

Revision ID: 0006
Revises: 0005

* ``revision.change`` and ``audit_log.change``: the client's change id (``X-AIS-Change``,
  the modules' ``change`` option, ``ais … --change``), NULL when none was given. Indexed for
  ``GET /revisions?change=`` and ``GET /audit?change=``.
* ``ix_audit_revision`` on ``audit_log.revision``: a revert reads a revision's rows and every
  later revision's rows to find conflicts.

Plain nullable columns without constraints, so ``add_column`` works on SQLite without a
batch rebuild (and therefore without the foreign-key hazard of one).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("revision", sa.Column("change", sa.String(64), nullable=True))
    op.create_index("ix_revision_change", "revision", ["change"])
    op.add_column("audit_log", sa.Column("change", sa.String(64), nullable=True))
    op.create_index("ix_audit_change", "audit_log", ["change"])
    op.create_index("ix_audit_revision", "audit_log", ["revision"])


def downgrade() -> None:
    op.drop_index("ix_audit_revision", table_name="audit_log")
    op.drop_index("ix_audit_change", table_name="audit_log")
    with op.batch_alter_table("audit_log") as b:
        b.drop_column("change")
    op.drop_index("ix_revision_change", table_name="revision")
    with op.batch_alter_table("revision") as b:
        b.drop_column("change")

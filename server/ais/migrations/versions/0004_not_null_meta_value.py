"""Release 0.2.0: ``node.meta`` and ``node_variable.value`` NOT NULL.

Revision ID: 0004
Revises: 0003

The models have always declared both NOT NULL; the spike baseline (``0001``) created them
nullable, which Alembic's autogenerate reported as the only drift. Existing SQL NULLs (none
are written by any code path since phase 0) become ``{}`` and the JSON literal ``null``,
which is what the ORM writes for Python ``None``.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_NAMING = {
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
}


def upgrade() -> None:
    op.execute("UPDATE node SET meta = '{}' WHERE meta IS NULL")
    op.execute("UPDATE node_variable SET value = 'null' WHERE value IS NULL")
    with op.batch_alter_table("node", naming_convention=_NAMING) as b:
        b.alter_column("meta", existing_type=sa.JSON(), nullable=False)
    with op.batch_alter_table("node_variable", naming_convention=_NAMING) as b:
        b.alter_column("value", existing_type=sa.JSON(), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("node_variable", naming_convention=_NAMING) as b:
        b.alter_column("value", existing_type=sa.JSON(), nullable=True)
    with op.batch_alter_table("node", naming_convention=_NAMING) as b:
        b.alter_column("meta", existing_type=sa.JSON(), nullable=True)

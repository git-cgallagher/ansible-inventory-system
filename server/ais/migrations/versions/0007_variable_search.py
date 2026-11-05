"""Variable search: where a key is set, and its audit rows by key alone.

Revision ID: 0007
Revises: 0006

* ``ix_nodevar_key_live`` on ``node_variable (key, deleted_rev)``: ``GET /vars?key=`` finds
  the nodes that set a key without reading every variable. The existing
  ``ix_nodevar_node_key_live`` leads with ``node_id``, so it cannot serve a key-only lookup.
* ``audit_log.key``: the variable a row is about, set by ``audit.record`` for
  ``node_variable`` rows (``target_id`` is ``"<node id>/<key>"``) and ``variable_definition``
  rows (``target_id`` is the name). Indexed with ``id`` for ``GET /audit?key=`` newest first.
  Existing rows are backfilled from ``target_id``; node ids are digits, so the key is
  everything after the first ``/`` (a key may itself contain ``/``).

Plain nullable column and indexes, so no batch rebuild on SQLite (as 0006).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_nodevar_key_live", "node_variable", ["key", "deleted_rev"])
    op.add_column("audit_log", sa.Column("key", sa.String(255), nullable=True))
    op.create_index("ix_audit_key", "audit_log", ["key", "id"])
    if op.get_bind().dialect.name == "postgresql":
        after_slash = "substr(target_id, strpos(target_id, '/') + 1)"
    else:
        after_slash = "substr(target_id, instr(target_id, '/') + 1)"
    op.execute(f"UPDATE audit_log SET key = {after_slash} WHERE target_type = 'node_variable'")
    op.execute("UPDATE audit_log SET key = target_id WHERE target_type = 'variable_definition'")


def downgrade() -> None:
    op.drop_index("ix_audit_key", table_name="audit_log")
    with op.batch_alter_table("audit_log") as b:
        b.drop_column("key")
    op.drop_index("ix_nodevar_key_live", table_name="node_variable")

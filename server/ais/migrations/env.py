"""Alembic environment.

Normally driven programmatically by ``ais.db.init_db`` (which passes an open
connection in ``config.attributes["connection"]``); ``alembic -c server/alembic.ini``
also works and then connects with ``AIS_DATABASE_URL``.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from ais.models import Base

config = context.config
target_metadata = Base.metadata


def _run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


connection = config.attributes.get("connection")
if connection is not None:
    _run(connection)
else:
    from ais.db import database_url

    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = database_url()
    engine = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with engine.connect() as conn:
        _run(conn)
        conn.commit()

"""Database engine, session helpers and schema management.

``path`` is a materialized path stored as text (``all.commercial.prod``) so the same
schema runs on SQLite (tests, local CLI) and PostgreSQL (deployment). On PostgreSQL
the migrations add a generated ``ltree`` copy with a GiST index; the resolver keeps
asking "is this node under that prefix" on the text column, so semantics are the
same on both.

The schema is created and upgraded by the Alembic migrations in ``ais.migrations``
(``init_db``). A database created by the phase-0 spike's ``create_all`` (no
``alembic_version`` table but a ``node`` table) is stamped at the baseline
revision first, then upgraded.
"""

from __future__ import annotations

import os
import pathlib
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session, sessionmaker

DEFAULT_URL = "sqlite:///./ais.db"
BASELINE_REVISION = "0001"


def database_url() -> str:
    return os.environ.get("AIS_DATABASE_URL", DEFAULT_URL)


def make_engine(url: str | None = None) -> Engine:
    url = url or database_url()
    kwargs = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(url, future=True, **kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _fk_on(dbapi_conn, _record):  # pragma: no cover - trivial
            dbapi_conn.execute("PRAGMA foreign_keys=ON")

    return engine


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine, _SessionLocal
    if _engine is None:
        _engine = make_engine()
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def configure(engine: Engine) -> None:
    """Point the module-level session factory at an explicit engine (tests)."""
    global _engine, _SessionLocal
    _engine = engine
    _SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


# ------------------------------------------------------------------ migrations


def alembic_config():
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(pathlib.Path(__file__).with_name("migrations")))
    return cfg


def _is_legacy_spike_db(conn: Connection) -> bool:
    names = set(inspect(conn).get_table_names())
    return "node" in names and "alembic_version" not in names


def init_db(engine: Engine | None = None) -> None:
    """Create or upgrade the schema to the latest migration."""
    from alembic import command

    engine = engine or get_engine()
    cfg = alembic_config()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        if _is_legacy_spike_db(conn):
            command.stamp(cfg, BASELINE_REVISION)
        command.upgrade(cfg, "head")


def schema_revision(engine: Engine | None = None) -> str | None:
    engine = engine or get_engine()
    with engine.connect() as conn:
        if "alembic_version" not in inspect(conn).get_table_names():
            return None
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()


# -------------------------------------------------------------------- sessions


@contextmanager
def session_scope() -> Iterator[Session]:
    get_engine()
    assert _SessionLocal is not None
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as s:
        yield s

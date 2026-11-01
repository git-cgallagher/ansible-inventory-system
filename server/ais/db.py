"""Database engine and session helpers.

The spike uses a materialized path stored as text (``all.commercial.prod``) so the
same schema runs on SQLite (tests, local CLI) and PostgreSQL (deployment). On
PostgreSQL the ``path`` column can later be swapped for ``ltree`` without changing
the resolver, which only ever asks "is this node under that prefix".
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

DEFAULT_URL = "sqlite:///./ais.db"


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


def init_db(engine: Engine | None = None) -> None:
    from ais.models import Base

    Base.metadata.create_all(engine or get_engine())


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

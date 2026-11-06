"""The migrations own the schema: a migrated database matches the models, and ``0004``
upgrades a database that still holds spike-era NULLs."""

from __future__ import annotations

import os

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text

from ais import db as aisdb
from ais.models import Base


def _fresh(tmp_path):
    url = os.environ.get("AIS_TEST_DATABASE_URL") or f"sqlite:///{tmp_path}/mig.db"
    eng = aisdb.make_engine(url)
    with eng.begin() as conn:
        Base.metadata.drop_all(conn)
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
    return eng


def test_migrated_schema_matches_the_models(engine):
    with engine.connect() as conn:
        diffs = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
    # path_ltree (+ its GiST index) and the trigram indexes of 0008 are migration-only on
    # PostgreSQL, on purpose (AGENTS.md).
    diffs = [d for d in diffs if not any(name in repr(d) for name in ("path_ltree", "ix_node_name_trgm", "ix_node_path_trgm"))]
    assert diffs == []


def test_0004_fills_spike_nulls(tmp_path):
    eng = _fresh(tmp_path)
    cfg = aisdb.alembic_config()
    with eng.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0003")
        conn.execute(text("INSERT INTO revision (id, actor, source, ts) VALUES (1, 'spike', 'import', CURRENT_TIMESTAMP)"))
        conn.execute(text("INSERT INTO node (id, parent_id, kind, name, path, depth, meta, created_rev) VALUES (1, NULL, 'all', 'all', 'all', 0, NULL, 1)"))
        conn.execute(text("INSERT INTO node_variable (node_id, key, value, created_rev) VALUES (1, 'x', NULL, 1)"))
    aisdb.init_db(eng)
    with eng.connect() as conn:
        assert aisdb.schema_revision(eng) == _head()
        meta, value = conn.execute(text("SELECT n.meta, v.value FROM node n JOIN node_variable v ON v.node_id = n.id")).one()
    assert meta in ({}, "{}") and value in (None, "null")  # JSON null reads back as None
    eng.dispose()


def _head() -> str:
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(aisdb.alembic_config()).get_current_head()


def test_0005_drops_repeated_snapshot_rows(tmp_path):
    """Old-style rows (one per host per exported revision): 0005 keeps only the rows where
    a host's hash changed, and creates the empty ``snapshot_pass`` table."""
    eng = _fresh(tmp_path)
    cfg = aisdb.alembic_config()
    with eng.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0004")
        for rid in (1, 2, 3):
            conn.execute(text("INSERT INTO revision (id, actor, source, ts) VALUES (:i, 't', 'api', CURRENT_TIMESTAMP)"), {"i": rid})
        conn.execute(text("INSERT INTO node (id, parent_id, kind, name, path, depth, meta, created_rev) VALUES (1, NULL, 'all', 'all', 'all', 0, '{}', 1)"))
        conn.execute(text("INSERT INTO node (id, parent_id, kind, name, path, depth, meta, created_rev) VALUES (2, 1, 'host', 'h1', 'all.h1', 1, '{}', 1)"))
        for rev, digest in ((1, "a"), (2, "a"), (3, "b")):
            conn.execute(
                text("INSERT INTO host_snapshot (host_node_id, host_name, revision, resolved, resolved_hash, created_at) VALUES (2, 'h1', :r, '{}', :h, CURRENT_TIMESTAMP)"),
                {"r": rev, "h": digest},
            )
    aisdb.init_db(eng)
    with eng.connect() as conn:
        assert aisdb.schema_revision(eng) == _head()
        kept = conn.execute(text("SELECT revision FROM host_snapshot ORDER BY revision")).scalars().all()
        passes = conn.execute(text("SELECT count(*) FROM snapshot_pass")).scalar()
    assert kept == [1, 3] and passes == 0
    eng.dispose()

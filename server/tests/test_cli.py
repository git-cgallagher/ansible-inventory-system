"""The ``ais`` CLI's import behaves like ``POST /api/v1/import``: a no-op writes no
revision, ``--dry-run`` writes nothing, and a real import leaves one audit row."""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

from tests.conftest import SEED


def _ais(db: pathlib.Path, *args: str) -> str:
    env = dict(os.environ, AIS_DATABASE_URL=f"sqlite:///{db}", AIS_SNAPSHOTS="off")
    out = subprocess.run([sys.executable, "-m", "ais.cli", *args], capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stdout + out.stderr
    return out.stdout


def _rows(db: pathlib.Path, sql: str) -> list[tuple]:
    import sqlite3

    with sqlite3.connect(db) as con:
        return con.execute(sql).fetchall()


def test_cli_import_is_a_noop_the_second_time_and_audited_once(tmp_path):
    db = tmp_path / "ais.db"
    assert _ais(db, "import", str(SEED), "--dry-run").startswith("dry run: would change")
    assert _rows(db, "SELECT count(*) FROM revision") == [(0,)]
    assert _ais(db, "import", str(SEED)).startswith("imported as revision 1:")
    assert _ais(db, "import", str(SEED)) == "nothing to change (revision 1)\n"
    assert _ais(db, "import", str(SEED), "--dry-run") == "dry run: nothing to change\n"
    assert _rows(db, "SELECT count(*) FROM revision") == [(1,)]
    assert _rows(db, "SELECT actor, action, source, revision FROM audit_log") == [("cli", "import", "cli", 1)]


def test_cli_import_refuses_a_bad_tree_without_writing(tmp_path):
    db = tmp_path / "ais.db"
    _ais(db, "import", str(SEED))
    bad = tmp_path / "bad.yml"
    bad.write_text("all:\n  children:\n    'not ok':\n      vars: {x: 1}\n")
    env = dict(os.environ, AIS_DATABASE_URL=f"sqlite:///{db}", AIS_SNAPSHOTS="off")
    out = subprocess.run([sys.executable, "-m", "ais.cli", "import", str(bad)], capture_output=True, text=True, env=env)
    assert out.returncode != 0 and "import refused: invalid node name" in out.stderr
    assert _rows(db, "SELECT count(*) FROM revision") == [(1,)]

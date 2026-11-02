from __future__ import annotations

import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import threading
import time

import pytest
import uvicorn
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from ais import auth, db as aisdb
from ais.api import app
from ais.importer import import_file
from ais.models import Role

# The background snapshot worker would race the tests' own sessions; tests run passes
# explicitly (``snapshots.run_pass``), and one test drives the worker on purpose.
os.environ.setdefault("AIS_SNAPSHOTS", "off")

ROOT = pathlib.Path(__file__).resolve().parents[2]
SEED = ROOT / "examples" / "seed.yml"
COLLECTION_PATH = ROOT / "collection"


@pytest.fixture(autouse=True)
def _reset_login_limiter():
    """The failed-login limiter is process-global; start every test with a clean slate."""
    from ais.ratelimit import login_limiter

    login_limiter.reset()
    yield
    login_limiter.reset()


@pytest.fixture()
def engine(tmp_path):
    """Per-test database. SQLite by default; set ``AIS_TEST_DATABASE_URL`` to a
    PostgreSQL URL (e.g. the compose stack's) to run the whole suite there: the
    schema is dropped and migrated again for every test."""
    url = os.environ.get("AIS_TEST_DATABASE_URL") or f"sqlite:///{tmp_path}/test.db"
    eng = aisdb.make_engine(url)
    if not url.startswith("sqlite"):
        from sqlalchemy import text

        from ais.models import Base

        with eng.begin() as conn:
            Base.metadata.drop_all(conn)
            conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
    aisdb.configure(eng)
    aisdb.init_db(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine):
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as s:
        yield s
        s.commit()


@pytest.fixture()
def seeded(session):
    rev = import_file(session, SEED)
    session.commit()
    return rev


def _mint(session, email: str, role: Role, password: str | None = None) -> str:
    user = auth.create_user(session, email, role=role, password=password)
    token, _ = auth.create_token(session, user, name="test")
    session.commit()
    return token


@pytest.fixture()
def admin_token(session):
    return _mint(session, "admin@example.com", Role.admin, password="admin-pass-example")


@pytest.fixture()
def editor_token(session):
    return _mint(session, "editor@example.com", Role.editor)


@pytest.fixture()
def viewer_token(session):
    return _mint(session, "viewer@example.com", Role.viewer)


class AuthClient(TestClient):
    """TestClient that sends a bearer token and can carry the If-Match header."""

    def __init__(self, app, token: str | None):
        super().__init__(app)
        self.token = token

    def _with_auth(self, kw: dict) -> dict:
        headers = dict(kw.pop("headers", None) or {})
        if self.token and "Authorization" not in headers:
            headers["Authorization"] = f"Bearer {self.token}"
        if_match = kw.pop("if_match", None)
        if if_match is not None:
            headers["If-Match"] = f'"{if_match}"'
        kw["headers"] = headers
        return kw

    def get(self, url, **kw):  # type: ignore[override]
        return super().get(url, **self._with_auth(kw))

    def post(self, url, **kw):  # type: ignore[override]
        return super().post(url, **self._with_auth(kw))

    def put(self, url, **kw):  # type: ignore[override]
        return super().put(url, **self._with_auth(kw))

    def patch(self, url, **kw):  # type: ignore[override]
        return super().patch(url, **self._with_auth(kw))

    def delete(self, url, **kw):  # type: ignore[override]
        return super().delete(url, **self._with_auth(kw))


@pytest.fixture()
def anon(engine):
    return AuthClient(app, None)


@pytest.fixture()
def admin(engine, admin_token):
    return AuthClient(app, admin_token)


@pytest.fixture()
def editor(engine, editor_token):
    return AuthClient(app, editor_token)


@pytest.fixture()
def viewer(engine, viewer_token):
    return AuthClient(app, viewer_token)


def current_rev(client: AuthClient) -> int:
    revs = client.get("/api/v1/revisions", params={"limit": 1}).json()
    return revs[0]["id"] if revs else 0


# ----------------------------------------------- live server + real plugin


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def server(engine, seeded, viewer_token, tmp_path):
    """Starts the API over the seeded DB; ``viewer_token`` exists before the plugin
    first calls it. The plugin reads the token from ``AIS_TOKEN`` and keeps its
    ETag store under the test's tmp dir."""
    os.environ["AIS_TOKEN"] = viewer_token
    os.environ["AIS_ETAG_STORE"] = str(tmp_path / "etag")
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(timeout=5)
    os.environ.pop("AIS_TOKEN", None)
    os.environ.pop("AIS_ETAG_STORE", None)


@pytest.fixture()
def inventory_file(tmp_path, server):
    inv = tmp_path / "inventory.yml"
    inv.write_text(f"plugin: git_cgallagher.inventory.db\nurl: {server}\n")
    return inv


def _ansible_inventory_bin() -> str:
    """Prefer the interpreter's own bin dir so the venv's ansible-core is used."""
    candidate = pathlib.Path(sys.executable).with_name("ansible-inventory")
    if candidate.exists():
        return str(candidate)
    found = shutil.which("ansible-inventory")
    if not found:
        pytest.skip("ansible-inventory not installed")
    return found


def ansible_inventory(inv, *args):
    # AIS_TOKEN is inherited from os.environ (set by the ``server`` fixture).
    env = dict(os.environ, ANSIBLE_COLLECTIONS_PATH=str(COLLECTION_PATH), ANSIBLE_INVENTORY_UNPARSED_FAILED="1")
    out = subprocess.run(
        [_ansible_inventory_bin(), "-i", str(inv), *args], check=True, capture_output=True, text=True, env=env
    )
    return json.loads(out.stdout)

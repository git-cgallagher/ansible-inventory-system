"""Golden test: the server's render equals what Ansible computes through the plugin."""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import socket
import sys
import subprocess
import threading
import time

import pytest
import uvicorn

from ais.api import app
from ais.resolver import build_export, load_snapshot, resolve
from tests.conftest import COLLECTION_PATH


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def server(engine, seeded):
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
    env = dict(os.environ, ANSIBLE_COLLECTIONS_PATH=str(COLLECTION_PATH), ANSIBLE_INVENTORY_UNPARSED_FAILED="1")
    out = subprocess.run(
        [_ansible_inventory_bin(), "-i", str(inv), *args], check=True, capture_output=True, text=True, env=env
    )
    return json.loads(out.stdout)


def test_every_host_matches_ansible(session, inventory_file):
    snap = load_snapshot(session)
    export = build_export(snap)
    for host in export["hosts"]:
        ours = resolve(snap, host=host)["resolved"]
        theirs = ansible_inventory(inventory_file, "--host", host)
        assert theirs == ours, host


def test_graph_lists_all_hosts(session, inventory_file):
    data = ansible_inventory(inventory_file, "--list")
    hosts = set(data["_meta"]["hostvars"])
    assert hosts == set(build_export(load_snapshot(session))["hosts"])
    assert set(data["tier_prod"]["hosts"]) == {"db01", "fed-edge01", "fed-web01", "web01", "web02"}

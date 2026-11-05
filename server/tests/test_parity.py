"""Golden test: the server's render equals what Ansible computes through the plugin."""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys

from ais.resolver import build_export, load_snapshot, resolve
from tests.conftest import COLLECTION_PATH, ansible_inventory, current_rev


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


def test_playbooks_evaluate_stored_templates_like_the_render(editor, viewer, inventory_file):
    """ansible-core 2.19+ templates an inventory plugin's strings only when the plugin is
    ``trusted_by_default``; without it a playbook saw the raw ``{{ … }}`` while the render
    showed the evaluated value."""
    web = editor.get("/api/v1/nodes/all.commercial.prod.dc1.web").json()["id"]
    editor.put(f"/api/v1/nodes/{web}/vars/listen", json={"value": "{{ ansible_host }}:{{ http_port | int + 1 }}"}, if_match=current_rev(editor))
    rendered = viewer.post("/api/v1/render", json={"host": "web01"}).json()["rendered"]["listen"]
    assert rendered == "10.1.1.11:8081"
    ansible = pathlib.Path(sys.executable).with_name("ansible")
    env = dict(os.environ, ANSIBLE_COLLECTIONS_PATH=str(COLLECTION_PATH))
    out = subprocess.run(
        [str(ansible if ansible.exists() else shutil.which("ansible")), "web01", "-i", str(inventory_file), "-c", "local", "-m", "ansible.builtin.debug", "-a", '{"msg": "{{ listen }}"}'],
        capture_output=True, text=True, env=env,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert json.loads(out.stdout.split("=>", 1)[1])["msg"] == rendered  # "web01 | SUCCESS => {…}"

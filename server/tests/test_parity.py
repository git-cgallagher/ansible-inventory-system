"""Golden test: the server's render equals what Ansible computes through the plugin."""

from __future__ import annotations

from ais.resolver import build_export, load_snapshot, resolve
from tests.conftest import ansible_inventory


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

"""Issue 20: deleting a subtree needs a confirmation, and ``POST /api/v1/nodes/restore``
brings a deleted subtree back with its history.

The restore oracle is the same as the tree export's (``test_tree_export``): the plugin
export (ids aside) and every host's resolved vars equal the pre-delete revision's. On top:
the audit history of a restored node reaches back before the delete (``node_lineage`` reads
the ``node.restore`` row's ``restored`` map), host snapshots carry on by name, and every
refusal writes nothing."""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import pytest
from sqlalchemy import func, select

from ais import snapshots
from ais.models import AuditLog, Node, Revision
from tests.conftest import current_rev
from tests.test_tree_export import EAST, POD, TREE, _plugin_view, _resolved, built  # noqa: F401 (fixture)

DC1 = "all.commercial.prod.dc1"


def _id(client, path: str) -> int:
    return client.get(f"/api/v1/nodes/{path}").json()["id"]


def _audit_count(session) -> int:
    session.expire_all()
    return session.scalar(select(func.count()).select_from(AuditLog)) or 0


def _subtree_paths(session, path: str, revision: int | None = None) -> list[str]:
    from ais.resolver import load_snapshot

    snap = load_snapshot(session, revision, path)
    return sorted(p for p in snap.by_path if p == path or p.startswith(path + "."))


# ------------------------------------------------------------------ delete guard


def test_delete_refuses_a_subtree_without_confirmation(editor, session, seeded):
    dc1 = _id(editor, DC1)
    rev, rows = current_rev(editor), _audit_count(session)
    r = editor.delete(f"/api/v1/nodes/{dc1}", if_match=rev)
    assert r.status_code == 409
    d = r.json()["detail"]
    assert d["error"] == "subtree not empty" and (d["path"], d["nodes"], d["hosts"]) == (DC1, 6, 3)
    assert "5 node(s) below it" in d["message"] and "recursive=true or expect_count=6" in d["message"]
    # a wrong count refuses even with recursive: the tree is not what the caller looked at
    for params in ({"expect_count": 5}, {"expect_count": 7, "recursive": "true"}):
        r = editor.delete(f"/api/v1/nodes/{dc1}", params=params, if_match=rev)
        assert r.status_code == 409 and r.json()["detail"]["error"] == "count mismatch", params
        assert "would delete 6 node(s) (3 host(s))" in r.json()["detail"]["message"]
    assert editor.delete(f"/api/v1/nodes/{dc1}", params={"expect_count": 0}, if_match=rev).status_code == 422
    # nothing written: no revision, no audit row, the subtree still live
    assert current_rev(editor) == rev and _audit_count(session) == rows
    assert editor.get(f"/api/v1/nodes/{DC1}.web.web01").status_code == 200

    r = editor.delete(f"/api/v1/nodes/{dc1}", params={"expect_count": 6}, if_match=rev)
    assert r.status_code == 200
    assert (r.json()["nodes"], r.json()["hosts"], len(r.json()["deleted"])) == (6, 3, 6)


def test_delete_with_recursive_and_leaves(editor, session, seeded):
    rev = current_rev(editor)
    # a host is a leaf: deleted as before, no confirmation
    r = editor.delete(f"/api/v1/nodes/{_id(editor, DC1 + '.web.web01')}", if_match=rev)
    assert r.status_code == 200 and r.json()["deleted"] == [f"{DC1}.web.web01"] and (r.json()["nodes"], r.json()["hosts"]) == (1, 1)
    rev = r.json()["revision"]
    # an empty group is a leaf too
    rev = editor.post("/api/v1/nodes", json={"parent_path": DC1, "name": "empty"}, if_match=rev).json()["revision"]
    r = editor.delete(f"/api/v1/nodes/{_id(editor, DC1 + '.empty')}", if_match=rev)
    assert r.status_code == 200 and (r.json()["nodes"], r.json()["hosts"]) == (1, 0)
    rev = r.json()["revision"]
    # expect_count on a leaf must still be right
    db = _id(editor, DC1 + ".db.db01")
    assert editor.delete(f"/api/v1/nodes/{db}", params={"expect_count": 2}, if_match=rev).status_code == 409
    # recursive alone confirms
    r = editor.delete(f"/api/v1/nodes/{_id(editor, DC1)}", params={"recursive": "true"}, if_match=rev)
    assert r.status_code == 200 and (r.json()["nodes"], r.json()["hosts"]) == (5, 2)
    assert editor.delete(f"/api/v1/nodes/{_id(editor, 'all')}", params={"recursive": "true"}, if_match=r.json()["revision"]).status_code == 422


def test_delete_dry_run_reports_and_writes_nothing(editor, viewer, session, seeded):
    dc1 = _id(editor, DC1)
    rev, rows = current_rev(editor), _audit_count(session)
    # no If-Match needed; the guard applies, so a dry run tells the truth about the real call
    r = editor.delete(f"/api/v1/nodes/{dc1}", params={"dry_run": "true"})
    assert r.status_code == 409 and r.json()["detail"]["nodes"] == 6
    r = editor.delete(f"/api/v1/nodes/{dc1}", params={"dry_run": "true", "recursive": "true"})
    assert r.status_code == 200
    body = r.json()
    assert body["dry_run"] is True and body["changed"] is True and body["revision"] == rev
    assert (body["nodes"], body["hosts"]) == (6, 3) and body["deleted"][0] == DC1 and len(body["deleted"]) == 6
    assert current_rev(editor) == rev and _audit_count(session) == rows
    assert viewer.delete(f"/api/v1/nodes/{dc1}", params={"dry_run": "true", "recursive": "true"}).status_code == 403


# ----------------------------------------------------------------------- restore


def _history(editor, path: str, **params) -> list[tuple[str, str]]:
    rows = editor.get("/api/v1/audit", params={"node": path, "limit": 1000, **params}).json()
    return [(r["action"], r["target_id"]) for r in rows]


def test_restore_brings_back_a_deep_subtree_with_its_history(editor, session, built):  # noqa: F811
    """Depth 8, every merge strategy, tags, meta; resolved vars, plugin export, audit
    history and snapshots all continue across the delete and the restore."""
    rev = built
    pod, east = _id(editor, POD), _id(editor, EAST)
    old_ids = {p: _id(editor, p) for p in _subtree_paths(session, EAST)}
    # history under the old ids: a value on a deep group, a value on the subtree root, meta
    rev = editor.put(f"/api/v1/nodes/{pod}/vars/owner", json={"value": "team-a"}, if_match=rev).json()["revision"]
    rev = editor.put(f"/api/v1/nodes/{pod}/vars/owner", json={"value": "team-b"}, if_match=rev).json()["revision"]
    rev = editor.put(f"/api/v1/nodes/{east}/vars/rack", json={"value": "r12"}, if_match=rev).json()["revision"]
    rev = editor.patch(f"/api/v1/nodes/{pod}", json={"meta": {"ticket": "OPS-1"}}, if_match=rev).json()["revision"]
    R = rev
    session.expire_all()
    snapshots.run_pass(session)
    session.commit()
    before_plugin, before_hosts = _plugin_view(session), _resolved(session)
    pod_history, east_history = _history(editor, POD), _history(editor, EAST)
    owner_history = _history(editor, POD, key="owner")
    assert [a for a, _ in owner_history] == ["variable.set", "variable.set"]
    stored = {h: len(snapshots.list_snapshots(session, h)) for h in before_hosts}

    r = editor.delete(f"/api/v1/nodes/{east}", params={"recursive": "true"}, if_match=R)
    assert r.status_code == 200 and r.json()["nodes"] == len(old_ids) == 8 and r.json()["hosts"] == 3
    D = r.json()["revision"]
    session.expire_all()
    snapshots.run_pass(session)
    session.commit()
    assert snapshots.get_snapshot(session, "web-01", D) is None  # not a host at D: 404 in the API
    assert editor.get("/api/v1/diff", params={"host": "web-01", "from": R, "to": D}).status_code == 404

    # one call, no revision: the last one before the delete
    r = editor.post("/api/v1/nodes/restore", json={"path": EAST}, if_match=D)
    assert r.status_code == 201, r.text
    body = r.json()
    S = body["revision"]
    assert S > D and r.headers["ETag"] == f'"{S}"' and body["changed"] is True and body["dry_run"] is False
    assert (body["path"], body["from_revision"], body["nodes"], body["hosts"]) == (EAST, R, 8, 3)
    assert body["tags"] == ["canary", "patch_wave_1"]
    assert {m["path"]: m["old_id"] for m in body["restored"]} == old_ids
    assert body["node"]["path"] == EAST and body["node"]["vars"]["rack"] == "r12"
    session.expire_all()
    revision = session.get(Revision, S)
    assert (revision.source, revision.message) == ("api", f"restore {EAST} from revision {R}")

    # identical to before the delete: plugin export, every host's resolved vars, tags, meta
    assert _plugin_view(session) == before_plugin
    assert _resolved(session) == before_hosts
    assert editor.get(f"/api/v1/nodes/{POD}").json()["meta"] == {"ticket": "OPS-1"}
    assert f"{POD}.web-01" in editor.get("/api/v1/tags/canary").json()["members"]
    assert f"{EAST}.web-tier" in editor.get("/api/v1/tags/patch_wave_1").json()["members"]

    # history continues: everything from before, plus the delete and the restore at the root
    assert _history(editor, POD)[-len(pod_history):] == pod_history
    assert _history(editor, POD, key="owner") == owner_history
    root_history = _history(editor, EAST)
    assert root_history[:2] == [("node.restore", str(body["node"]["id"])), ("node.delete", str(east))]
    assert root_history[2:] == east_history
    restore_row = editor.get("/api/v1/audit", params={"action": "node.restore"}).json()
    assert len(restore_row) == 1 and restore_row[0]["revision"] == S and restore_row[0]["before"] == {"path": EAST, "id": east, "revision": R}

    # snapshots carry on by host name: the pass after the restore stores nothing new, and the
    # pre-delete revision diffs identical to the restored one
    session.expire_all()
    snapshots.run_pass(session)
    session.commit()
    assert {h: len(snapshots.list_snapshots(session, h)) for h in before_hosts} == stored
    for host in before_hosts:
        assert editor.get("/api/v1/diff", params={"host": host, "from": R, "to": S}).json()["identical"], host

    # lineage chains: move the restored node, delete and restore it again (explicit revision):
    # the first life's history is still there
    rev = editor.post(f"/api/v1/nodes/{_id(editor, POD)}/move", json={"name": "pod-y"}, if_match=S).json()["revision"]
    moved = POD.replace("pod-x", "pod-y")
    assert _history(editor, moved, key="owner") == owner_history
    rev = editor.delete(f"/api/v1/nodes/{_id(editor, EAST)}", params={"expect_count": 8}, if_match=rev).json()["revision"]
    r = editor.post("/api/v1/nodes/restore", json={"path": EAST, "revision": R}, if_match=rev)
    assert r.status_code == 201
    assert _history(editor, POD, key="owner") == owner_history  # the R version has pod-x again
    assert _resolved(session) == before_hosts


def test_restore_refusals_write_nothing(editor, viewer, session, built):  # noqa: F811
    R = built
    east = _id(editor, EAST)

    def refused(body, status, text, if_match):
        rows = _audit_count(session)
        r = editor.post("/api/v1/nodes/restore", json=body, if_match=if_match)
        assert r.status_code == status, (body, r.text)
        assert text in str(r.json()["detail"]), (body, r.text)
        assert current_rev(editor) == if_match and _audit_count(session) == rows

    refused({"path": EAST}, 409, "exists now", R)  # live: a restore never overwrites
    refused({"path": "all"}, 422, "root cannot be restored", R)
    refused({"path": "all.commercial.prod.nowhere"}, 404, "nothing was ever deleted", R)
    D = editor.delete(f"/api/v1/nodes/{east}", params={"recursive": "true"}, if_match=R).json()["revision"]
    refused({"path": EAST, "revision": 99999}, 404, "revision 99999 does not exist", D)
    refused({"path": f"{EAST}.nope", "revision": R}, 404, "did not exist at revision", D)
    assert viewer.post("/api/v1/nodes/restore", json={"path": EAST}, if_match=D).status_code == 403
    assert editor.post("/api/v1/nodes/restore", json={"path": EAST}).status_code == 428
    assert editor.post("/api/v1/nodes/restore", json={"path": EAST}, if_match=R).status_code == 409  # stale

    # a host name now live elsewhere
    rev = editor.post("/api/v1/nodes", json={"parent_path": "all.commercial.prod.dc-west.web", "name": "web-01", "kind": "host"}, if_match=D).json()["revision"]
    refused({"path": EAST}, 409, "web-01 (at all.commercial.prod.dc-west.web.web-01)", rev)
    rev = editor.delete(f"/api/v1/nodes/{_id(editor, 'all.commercial.prod.dc-west.web.web-01')}", if_match=rev).json()["revision"]

    # a definition tightened since the delete rejects an old value: 422, nothing written
    rev = editor.put("/api/v1/variables/http_port", json={"json_schema": {"type": "integer", "maximum": 8000}}, if_match=rev).json()["revision"]
    refused({"path": EAST}, 422, "http_port", rev)
    rev = editor.put("/api/v1/variables/http_port", json={"json_schema": {"type": "integer"}}, if_match=rev).json()["revision"]

    # a group name taken since the delete: the export check's 409
    rev = editor.post("/api/v1/nodes", json={"parent_path": "all.commercial.lab", "name": "x", "inventory_name": "east1"}, if_match=rev).json()["revision"]
    refused({"path": EAST}, 409, "east1", rev)
    rev = editor.delete(f"/api/v1/nodes/{_id(editor, 'all.commercial.lab.x')}", if_match=rev).json()["revision"]

    # the parent is gone too: restore the highest deleted ancestor instead
    prod = _id(editor, "all.commercial.prod")
    rev = editor.delete(f"/api/v1/nodes/{prod}", params={"recursive": "true"}, if_match=rev).json()["revision"]
    refused({"path": EAST, "revision": R}, 409, "restore all.commercial.prod (the highest deleted ancestor)", rev)
    r = editor.post("/api/v1/nodes/restore", json={"path": "all.commercial.prod"}, if_match=rev)
    assert r.status_code == 201 and r.json()["from_revision"] < rev
    # prod came back as it was just before its delete, i.e. without dc-east-1
    assert editor.get(f"/api/v1/nodes/{EAST}").status_code == 404
    assert editor.post("/api/v1/nodes/restore", json={"path": EAST}, if_match=r.json()["revision"]).status_code == 201


def test_restore_dry_run(editor, session, built):  # noqa: F811
    R = built
    D = editor.delete(f"/api/v1/nodes/{_id(editor, EAST)}", params={"recursive": "true"}, if_match=R).json()["revision"]
    rows = _audit_count(session)
    r = editor.post("/api/v1/nodes/restore", json={"path": EAST, "dry_run": True})  # no If-Match needed
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True and body["revision"] == D
    assert (body["from_revision"], body["nodes"], body["hosts"], body["tags"]) == (R, 8, 3, ["canary", "patch_wave_1"])
    assert all("id" not in m for m in body["restored"]) and body["restored"][0]["path"] == EAST
    assert current_rev(editor) == D and _audit_count(session) == rows
    assert editor.get(f"/api/v1/nodes/{EAST}").status_code == 404
    # a dry run refuses what the real call refuses
    assert editor.post("/api/v1/nodes/restore", json={"path": "all.commercial.prod", "dry_run": True}).status_code == 409


def test_restore_a_single_host(editor, session, seeded):
    rev = current_rev(editor)
    web01 = f"{DC1}.web.web01"
    before = editor.post("/api/v1/render", json={"host": "web01", "template": False}).json()["resolved"]
    rev = editor.delete(f"/api/v1/nodes/{_id(editor, web01)}", if_match=rev).json()["revision"]
    r = editor.post("/api/v1/nodes/restore", json={"path": web01}, if_match=rev)
    assert r.status_code == 201 and (r.json()["nodes"], r.json()["hosts"]) == (1, 1)
    after = editor.post("/api/v1/render", json={"host": "web01", "template": False}).json()["resolved"]
    before.pop("inventory_revision"), after.pop("inventory_revision")
    assert after == before


# --------------------------------------------------------------------------- CLI


def _ais(db: pathlib.Path, *args: str, ok: bool = True) -> subprocess.CompletedProcess:
    env = dict(os.environ, AIS_DATABASE_URL=f"sqlite:///{db}", AIS_SNAPSHOTS="off")
    out = subprocess.run([sys.executable, "-m", "ais.cli", *args], capture_output=True, text=True, env=env)
    assert (out.returncode == 0) is ok, out.stdout + out.stderr
    return out


def test_cli_restore(tmp_path):
    import sqlite3

    import yaml

    db = tmp_path / "ais.db"
    tree = tmp_path / "tree.yml"
    tree.write_text(yaml.safe_dump(TREE))
    _ais(db, "import", str(tree))
    with sqlite3.connect(db) as con:  # the CLI has no delete: close the subtree as the API would
        con.execute("INSERT INTO revision (actor, source, ts) VALUES ('t', 'api', CURRENT_TIMESTAMP)")
        con.execute(f"UPDATE node SET deleted_rev = 2 WHERE path = '{EAST}' OR path LIKE '{EAST}.%'")
        con.execute(f"UPDATE node_variable SET deleted_rev = 2 WHERE node_id IN (SELECT id FROM node WHERE deleted_rev = 2)")
    out = _ais(db, "restore", EAST, "--dry-run")
    lines = out.stdout.splitlines()
    assert lines[0] == f"dry run: would restore {EAST} as of revision 1: 8 nodes (3 hosts), 15 variables, 2 tags"
    assert lines[1] == "impact: 3 host(s) affected, 3 added" and len(lines) == 5 and all(": added [" in line for line in lines[2:])
    out = _ais(db, "restore", EAST)
    assert out.stdout.startswith(f"restored {EAST} as of revision 1 as revision 3: 8 nodes")
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT actor, action, source, revision FROM audit_log").fetchall() == [("cli", "import", "cli", 1), ("cli", "node.restore", "cli", 3)]
        assert con.execute("SELECT source FROM revision WHERE id = 3").fetchall() == [("cli",)]
    out = _ais(db, "restore", EAST, ok=False)
    assert "restore refused" in out.stderr and "exists now" in out.stderr

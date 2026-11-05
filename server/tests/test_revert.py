"""Issue 21: change ids and revert.

The round-trip oracle: for every write kind, do it, revert it, and the tree export (own
values, kinds, declarations, tags), the plugin export (ids aside), node ``meta`` and every
host's resolved vars equal what they were before the write. On top: change sets (a header
on many writes, filtered and reverted together), conflicts and ``force``, the dry run,
reverting a revert, the refusals, and the CLI."""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
from typing import Any, Callable

import pytest
from sqlalchemy import func, select

from ais.models import AuditLog, NodeKind, Revision
from ais.resolver import load_snapshot
from ais.tree import export_tree
from tests.conftest import SEED, current_rev
from tests.test_tree_export import EAST, POD, _plugin_view, _resolved, built  # noqa: F401 (fixture)

DC1 = "all.commercial.prod.dc1"
WEST = "all.commercial.prod.dc-west"


def _id(client, path: str) -> int:
    r = client.get(f"/api/v1/nodes/{path}")
    assert r.status_code == 200, (path, r.text)
    return r.json()["id"]


def _state(session) -> dict[str, Any]:
    """Everything a revert must put back, keyed by names and paths (ids change)."""
    session.expire_all()
    snap = load_snapshot(session)
    return {
        "tree": export_tree(session),
        "plugin": _plugin_view(session),
        "resolved": _resolved(session),
        "meta": {n.path: n.meta or {} for n in snap.nodes.values()},
    }


def _revert(client, revision: int, if_match: int, **body) -> Any:
    return client.post(f"/api/v1/revisions/{revision}/revert", json=body, if_match=if_match)


def _rows(session, revision: int) -> list[tuple[str, str]]:
    session.expire_all()
    return [(r.action, r.target_id) for r in session.scalars(select(AuditLog).where(AuditLog.revision == revision).order_by(AuditLog.id))]


# --------------------------------------------------------------- every kind


def _var_new(c, rev):
    return c.put(f"/api/v1/nodes/{_id(c, POD)}/vars/owner", json={"value": "team-a"}, if_match=rev)


def _var_change(c, rev):
    return c.put(f"/api/v1/nodes/{_id(c, EAST)}/vars/dns_servers", json={"value": ["10.9.9.9"]}, if_match=rev)


def _var_unset(c, rev):
    return c.delete(f"/api/v1/nodes/{_id(c, POD)}/vars/audit_rules", if_match=rev)


def _node_create(c, rev):
    return c.post("/api/v1/nodes", json={"parent_path": EAST, "name": "cache", "kind": "group", "vars": {"http_port": 6379}}, if_match=rev)


def _node_patch(c, rev):
    return c.patch(f"/api/v1/nodes/{_id(c, EAST)}", json={"inventory_name": "east_one", "meta": {"owner": "dc-team"}}, if_match=rev)


def _node_move(c, rev):
    return c.post(f"/api/v1/nodes/{_id(c, EAST + '.web-tier')}/move", json={"parent_path": WEST, "name": "web-tier-moved"}, if_match=rev)


def _node_delete(c, rev):
    return c.delete(f"/api/v1/nodes/{_id(c, EAST)}", params={"recursive": "true"}, if_match=rev)


def _def_new(c, rev):
    return c.put("/api/v1/variables/owner_team", json={"merge_strategy": "append", "description": "teams"}, if_match=rev)


def _def_change(c, rev):
    return c.put("/api/v1/variables/sysctl", json={"merge_strategy": "replace"}, if_match=rev)


def _def_delete(c, rev):
    return c.delete("/api/v1/variables/audit_rules", if_match=rev)


def _tag_new(c, rev):
    return c.put("/api/v1/tags/wave_2", json={"description": "second wave", "members": [WEST, f"{POD}.web-02"]}, if_match=rev)


def _tag_change(c, rev):
    return c.put("/api/v1/tags/patch_wave_1", json={"description": "changed", "members": [WEST]}, if_match=rev)


def _tag_delete(c, rev):
    return c.delete("/api/v1/tags/patch_wave_1", if_match=rev)


def _import(c, rev):
    tree = {
        "variables": {"packages": {"merge_strategy": "append"}, "new_decl": {"merge_strategy": "merge"}},
        "all": {
            "vars": {"ntp_server": "ntp.example.com", "brand_new": 1},
            "children": {
                "commercial": {
                    "children": {
                        "prod": {
                            "children": {
                                "dc-west": {
                                    "vars": {"site_code": "W2"},
                                    "children": {"api": {"kind": "group", "vars": {"http_port": 9000}, "hosts": {"west-api-01": {"ansible_host": "10.2.3.1"}}}},
                                }
                            }
                        }
                    }
                }
            },
        },
        "tags": {"canary": {"description": "now described", "members": [f"{WEST}.api.west-api-01"]}, "fresh": {"members": [WEST]}},
    }
    return c.post("/api/v1/import", json={"tree": tree}, if_match=rev)


KINDS: dict[str, Callable] = {
    "variable.set new key": _var_new,
    "variable.set changed value": _var_change,
    "variable.unset": _var_unset,
    "node.create": _node_create,
    "node.update": _node_patch,
    "node.move": _node_move,
    "node.delete": _node_delete,
    "definition.set new": _def_new,
    "definition.set changed": _def_change,
    "definition.delete": _def_delete,
    "tag.set new": _tag_new,
    "tag.set changed": _tag_change,
    "tag.delete": _tag_delete,
    "import": _import,
}


@pytest.mark.parametrize("kind", list(KINDS))
def test_revert_round_trips_every_write_kind(kind, editor, session, built):  # noqa: F811
    rev = built
    before = _state(session)
    r = KINDS[kind](editor, rev)
    assert r.status_code in (200, 201), r.text
    done = r.json()["revision"]
    after_write = _state(session)
    assert after_write != before, "the write must change something the oracle sees"

    r = _revert(editor, done, done)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["reverted"] == [done] and body["operations"] and body["dry_run"] is False
    reverted = body["revision"]
    assert _state(session) == before
    # one revision, ordinary rows for each operation plus the summary row, message says what
    rows = _rows(session, reverted)
    assert rows[-1] == ("revert", str(done)) and len(rows) == len(body["operations"]) + 1
    msg = session.get(Revision, reverted).message
    assert msg.startswith(f"revert revision {done}")

    # reverting the revert redoes the write
    r = _revert(editor, reverted, reverted)
    assert r.status_code == 201, r.text
    assert _state(session) == after_write


def test_revert_of_a_restore_and_history_across_reverts(editor, session, built):  # noqa: F811
    rev = built
    rev = _var_new(editor, rev).json()["revision"]  # history on pod-x before anything else
    rev = _node_delete(editor, rev).json()["revision"]
    after_delete = _state(session)
    r = editor.post("/api/v1/nodes/restore", json={"path": EAST}, if_match=rev)
    assert r.status_code == 201
    restored = r.json()["revision"]
    r = _revert(editor, restored, restored)
    assert r.status_code == 201, r.text
    assert _state(session) == after_delete
    # undo the revert-of-restore: the subtree is back, and its history reaches the first write
    back = r.json()["revision"]
    assert _revert(editor, back, back).status_code == 201
    actions = [row["action"] for row in editor.get("/api/v1/audit", params={"node": POD, "key": "owner"}).json()]
    assert actions == ["variable.set"]
    node_actions = [row["action"] for row in editor.get("/api/v1/audit", params={"node": EAST}).json()]
    assert node_actions[:3] == ["node.restore", "node.delete", "node.restore"]


def test_revert_of_a_move_keeps_the_history(editor, session, built):  # noqa: F811
    rev = built
    rev = _var_new(editor, rev).json()["revision"]
    moved = _node_move(editor, rev).json()["revision"]
    r = _revert(editor, moved, moved)
    assert r.status_code == 201
    op = r.json()["operations"][0]
    assert op["action"] == "node.move" and op["after"]["path"] == f"{EAST}.web-tier" and op["after"]["moved"]
    rows = editor.get("/api/v1/audit", params={"node": POD, "key": "owner"}).json()
    assert [row["action"] for row in rows] == ["variable.set"]  # followed through two moves


# ----------------------------------------------------------------- change sets


def test_change_set_revert_undoes_many_revisions_as_one(editor, session, built):  # noqa: F811
    rev = built
    before = _state(session)
    sre = {"X-AIS-Change": "SRE-1234"}
    rev = editor.post(f"/api/v1/nodes/{_id(editor, WEST + '.web')}/move", json={"name": "frontend"}, headers=sre, if_match=rev).json()["revision"]
    rev = editor.put(f"/api/v1/nodes/{_id(editor, WEST + '.frontend')}/vars/http_port", json={"value": 8088}, headers=sre, if_match=rev).json()[
        "revision"
    ]
    rev = editor.put("/api/v1/tags/frontend", json={"members": [WEST + ".frontend"]}, headers=sre, if_match=rev).json()["revision"]
    rev = editor.post("/api/v1/nodes", json={"parent_path": WEST + ".frontend", "name": "west-web-02", "kind": "host"}, headers=sre, if_match=rev).json()["revision"]
    unrelated = _var_change(editor, rev).json()["revision"]  # not part of the change, touches something else
    after_unrelated = _state(session)

    revs = editor.get("/api/v1/revisions", params={"change": "SRE-1234"}).json()
    assert len(revs) == 4 and {r["change"] for r in revs} == {"SRE-1234"}
    ids = sorted(r["id"] for r in revs)
    audit_rows = editor.get("/api/v1/audit", params={"change": "SRE-1234"}).json()
    assert sorted(r["revision"] for r in audit_rows) == ids and {r["change"] for r in audit_rows} == {"SRE-1234"}
    assert editor.get("/api/v1/revisions", params={"change": "nope"}).json() == []

    # dry run first: what it would do and to which hosts
    r = editor.post("/api/v1/changes/SRE-1234/revert", json={"dry_run": True})
    assert r.status_code == 200, r.text
    dry = r.json()
    assert dry["dry_run"] is True and dry["revision"] == unrelated and dry["reverted"] == ids
    assert [o["reverts"] for o in dry["operations"]] == sorted((o["reverts"] for o in dry["operations"]), reverse=True)
    hosts = {h["host"]: h for h in dry["hosts"]}
    assert set(hosts) == {"west-web-01", "west-web-02"} and hosts["west-web-02"]["status"] == "removed"
    assert hosts["west-web-01"]["status"] == "changed" and "http_port" in hosts["west-web-01"]["removed"]
    assert current_rev(editor) == unrelated and _state(session) == after_unrelated

    r = editor.post("/api/v1/changes/SRE-1234/revert", json={}, headers={"X-AIS-Change": "SRE-1234-rollback"}, if_match=unrelated)
    assert r.status_code == 201, r.text
    new = r.json()["revision"]
    assert [o["summary"] for o in r.json()["operations"]][0].startswith("delete all.commercial.prod.dc-west.frontend.west-web-02")
    revision = session.get(Revision, new)
    assert revision.change == "SRE-1234-rollback" and revision.message == f"revert change SRE-1234 (revisions {', '.join(map(str, ids))})"
    assert _rows(session, new)[-1] == ("revert", "SRE-1234")
    # everything but the unrelated write is as before
    expected = _state(session)
    assert expected["resolved"]["db-01"] == after_unrelated["resolved"]["db-01"]  # unrelated kept
    unrelated_undone = _revert(editor, unrelated, new)
    assert unrelated_undone.status_code == 201
    assert _state(session) == before


# ------------------------------------------------------------------- conflicts


def test_conflicts_refuse_unless_forced(editor, session, built):  # noqa: F811
    rev = built
    first = _var_change(editor, rev).json()["revision"]
    later = editor.put(f"/api/v1/nodes/{_id(editor, EAST)}/vars/dns_servers", json={"value": ["10.8.8.8"]}, if_match=first).json()["revision"]
    rows_before = session.scalar(select(func.count()).select_from(AuditLog))
    r = _revert(editor, first, later)
    assert r.status_code == 409
    d = r.json()["detail"]
    assert d["error"] == "conflict" and len(d["conflicts"]) == 1
    c = d["conflicts"][0]
    assert (c["revision"], c["action"], c["reverting"], c["on"]) == (later, "variable.set", first, f"dns_servers on {EAST}")
    assert "force=true" in d["message"]
    # the dry run tells the same truth; nothing was written
    assert editor.post(f"/api/v1/revisions/{first}/revert", json={"dry_run": True}).status_code == 409
    assert current_rev(editor) == later and session.scalar(select(func.count()).select_from(AuditLog)) == rows_before

    r = _revert(editor, first, later, force=True)
    assert r.status_code == 201, r.text
    assert [c["revision"] for c in r.json()["forced_over"]] == [later]
    assert editor.get(f"/api/v1/nodes/{EAST}").json()["vars"]["dns_servers"] == ["10.1.0.2", "10.1.0.3"]


def test_a_later_move_of_an_ancestor_conflicts(editor, session, built):  # noqa: F811
    rev = built
    first = _var_new(editor, rev).json()["revision"]
    moved = _node_move(editor, first).json()["revision"]
    r = _revert(editor, first, moved)
    assert r.status_code == 409 and r.json()["detail"]["conflicts"][0]["action"] == "node.move"
    # force skips the check, never the impossible: the node is not at that path any more
    r = _revert(editor, first, moved, force=True)
    assert r.status_code == 409 and r.json()["detail"]["error"] == "cannot apply"
    assert f"{POD} does not exist now" in r.json()["detail"]["message"]
    assert current_rev(editor) == moved


def test_force_never_deletes_what_the_reverted_write_did_not_create(editor, session, built):  # noqa: F811
    rev = built
    created = _node_create(editor, rev).json()["revision"]
    child = editor.post("/api/v1/nodes", json={"parent_path": EAST + ".cache", "name": "cache-01", "kind": "host"}, if_match=created).json()["revision"]
    r = _revert(editor, created, child, force=True)
    assert r.status_code == 409 and "cache-01" in r.json()["detail"]["message"]
    assert editor.get(f"/api/v1/nodes/{EAST}.cache.cache-01").status_code == 200


def test_already_reverted(editor, session, built):  # noqa: F811
    rev = built
    done = _var_new(editor, rev).json()["revision"]
    undone = _revert(editor, done, done).json()["revision"]
    r = _revert(editor, done, undone)
    assert r.status_code == 409
    d = r.json()["detail"]
    assert d["error"] == "already reverted" and d["reverted_by"] == [undone]


# -------------------------------------------------------------------- refusals


def test_refusals_permissions_and_locking(editor, viewer, session, built):  # noqa: F811
    rev = built
    done = _var_new(editor, rev).json()["revision"]
    assert viewer.post(f"/api/v1/revisions/{done}/revert", json={}, if_match=done).status_code == 403
    assert viewer.post(f"/api/v1/revisions/{done}/revert", json={"dry_run": True}).status_code == 403
    assert viewer.post("/api/v1/changes/X-1/revert", json={"dry_run": True}).status_code == 403
    assert editor.post(f"/api/v1/revisions/{done}/revert", json={}).status_code == 428
    assert _revert(editor, done, done - 1).status_code == 409  # stale
    assert _revert(editor, 99999, done).status_code == 404
    assert editor.post("/api/v1/changes/NO-SUCH/revert", json={}, if_match=done).status_code == 404
    # the import that built the tree went through the importer directly: no audit row
    r = _revert(editor, built, done)
    assert r.status_code == 422 and "no audit rows" in r.json()["detail"]["message"]
    assert current_rev(editor) == done


def test_old_import_rows_without_before_are_refused(editor, session, built):  # noqa: F811
    rev = _import(editor, built).json()["revision"]
    row = session.scalar(select(AuditLog).where(AuditLog.revision == rev))
    row.before = None  # what an import row written before this release looks like
    session.commit()
    r = _revert(editor, rev, rev)
    assert r.status_code == 422
    msg = r.json()["detail"]["message"]
    assert "recorded before revert existed" in msg and "definitions new_decl, packages" in msg and "tags canary, fresh" in msg


def test_a_schema_tightened_since_refuses_the_old_value(editor, session, built):  # noqa: F811
    rev = built
    done = editor.put(f"/api/v1/nodes/{_id(editor, POD)}/vars/http_port", json={"value": 8000}, if_match=rev).json()["revision"]
    rev = editor.put(f"/api/v1/nodes/{_id(editor, EAST + '.web-tier')}/vars/http_port", json={"value": 8001}, if_match=done).json()["revision"]
    # pod-x had no own http_port: the revert unsets it, fine; now make it set an old value
    tight = editor.put("/api/v1/variables/http_port", json={"json_schema": {"type": "integer", "minimum": 8001}}, if_match=rev)
    assert tight.status_code == 422  # 8000 is live: the schema itself is refused
    changed = editor.put(f"/api/v1/nodes/{_id(editor, POD)}/vars/http_port", json={"value": 8002}, if_match=rev).json()["revision"]
    tight = editor.put("/api/v1/variables/http_port", json={"json_schema": {"type": "integer", "minimum": 8001}}, if_match=changed).json()["revision"]
    r = _revert(editor, changed, tight)  # would put 8000 back
    assert r.status_code == 422 and "json_schema" in r.json()["detail"]["message"]


def test_change_ids_are_validated(editor, session, built):  # noqa: F811
    rev = built
    for bad in ("has space", "x" * 65, "ais_abcdefabcdef", "-leading", "quote\"d", "a/b", "a#b"):
        r = editor.put(f"/api/v1/nodes/{_id(editor, POD)}/vars/owner", json={"value": 1}, headers={"X-AIS-Change": bad}, if_match=rev)
        assert r.status_code == 400, bad
        assert r.json()["detail"]["error"] == "invalid change id"
        assert bad not in r.json()["detail"]["message"]
    r = editor.put(f"/api/v1/nodes/{_id(editor, POD)}/vars/owner", json={"value": 1}, headers={"X-AIS-Change": "CHG0012345:deploy.3"}, if_match=rev)
    assert r.status_code == 200 and session.get(Revision, r.json()["revision"]).change == "CHG0012345:deploy.3"
    # reads with a valid header are fine and record nothing
    assert editor.get("/api/v1/revisions", headers={"X-AIS-Change": "SRE-1"}).status_code == 200


def test_tag_delete_brings_back_memberships_of_deleted_nodes(editor, session, built):  # noqa: F811
    rev = built
    rev = editor.delete(f"/api/v1/nodes/{_id(editor, WEST)}", params={"recursive": "true"}, if_match=rev).json()["revision"]
    gone = editor.delete("/api/v1/tags/west_only", if_match=rev).json()["revision"]
    row = session.scalar(select(AuditLog).where(AuditLog.revision == gone))
    assert [m["path"] for m in row.before["deleted_members"]] == [WEST]
    rev = _revert(editor, gone, gone).json()["revision"]
    rev = editor.post("/api/v1/nodes/restore", json={"path": WEST}, if_match=rev).json()["revision"]
    assert editor.get("/api/v1/tags/west_only").json()["members"] == [WEST]


# ------------------------------------------------------------------------- CLI


def _ais(db: pathlib.Path, *args: str, ok: bool = True) -> subprocess.CompletedProcess:
    env = dict(os.environ, AIS_DATABASE_URL=f"sqlite:///{db}", AIS_SNAPSHOTS="off")
    out = subprocess.run([sys.executable, "-m", "ais.cli", *args], capture_output=True, text=True, env=env)
    assert (out.returncode == 0) == ok, out.stdout + out.stderr
    return out


def _sql(db: pathlib.Path, sql: str) -> list[tuple]:
    import sqlite3

    with sqlite3.connect(db) as con:
        return con.execute(sql).fetchall()


def test_cli_change_ids_and_revert(tmp_path):
    db = tmp_path / "ais.db"
    _ais(db, "import", str(SEED), "--change", "INIT-1")
    extra = tmp_path / "extra.yml"
    extra.write_text("all:\n  vars:\n    ntp_server: ntp2.example.com\n  children:\n    lab2:\n      hosts:\n        lab2-01: {}\n")
    _ais(db, "import", str(extra), "--change", "SRE-7")
    assert _sql(db, "SELECT id, change FROM revision ORDER BY id") == [(1, "INIT-1"), (2, "SRE-7")]
    assert _sql(db, "SELECT revision, change FROM audit_log ORDER BY id") == [(1, "INIT-1"), (2, "SRE-7")]
    assert "invalid change id" in _ais(db, "import", str(extra), "--change", "bad id", ok=False).stderr

    out = _ais(db, "revert", "--of-change", "SRE-7", "--dry-run").stdout
    assert "dry run: reverting change SRE-7 would apply" in out and "lab2-01: removed" in out and "ntp_server" in out
    assert _sql(db, "SELECT count(*) FROM revision") == [(2,)]
    out = _ais(db, "revert", "--of-change", "SRE-7", "--change", "SRE-7-undo").stdout
    assert out.strip().endswith("operation(s)") and "reverted change SRE-7 as revision 3" in out
    assert _sql(db, "SELECT change, source FROM revision WHERE id = 3") == [("SRE-7-undo", "cli")]
    assert ("revert", "change", "SRE-7") in _sql(db, "SELECT action, target_type, target_id FROM audit_log WHERE revision = 3")
    assert "already reverted" in _ais(db, "revert", "--revision", "2", ok=False).stderr
    _ais(db, "revert", "--revision", "3")
    assert _sql(db, "SELECT count(*) FROM node WHERE name = 'lab2-01' AND deleted_rev IS NULL") == [(1,)]

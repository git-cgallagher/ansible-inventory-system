"""Write endpoints: If-Match locking, temporal rows, kind/name/collision rules."""

from __future__ import annotations

from sqlalchemy import select

from ais.models import Node, NodeVariable
from ais.resolver import build_export, load_snapshot, resolve
from tests.conftest import current_rev


def _node_id(client, path):
    return next(n["id"] for n in client.get("/api/v1/nodes").json() if n["path"] == path)


def test_missing_if_match_is_428(editor, seeded):
    r = editor.post("/api/v1/nodes", json={"parent_path": "all", "name": "x"})
    assert r.status_code == 428


def test_stale_if_match_is_409(editor, seeded):
    rev = current_rev(editor)
    r = editor.post("/api/v1/nodes", json={"parent_path": "all.commercial.prod.dc1", "name": "cache"}, if_match=rev)
    assert r.status_code == 201
    assert r.headers["ETag"] == f'"{rev + 1}"'
    assert r.json()["revision"] == rev + 1
    stale = editor.post("/api/v1/nodes", json={"parent_path": "all.commercial.prod.dc1", "name": "queue"}, if_match=rev)
    assert stale.status_code == 409
    assert stale.json()["detail"] == {"error": "stale revision", "expected": rev, "current": rev + 1}
    assert stale.headers["ETag"] == f'"{rev + 1}"'
    assert current_rev(editor) == rev + 1  # the refused write left no revision behind
    assert editor.post("/api/v1/nodes", json={"parent_path": "all", "name": "y"}, headers={"If-Match": "abc"}).status_code == 400


def test_var_put_closes_and_reopens_rows(editor, session, seeded):
    web = _node_id(editor, "all.commercial.prod.dc1.web")
    rev0 = current_rev(editor)
    r = editor.put(f"/api/v1/nodes/{web}/vars/http_port", json={"value": 9090}, if_match=rev0)
    assert r.status_code == 200 and r.json()["changed"] is True
    rev1 = r.json()["revision"]
    rows = session.scalars(select(NodeVariable).where(NodeVariable.node_id == web, NodeVariable.key == "http_port").order_by(NodeVariable.id)).all()
    assert [(x.value, x.created_rev, x.deleted_rev) for x in rows] == [(8080, seeded.id, rev1), (9090, rev1, None)]
    # history is intact: the old revision still resolves the old value
    assert resolve(load_snapshot(session, revision=rev0), host="web01")["resolved"]["http_port"] == 8080
    assert resolve(load_snapshot(session, revision=rev1), host="web01")["resolved"]["http_port"] == 9090
    # idempotent re-put: no new revision, no precondition needed
    again = editor.put(f"/api/v1/nodes/{web}/vars/http_port", json={"value": 9090})
    assert again.status_code == 200 and again.json() == {"revision": rev1, "changed": False, "node_id": web, "key": "http_port", "value": 9090}
    # unset
    r = editor.delete(f"/api/v1/nodes/{web}/vars/http_port", if_match=rev1)
    assert r.status_code == 200
    assert "http_port" not in resolve(load_snapshot(session), host="web01")["resolved"]
    assert editor.delete(f"/api/v1/nodes/{web}/vars/http_port", if_match=r.json()["revision"]).status_code == 404
    assert editor.get(f"/api/v1/nodes/{web}/vars/http_port").status_code == 404


def test_delete_node_closes_subtree_and_keeps_history(editor, session, seeded):
    dc1 = _node_id(editor, "all.commercial.prod.dc1")
    rev0 = current_rev(editor)
    r = editor.delete(f"/api/v1/nodes/{dc1}", if_match=rev0)
    assert r.status_code == 200
    assert sorted(r.json()["deleted"]) == sorted(
        ["all.commercial.prod.dc1", "all.commercial.prod.dc1.web", "all.commercial.prod.dc1.web.web01", "all.commercial.prod.dc1.web.web02", "all.commercial.prod.dc1.db", "all.commercial.prod.dc1.db.db01"]
    )
    now = build_export(load_snapshot(session))
    assert "web01" not in now["hosts"] and "commercial_prod_dc1" not in now["groups"]
    then = build_export(load_snapshot(session, revision=rev0))
    assert then["hosts"]["web02"]["vars"]["packages"] == ["vim", "curl", "awscli", "nginx", "tcpdump"]
    # rows were closed, not deleted
    assert session.scalar(select(Node).where(Node.path == "all.commercial.prod.dc1.web.web01")).deleted_rev == r.json()["revision"]
    assert session.scalar(select(NodeVariable).where(NodeVariable.key == "site_code", NodeVariable.deleted_rev.is_(None))) is not None  # dc2's
    # a node of the same name can be created again
    r2 = editor.post("/api/v1/nodes", json={"parent_path": "all.commercial.prod", "name": "dc1"}, if_match=r.json()["revision"])
    assert r2.status_code == 201
    assert editor.get(f"/api/v1/nodes/{dc1}").json()["deleted_rev"] is not None
    assert editor.delete(f"/api/v1/nodes/{_node_id(editor, 'all')}", if_match=r2.json()["revision"]).status_code == 422


def test_kind_order_and_names_enforced(editor, seeded):
    rev = current_rev(editor)
    r = editor.post("/api/v1/nodes", json={"parent_path": "all.commercial.prod", "name": "oops", "kind": "business"}, if_match=rev)
    assert r.status_code == 422 and "deeper" in r.json()["detail"]
    assert editor.post("/api/v1/nodes", json={"parent_path": "all.commercial.prod.dc1.web.web01", "name": "child"}, if_match=rev).status_code == 422
    assert editor.post("/api/v1/nodes", json={"parent_path": "all", "name": "bad.name"}, if_match=rev).status_code == 422
    assert editor.post("/api/v1/nodes", json={"parent_path": "all", "name": "bad name"}, if_match=rev).status_code == 422
    assert editor.post("/api/v1/nodes", json={"parent_path": "all.commercial", "name": "prod"}, if_match=rev).status_code == 409  # exists
    assert editor.post("/api/v1/nodes", json={"parent_path": "all", "name": "x", "parent_id": 1}, if_match=rev).status_code == 422
    assert current_rev(editor) == rev  # every refusal rolled back


def test_group_name_collision_rejected_at_write_time(editor, seeded):
    rev = current_rev(editor)
    # commercial_prod_dc1_web already exists as a qualified name; an inventory_name override that
    # collides must be refused, as must a tag-group collision (tier_prod).
    dc2 = _node_id(editor, "all.federal.prod.dc2")
    r = editor.patch(f"/api/v1/nodes/{dc2}", json={"inventory_name": "commercial_prod_dc1_web"}, if_match=rev)
    assert r.status_code == 409 and "collision" in r.json()["detail"]
    r = editor.post("/api/v1/nodes", json={"parent_path": "all", "name": "tier_prod", "kind": "business"}, if_match=rev)
    assert r.status_code == 409 and "tag group" in r.json()["detail"]
    assert current_rev(editor) == rev
    ok = editor.patch(f"/api/v1/nodes/{dc2}", json={"inventory_name": "fed_dc2", "meta": {"owner": "ops"}}, if_match=rev)
    assert ok.status_code == 200 and ok.json()["node"]["inventory_name"] == "fed_dc2"
    assert "fed_dc2" in editor.get("/api/v1/export").json()["groups"]


def test_variable_definitions(editor, viewer, session, seeded):
    rev = current_rev(editor)
    r = editor.put("/api/v1/variables/extra_packages", json={"merge_strategy": "unique_append", "description": "x"}, if_match=rev)
    assert r.status_code == 200
    assert viewer.get("/api/v1/variables/extra_packages").json()["merge_strategy"] == "unique_append"
    assert any(d["name"] == "extra_packages" for d in viewer.get("/api/v1/variables").json())
    r = editor.delete("/api/v1/variables/extra_packages", if_match=r.json()["revision"])
    assert r.status_code == 200
    assert viewer.get("/api/v1/variables/extra_packages").status_code == 404


def test_tags_never_carry_vars(editor, session, seeded):
    """No route can attach a variable to a tag: the var routes take node ids and the
    tag table has no vars column. Trying the natural URLs fails."""
    from ais.models import Tag

    session.add(Tag(name="canary"))
    session.commit()
    rev = current_rev(editor)
    assert editor.put("/api/v1/tags/canary/vars/x", json={"value": 1}, if_match=rev).status_code in (404, 405)
    assert editor.post("/api/v1/tags", json={"name": "t", "vars": {"x": 1}}, if_match=rev).status_code in (404, 405)
    assert not hasattr(Tag, "variables") and not hasattr(Tag, "vars")
    assert editor.get("/api/v1/export").json()["tags"] == {k: v for k, v in editor.get("/api/v1/export").json()["tags"].items()}  # lists of hosts only
    assert all(isinstance(v, list) for v in editor.get("/api/v1/export").json()["tags"].values())


def test_import_requires_precondition_and_records_actor(editor, viewer, seeded):
    tree = {"all": {"vars": {"ntp_server": "ntp9.example.com"}}}
    assert editor.post("/api/v1/import", json={"tree": tree}).status_code == 428
    rev = current_rev(editor)
    r = editor.post("/api/v1/import", json={"tree": tree, "message": "bump ntp"}, if_match=rev)
    assert r.status_code == 201 and r.json()["revision"] == rev + 1
    latest = viewer.get("/api/v1/revisions", params={"limit": 1}).json()[0]
    assert latest == {**latest, "actor": "editor@example.com", "source": "import", "message": "bump ntp"}
    assert viewer.post("/api/v1/render", json={"host": "web01"}).json()["resolved"]["ntp_server"] == "ntp9.example.com"

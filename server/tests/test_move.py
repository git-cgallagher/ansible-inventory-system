"""``POST /nodes/{id}/move``: subtree path rewrite as new temporal rows, history kept,
collisions / kind / cycle / root refused, vars and tag memberships carried over."""

from __future__ import annotations

from sqlalchemy import select

from ais.models import Node, NodeVariable, Tag, TagMember
from ais.resolver import build_export, load_snapshot, resolve
from tests.conftest import current_rev


def _node(client, path):
    r = client.get(f"/api/v1/nodes/{path}")
    assert r.status_code == 200, r.text
    return r.json()


def test_lookup_by_path_and_id(viewer, seeded):
    web = _node(viewer, "all.commercial.prod.dc1.web")
    assert web["vars"] == {"http_port": 8080, "packages": ["nginx"]}
    assert viewer.get(f"/api/v1/nodes/{web['id']}").json() == web
    assert viewer.get("/api/v1/nodes/all.nope").status_code == 404


def test_move_subtree_rewrites_paths_and_keeps_history(editor, session, seeded):
    web = _node(editor, "all.commercial.prod.dc1.web")
    rev0 = current_rev(editor)
    session.add(Tag(name="canary"))
    session.flush()
    tag = session.scalar(select(Tag).where(Tag.name == "canary"))
    session.add(TagMember(tag_id=tag.id, node_id=web["id"]))
    session.commit()
    before = build_export(load_snapshot(session))
    assert before["tags"]["canary"] == ["web01", "web02"]

    r = editor.post(f"/api/v1/nodes/{web['id']}/move", json={"parent_path": "all.federal.prod.dc2", "name": "www"}, if_match=rev0)
    assert r.status_code == 200, r.text
    body = r.json()
    rev1 = body["revision"]
    assert body["node"]["path"] == "all.federal.prod.dc2.www" and body["node"]["depth"] == 4 and body["node"]["id"] != web["id"]
    assert body["node"]["vars"] == {"http_port": 8080, "packages": ["nginx"]}
    assert [(m["from"], m["to"]) for m in body["moved"]] == [
        ("all.commercial.prod.dc1.web", "all.federal.prod.dc2.www"),
        ("all.commercial.prod.dc1.web.web01", "all.federal.prod.dc2.www.web01"),
        ("all.commercial.prod.dc1.web.web02", "all.federal.prod.dc2.www.web02"),
    ]
    assert all(m["old_id"] != m["id"] for m in body["moved"])

    # live tree: new paths, inherited vars now come from the federal chain
    now = resolve(load_snapshot(session), host="web02")
    assert now["path"] == "all.federal.prod.dc2.www.web02"
    assert now["resolved"]["compliance_profile"] == "fedramp-moderate" and now["resolved"]["site_code"] == "DC2"
    assert now["resolved"]["packages"] == ["vim", "curl", "aide", "nginx", "tcpdump"]
    assert now["resolved"]["http_port"] == 8080
    export_now = build_export(load_snapshot(session))
    assert "federal_prod_dc2_www" in export_now["groups"] and "commercial_prod_dc1_web" not in export_now["groups"]
    assert export_now["groups"]["federal_prod_dc2_www"]["hosts"] == ["web01", "web02"]
    assert export_now["tags"]["canary"] == ["web01", "web02"]  # membership carried to the new rows
    assert "web01" not in export_now["groups"]["commercial_prod_dc1"]["hosts"]
    # the old revision is untouched
    assert build_export(load_snapshot(session, revision=rev0)) == before
    assert resolve(load_snapshot(session, revision=rev0), host="web02")["resolved"]["compliance_profile"] == "cis-level1"
    # rows: old closed at rev1, new opened at rev1, variables likewise
    old = session.scalar(select(Node).where(Node.id == web["id"]))
    assert old.deleted_rev == rev1 and old.path == "all.commercial.prod.dc1.web"
    new = session.scalar(select(Node).where(Node.path == "all.federal.prod.dc2.www", Node.deleted_rev.is_(None)))
    assert new.created_rev == rev1 and new.kind.value == "group" and new.parent_id == _node(editor, "all.federal.prod.dc2")["id"]
    port_rows = session.scalars(select(NodeVariable).where(NodeVariable.key == "http_port", NodeVariable.node_id.in_([web["id"], new.id])).order_by(NodeVariable.id)).all()
    assert [(v.node_id, v.created_rev, v.deleted_rev) for v in port_rows] == [(web["id"], seeded.id, rev1), (new.id, rev1, None)]
    # children live under the new ids
    kids = editor.get(f"/api/v1/nodes/{new.id}/children").json()
    assert [k["path"] for k in kids] == ["all.federal.prod.dc2.www.web01", "all.federal.prod.dc2.www.web02"]
    assert editor.get(f"/api/v1/nodes/{web['id']}").json()["deleted_rev"] == rev1


def test_rename_in_place_and_host_rename(editor, session, seeded):
    rev = current_rev(editor)
    dc1 = _node(editor, "all.commercial.prod.dc1")
    r = editor.post(f"/api/v1/nodes/{dc1['id']}/move", json={"name": "dc1a"}, if_match=rev)
    assert r.status_code == 200 and r.json()["node"]["parent_id"] == dc1["parent_id"] and r.json()["node"]["depth"] == dc1["depth"]
    assert {m["to"] for m in r.json()["moved"]} >= {"all.commercial.prod.dc1a", "all.commercial.prod.dc1a.web.web01", "all.commercial.prod.dc1a.db.db01"}
    export = build_export(load_snapshot(session))
    assert "commercial_prod_dc1a_web" in export["groups"] and export["tags"]["datacenter_dc1a"] == ["db01", "web01", "web02"]
    rev = r.json()["revision"]
    host = _node(editor, "all.commercial.prod.dc1a.web.web01")
    r = editor.post(f"/api/v1/nodes/{host['id']}/move", json={"name": "web01a"}, if_match=rev)
    assert r.status_code == 200
    assert resolve(load_snapshot(session), host="web01a")["resolved"]["ansible_host"] == "10.1.1.11"
    assert "web01" not in build_export(load_snapshot(session))["hosts"]


def test_move_refusals(editor, session, seeded):
    rev = current_rev(editor)
    root = _node(editor, "all")
    prod = _node(editor, "all.commercial.prod")
    web = _node(editor, "all.commercial.prod.dc1.web")
    web01 = _node(editor, "all.commercial.prod.dc1.web.web01")
    # root
    assert editor.post(f"/api/v1/nodes/{root['id']}/move", json={"name": "root"}, if_match=rev).status_code == 422
    # nothing to do / both parents / same place
    assert editor.post(f"/api/v1/nodes/{web['id']}/move", json={}, if_match=rev).status_code == 422
    assert editor.post(f"/api/v1/nodes/{web['id']}/move", json={"parent_id": 1, "parent_path": "all"}, if_match=rev).status_code == 422
    assert editor.post(f"/api/v1/nodes/{web['id']}/move", json={"name": "web"}, if_match=rev).status_code == 422
    # under its own subtree
    r = editor.post(f"/api/v1/nodes/{prod['id']}/move", json={"parent_path": "all.commercial.prod.dc1"}, if_match=rev)
    assert r.status_code == 422 and "own subtree" in r.json()["detail"]
    assert editor.post(f"/api/v1/nodes/{prod['id']}/move", json={"parent_path": "all.commercial.prod", "name": "x"}, if_match=rev).status_code == 422
    # kind order: a tier under a datacenter, anything under a host
    r = editor.post(f"/api/v1/nodes/{prod['id']}/move", json={"parent_path": "all.federal.prod.dc2"}, if_match=rev)
    assert r.status_code == 422 and "deeper" in r.json()["detail"]
    assert editor.post(f"/api/v1/nodes/{web['id']}/move", json={"parent_id": web01["id"]}, if_match=rev).status_code == 422
    # bad name, missing parent, missing node
    assert editor.post(f"/api/v1/nodes/{web['id']}/move", json={"name": "bad.name"}, if_match=rev).status_code == 422
    assert editor.post(f"/api/v1/nodes/{web['id']}/move", json={"parent_path": "all.nope"}, if_match=rev).status_code == 404
    assert editor.post("/api/v1/nodes/999999/move", json={"name": "x"}, if_match=rev).status_code == 404
    # live node at the new path
    r = editor.post(f"/api/v1/nodes/{web['id']}/move", json={"parent_path": "all.commercial.lab.dc1"}, if_match=rev)
    assert r.status_code == 409 and "already exists" in r.json()["detail"]
    # duplicate host name through a rename
    r = editor.post(f"/api/v1/nodes/{web01['id']}/move", json={"name": "web02"}, if_match=rev)
    assert r.status_code == 409 and "already exists" in r.json()["detail"]
    r = editor.post(f"/api/v1/nodes/{web01['id']}/move", json={"name": "db01"}, if_match=rev)
    assert r.status_code == 409 and "duplicate host" in r.json()["detail"]
    # a qualified-name collision that only exists after the move is caught by the exporter
    fed_web = _node(editor, "all.federal.prod.dc2.web")
    r = editor.patch(f"/api/v1/nodes/{fed_web['id']}", json={"inventory_name": "commercial_prod_dc1_www"}, if_match=rev)
    assert r.status_code == 200
    rev = r.json()["revision"]
    r = editor.post(f"/api/v1/nodes/{web['id']}/move", json={"name": "www"}, if_match=rev)
    assert r.status_code == 409 and "collision" in r.json()["detail"]
    # precondition still applies
    assert editor.post(f"/api/v1/nodes/{web['id']}/move", json={"name": "w"}).status_code == 428
    assert editor.post(f"/api/v1/nodes/{web['id']}/move", json={"name": "w"}, if_match=rev - 1).status_code == 409
    assert current_rev(editor) == rev
    assert session.scalar(select(Node).where(Node.path == "all.commercial.prod.dc1.web", Node.deleted_rev.is_(None))) is not None


def test_viewer_cannot_move(viewer, seeded):
    web = _node(viewer, "all.commercial.prod.dc1.web")
    assert viewer.post(f"/api/v1/nodes/{web['id']}/move", json={"name": "x"}, if_match=current_rev(viewer)).status_code == 403

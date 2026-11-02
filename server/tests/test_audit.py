"""Audit log: one row per effective write, in the write's transaction; auth events;
filters and paging; editor-readable, viewer 403."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select

from ais.models import AuditLog
from tests.conftest import AuthClient, current_rev


def _node_id(client, path):
    return client.get(f"/api/v1/nodes/{path}").json()["id"]


def _rows(session):
    return session.scalars(select(AuditLog).order_by(AuditLog.id)).all()


def test_every_write_leaves_exactly_one_row(editor, session, seeded):
    assert _rows(session) == []
    rev = current_rev(editor)
    r = editor.post("/api/v1/nodes", json={"parent_path": "all.commercial.prod.dc1", "name": "cache", "vars": {"x": 1}}, if_match=rev, headers={"X-Request-ID": "req-create"})
    assert r.status_code == 201 and r.headers["X-Request-ID"] == "req-create"
    cache = r.json()["node"]["id"]
    rev = r.json()["revision"]
    rev = editor.patch(f"/api/v1/nodes/{cache}", json={"inventory_name": "dc1_cache"}, if_match=rev).json()["revision"]
    rev = editor.put(f"/api/v1/nodes/{cache}/vars/x", json={"value": 2}, if_match=rev).json()["revision"]
    assert editor.put(f"/api/v1/nodes/{cache}/vars/x", json={"value": 2}).json()["changed"] is False  # no-op: no row
    rev = editor.delete(f"/api/v1/nodes/{cache}/vars/x", if_match=rev).json()["revision"]
    rev = editor.put("/api/v1/variables/x", json={"merge_strategy": "merge"}, if_match=rev).json()["revision"]
    assert editor.put("/api/v1/variables/x", json={"merge_strategy": "merge"}).json()["changed"] is False  # no-op: no row
    rev = editor.delete("/api/v1/variables/x", if_match=rev).json()["revision"]
    rev = editor.post(f"/api/v1/nodes/{cache}/move", json={"name": "cache2"}, if_match=rev).json()["revision"]
    cache2 = _node_id(editor, "all.commercial.prod.dc1.cache2")
    rev = editor.delete(f"/api/v1/nodes/{cache2}", if_match=rev).json()["revision"]
    r = editor.post("/api/v1/import", json={"tree": {"all": {"vars": {"ntp_server": "ntp9.example.com"}}}}, if_match=rev)
    assert r.status_code == 201 and r.json()["variables_set"] == 1 and r.json()["nodes_created"] == 0
    rev = r.json()["revision"]

    rows = _rows(session)
    assert [a.action for a in rows] == [
        "node.create", "node.update", "variable.set", "variable.unset", "definition.set", "definition.delete", "node.move", "node.delete", "import",
    ]
    assert all(a.actor == "editor@example.com" and a.request_id for a in rows)
    assert rows[0].request_id == "req-create" and rows[0].source == "api"
    assert [a.revision for a in rows] == list(range(seeded.id + 1, rev + 1))  # one row per revision, in step
    create, update, vset, vunset, dset, ddel, move, delete, imp = rows
    assert create.before is None and create.after["path"] == "all.commercial.prod.dc1.cache" and create.after["vars"] == {"x": 1}
    assert (update.before["inventory_name"], update.after["inventory_name"]) == (None, "dc1_cache")
    assert vset.target_type == "node_variable" and vset.target_id == f"{cache}/x" and (vset.before["value"], vset.after["value"]) == (1, 2)
    assert vunset.before["value"] == 2 and vunset.after is None
    assert dset.before is None and dset.after["merge_strategy"] == "merge" and dset.target_id == "x"
    assert ddel.before["merge_strategy"] == "merge" and ddel.after is None
    assert move.before["path"] == "all.commercial.prod.dc1.cache" and move.after["path"] == "all.commercial.prod.dc1.cache2"
    assert move.after["moved"][0]["old_id"] == cache and move.after["moved"][0]["id"] == cache2
    assert delete.before["subtree"] == ["all.commercial.prod.dc1.cache2"] and delete.after is None
    assert imp.source == "import" and imp.target_type == "tree" and imp.after["variables_set"] == 1


def test_refused_writes_leave_no_row(editor, session, seeded):
    rev = current_rev(editor)
    assert editor.post("/api/v1/nodes", json={"parent_path": "all", "name": "x"}).status_code == 428
    assert editor.post("/api/v1/nodes", json={"parent_path": "all", "name": "bad.name"}, if_match=rev).status_code == 422
    assert editor.post("/api/v1/nodes", json={"parent_path": "all.commercial", "name": "prod"}, if_match=rev).status_code == 409
    dc2 = _node_id(editor, "all.federal.prod.dc2")
    assert editor.patch(f"/api/v1/nodes/{dc2}", json={"inventory_name": "commercial_prod_dc1_web"}, if_match=rev).status_code == 409
    assert editor.post("/api/v1/nodes", json={"parent_path": "all", "name": "x"}, if_match=rev - 1).status_code == 409
    assert _rows(session) == [] and current_rev(editor) == rev


def test_auth_events(admin, anon, session, seeded):
    assert anon.post("/api/v1/auth/login", json={"email": "Admin@example.com", "password": "wrong"}).status_code == 401
    assert anon.post("/api/v1/auth/login", json={"email": "nobody@example.com", "password": "x"}).status_code == 401
    login = anon.post("/api/v1/auth/login", json={"email": "admin@example.com", "password": "admin-pass-example"})
    assert login.status_code == 200
    tok = admin.post("/api/v1/tokens", json={"name": "ci", "role": "viewer"}).json()
    assert admin.delete(f"/api/v1/tokens/{tok['id']}").status_code == 204
    assert admin.delete(f"/api/v1/tokens/{tok['id']}").status_code == 204  # already revoked: no second row
    assert AuthClient(anon.app, login.json()["token"]).post("/api/v1/auth/logout").status_code == 204
    u = admin.post("/api/v1/users", json={"email": "svc@example.com", "role": "editor"}).json()
    admin.patch(f"/api/v1/users/{u['id']}", json={"role": "viewer", "password": "new-pass-example"})

    rows = _rows(session)
    assert [(a.action, a.actor, a.target_type, a.target_id, a.source) for a in rows] == [
        ("login.failure", "admin@example.com", "user", "admin@example.com", "login"),
        ("login.failure", "nobody@example.com", "user", "nobody@example.com", "login"),
        ("login.success", "admin@example.com", "user", "admin@example.com", "login"),
        ("token.create", "admin@example.com", "token", str(tok["id"]), "api"),
        ("token.revoke", "admin@example.com", "token", str(tok["id"]), "api"),
        ("logout", "admin@example.com", "token", str(login.json()["token"] and rows[5].target_id), "login"),
        ("user.create", "admin@example.com", "user", "svc@example.com", "api"),
        ("user.update", "admin@example.com", "user", "svc@example.com", "api"),
    ]
    assert rows[3].after["role"] == "viewer" and "token" not in rows[3].after  # the secret is never logged
    assert rows[4].before["revoked_at"] is None and rows[4].after["revoked_at"] is not None
    assert (rows[7].before["role"], rows[7].after["role"], rows[7].after["password_changed"]) == ("editor", "viewer", True)
    assert "password" not in str(rows[7].after) or rows[7].after.get("password_changed") is True
    assert all(a.revision is None for a in rows)


def test_audit_route_roles_filters_and_paging(admin, editor, viewer, session, seeded):
    rev = current_rev(editor)
    web = _node_id(editor, "all.commercial.prod.dc1.web")
    t0 = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=5)
    for i in range(3):
        rev = editor.put(f"/api/v1/nodes/{web}/vars/http_port", json={"value": 9000 + i}, if_match=rev).json()["revision"]
    rev = editor.put("/api/v1/variables/zzz", json={"merge_strategy": "append"}, if_match=rev).json()["revision"]
    admin.post("/api/v1/tokens", json={"name": "x"})

    assert viewer.get("/api/v1/audit").status_code == 403
    assert editor.get("/api/v1/audit").status_code == 200
    rows = admin.get("/api/v1/audit").json()
    assert [r["action"] for r in rows] == ["token.create", "definition.set", "variable.set", "variable.set", "variable.set"]  # newest first
    assert {k for k in rows[0]} == {"id", "ts", "actor", "action", "target_type", "target_id", "before", "after", "source", "request_id", "revision"}
    assert [r["after"]["value"] for r in admin.get("/api/v1/audit", params={"action": "variable.set"}).json()] == [9002, 9001, 9000]
    assert len(admin.get("/api/v1/audit", params={"action": "variable."}).json()) == 3  # prefix
    assert len(admin.get("/api/v1/audit", params={"actor": "admin@example.com"}).json()) == 1
    assert len(admin.get("/api/v1/audit", params={"target_type": "node_variable", "target_id": f"{web}/http_port"}).json()) == 3
    assert [r["after"]["value"] for r in admin.get("/api/v1/audit", params={"revision": rev - 1}).json()] == [9002]
    assert len(admin.get("/api/v1/audit", params={"since": t0.isoformat()}).json()) == 5
    assert admin.get("/api/v1/audit", params={"until": t0.isoformat()}).json() == []
    assert len(admin.get("/api/v1/audit", params={"since": (t0 - dt.timedelta(days=1)).replace(tzinfo=None).isoformat(), "until": (t0 + dt.timedelta(days=1)).isoformat()}).json()) == 5
    page1 = admin.get("/api/v1/audit", params={"limit": 2}).json()
    page2 = admin.get("/api/v1/audit", params={"limit": 2, "before": page1[-1]["id"]}).json()
    page3 = admin.get("/api/v1/audit", params={"limit": 2, "before": page2[-1]["id"]}).json()
    assert [r["id"] for r in page1 + page2 + page3] == sorted((r["id"] for r in rows), reverse=True)
    assert admin.get("/api/v1/audit", params={"request_id": rows[1]["request_id"]}).json()[0]["id"] == rows[1]["id"]


def test_source_header_is_recorded_on_revision_and_audit(editor, viewer, seeded):
    rev = current_rev(editor)
    r = editor.post("/api/v1/nodes", json={"parent_path": "all.commercial.prod.dc1", "name": "m"}, if_match=rev, headers={"X-AIS-Source": "module"})
    assert r.status_code == 201
    assert viewer.get("/api/v1/revisions", params={"limit": 1}).json()[0]["source"] == "module"
    assert editor.get("/api/v1/audit", params={"limit": 1}).json()[0]["source"] == "module"
    r = editor.post("/api/v1/nodes", json={"parent_path": "all.commercial.prod.dc1", "name": "n"}, if_match=rev + 1, headers={"X-AIS-Source": "evil"})
    assert r.status_code == 201 and viewer.get("/api/v1/revisions", params={"limit": 1}).json()[0]["source"] == "api"


def test_node_history_survives_moves(editor, seeded):
    """GET /audit?node=<path>&key= follows node.move rows back through every id the node had,
    including moves of an ancestor (which change the ids of the whole subtree)."""
    rev = current_rev(editor)
    web = _node_id(editor, "all.commercial.prod.dc1.web")
    rev = editor.put(f"/api/v1/nodes/{web}/vars/http_port", json={"value": 9001}, if_match=rev).json()["revision"]
    rev = editor.post(f"/api/v1/nodes/{web}/move", json={"name": "www"}, if_match=rev).json()["revision"]
    www = _node_id(editor, "all.commercial.prod.dc1.www")
    rev = editor.put(f"/api/v1/nodes/{www}/vars/http_port", json={"value": 9002}, if_match=rev).json()["revision"]
    dc1 = _node_id(editor, "all.commercial.prod.dc1")
    rev = editor.post(f"/api/v1/nodes/{dc1}/move", json={"name": "dc9"}, if_match=rev).json()["revision"]
    www2 = _node_id(editor, "all.commercial.prod.dc9.www")
    rev = editor.put(f"/api/v1/nodes/{www2}/vars/http_port", json={"value": 9003}, if_match=rev).json()["revision"]
    assert len({web, www, www2}) == 3

    rows = editor.get("/api/v1/audit", params={"node": "all.commercial.prod.dc9.www", "key": "http_port"}).json()
    assert [r["after"]["value"] for r in rows] == [9003, 9002, 9001]
    everything = editor.get("/api/v1/audit", params={"node": "all.commercial.prod.dc9.www"}).json()
    assert [r["action"] for r in everything] == ["variable.set", "variable.set", "node.move", "variable.set"]
    assert editor.get("/api/v1/audit", params={"key": "http_port"}).status_code == 422
    assert editor.get("/api/v1/audit", params={"node": "all.nowhere"}).status_code == 404

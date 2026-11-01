"""json_schema on variable definitions is enforced at write time."""

from __future__ import annotations

from ais.importer import ImportError_, import_tree
from tests.conftest import current_rev

PORT = {"type": "integer", "minimum": 1, "maximum": 65535}


def _node_id(client, path):
    return client.get(f"/api/v1/nodes/{path}").json()["id"]


def test_value_writes_are_validated(editor, seeded):
    rev = current_rev(editor)
    assert editor.put("/api/v1/variables/http_port", json={"json_schema": {"type": "nope"}}, if_match=rev).status_code == 422
    rev = editor.put("/api/v1/variables/http_port", json={"json_schema": PORT}, if_match=rev).json()["revision"]
    web = _node_id(editor, "all.commercial.prod.dc1.web")
    bad = editor.put(f"/api/v1/nodes/{web}/vars/http_port", json={"value": "8080"}, if_match=rev)
    assert bad.status_code == 422 and "http_port at all.commercial.prod.dc1.web" in bad.json()["detail"]
    assert editor.put(f"/api/v1/nodes/{web}/vars/http_port", json={"value": 70000}, if_match=rev).status_code == 422
    ok = editor.put(f"/api/v1/nodes/{web}/vars/http_port", json={"value": 8081}, if_match=rev)
    assert ok.status_code == 200
    rev = ok.json()["revision"]
    r = editor.post("/api/v1/nodes", json={"parent_path": "all.commercial.prod.dc1", "name": "cache", "vars": {"http_port": -1}}, if_match=rev)
    assert r.status_code == 422
    assert current_rev(editor) == rev


def test_new_schema_must_accept_live_values(editor, seeded):
    rev = current_rev(editor)
    r = editor.put("/api/v1/variables/http_port", json={"json_schema": {"type": "string"}}, if_match=rev)
    assert r.status_code == 422 and "http_port at all.commercial.prod.dc1.web" in r.json()["detail"]
    assert editor.get("/api/v1/variables/http_port").status_code == 404  # nothing stored
    assert current_rev(editor) == rev


def test_importer_validates(session, seeded):
    tree = {"variables": {"http_port": {"json_schema": PORT}}, "all": {"children": {"commercial": {"vars": {"http_port": "x"}}}}}
    try:
        import_tree(session, tree)
    except ImportError_ as exc:
        assert "http_port at all.commercial" in str(exc)
    else:
        raise AssertionError("import accepted a value its schema rejects")
    session.rollback()
    try:
        import_tree(session, {"variables": {"http_port": {"json_schema": {"type": 3}}}, "all": {}})
    except ImportError_ as exc:
        assert "invalid json_schema" in str(exc)
    else:
        raise AssertionError("import accepted an invalid schema")

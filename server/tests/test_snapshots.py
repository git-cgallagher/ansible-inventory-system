"""host_snapshot materialization and diffs."""

from __future__ import annotations

from sqlalchemy import select

from ais.models import HostSnapshot
from ais.snapshots import materialize
from ais.resolver import load_snapshot
from tests.conftest import current_rev


def test_export_and_render_materialize(viewer, session, seeded):
    assert session.scalar(select(HostSnapshot)) is None
    viewer.post("/api/v1/render", json={"host": "web01"})
    rows = session.scalars(select(HostSnapshot)).all()
    assert [(r.host_name, r.revision) for r in rows] == [("web01", seeded.id)]
    viewer.get("/api/v1/export")
    rows = session.scalars(select(HostSnapshot)).all()
    assert sorted(r.host_name for r in rows) == ["db01", "fed-edge01", "fed-web01", "lab-web01", "web01", "web02"]
    assert all(r.revision == seeded.id for r in rows)
    # second export at the same revision adds nothing (on-conflict-do-nothing path too)
    viewer.get("/api/v1/export")
    assert materialize(session, load_snapshot(session)) == 0
    assert len(session.scalars(select(HostSnapshot)).all()) == 6
    listed = viewer.get("/api/v1/hosts/web01/snapshots").json()
    assert [s["revision"] for s in listed] == [seeded.id]
    full = viewer.get(f"/api/v1/hosts/web01/snapshots/{seeded.id}").json()
    assert full["resolved"]["http_port"] == 8080 and full["resolved_hash"] == listed[0]["resolved_hash"]
    assert viewer.get("/api/v1/hosts/nope/snapshots").status_code == 404


def test_diff_between_revisions(editor, viewer, seeded):
    rev0 = current_rev(editor)
    web = next(n["id"] for n in editor.get("/api/v1/nodes").json() if n["path"] == "all.commercial.prod.dc1.web")
    rev1 = editor.put(f"/api/v1/nodes/{web}/vars/http_port", json={"value": 9090}, if_match=rev0).json()["revision"]
    rev2 = editor.put(f"/api/v1/nodes/{web}/vars/tls", json={"value": True}, if_match=rev1).json()["revision"]
    rev3 = editor.delete(f"/api/v1/nodes/{web}/vars/http_port", if_match=rev2).json()["revision"]
    d = viewer.get("/api/v1/diff", params={"host": "web01", "from": rev0, "to": rev2}).json()
    assert d["changed"] == {"http_port": {"from": 8080, "to": 9090}} and d["added"] == {"tls": True} and d["removed"] == {}
    assert d["identical"] is False and d["from_hash"] != d["to_hash"]
    d = viewer.get("/api/v1/diff", params={"host": "web01", "from": rev2, "to": rev3}).json()
    assert d["removed"] == {"http_port": 9090} and d["added"] == {} and d["changed"] == {}
    # snapshots were computed on demand for every revision asked about
    assert [s["revision"] for s in viewer.get("/api/v1/hosts/web01/snapshots").json()] == [rev0, rev2, rev3]
    # inventory_revision alone never counts as a change
    same = viewer.get("/api/v1/diff", params={"host": "db01", "from": rev0, "to": rev3}).json()
    assert same["identical"] is True and same["from_hash"] == same["to_hash"]
    assert viewer.get("/api/v1/diff", params={"host": "web01", "from": rev0, "to": 9999}).status_code == 404
    assert viewer.get("/api/v1/diff", params={"host": "ghost", "from": rev0, "to": rev1}).status_code == 404


def test_deleted_host_has_no_snapshot_after_deletion(editor, viewer, seeded):
    rev0 = current_rev(editor)
    web01 = next(n["id"] for n in editor.get("/api/v1/nodes").json() if n["path"] == "all.commercial.prod.dc1.web.web01")
    rev1 = editor.delete(f"/api/v1/nodes/{web01}", if_match=rev0).json()["revision"]
    assert viewer.get(f"/api/v1/hosts/web01/snapshots/{rev0}").status_code == 200
    assert viewer.get(f"/api/v1/hosts/web01/snapshots/{rev1}").status_code == 404
    assert viewer.get("/api/v1/diff", params={"host": "web01", "from": rev0, "to": rev1}).status_code == 404

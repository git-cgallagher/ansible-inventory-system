"""Tag write API: declarative PUT, members by path, var-less, locked and audited."""

from __future__ import annotations

from sqlalchemy import select

from ais.models import AuditLog
from tests.conftest import current_rev


def _node_id(client, path):
    return client.get(f"/api/v1/nodes/{path}").json()["id"]


def test_tag_lifecycle_exports_member_hosts(editor, viewer, session, seeded):
    rev = current_rev(editor)
    body = {"description": "patch first", "members": ["all.commercial.prod.dc1.web", "all.commercial.prod.dc1.db.db01"]}
    assert editor.put("/api/v1/tags/canary", json=body).status_code == 428
    r = editor.put("/api/v1/tags/canary", json=body, if_match=rev)
    assert r.status_code == 200 and r.json()["changed"] is True and r.json()["revision"] == rev + 1
    assert r.json()["tag"] == {"name": "canary", "description": "patch first", "members": sorted(body["members"])}
    assert viewer.get("/api/v1/export").json()["tags"]["canary"] == ["db01", "web01", "web02"]
    # idempotent re-put: no revision, no precondition needed
    again = editor.put("/api/v1/tags/canary", json=body)
    assert again.json()["changed"] is False and again.json()["revision"] == rev + 1
    # replace the member set
    r = editor.put("/api/v1/tags/canary", json={"description": "patch first", "members": ["all.commercial.prod.dc1.web.web02"]}, if_match=rev + 1)
    assert r.json()["tag"]["members"] == ["all.commercial.prod.dc1.web.web02"]
    assert viewer.get("/api/v1/tags").json() == [r.json()["tag"]]
    assert viewer.get("/api/v1/export").json()["tags"]["canary"] == ["web02"]
    r = editor.delete("/api/v1/tags/canary", if_match=rev + 2)
    assert r.status_code == 200 and "canary" not in viewer.get("/api/v1/export").json()["tags"]
    assert viewer.get("/api/v1/tags/canary").status_code == 404
    actions = [a.action for a in session.scalars(select(AuditLog).where(AuditLog.target_type == "tag").order_by(AuditLog.id))]
    assert actions == ["tag.set", "tag.set", "tag.delete"]


def test_tag_refusals(editor, viewer, seeded):
    rev = current_rev(editor)
    assert viewer.put("/api/v1/tags/x", json={}, if_match=rev).status_code == 403
    assert editor.put("/api/v1/tags/x", json={"vars": {"a": 1}}, if_match=rev).status_code == 422  # tags never carry vars
    assert editor.put("/api/v1/tags/bad-name", json={}, if_match=rev).status_code == 422
    assert editor.put("/api/v1/tags/tier_prod", json={}, if_match=rev).status_code == 422  # automatic tag group prefix
    assert editor.put("/api/v1/tags/x", json={"members": ["all.nowhere"]}, if_match=rev).status_code == 404
    # a tag named like a hierarchy group is refused by the exporter check
    assert editor.put("/api/v1/tags/commercial_prod", json={"members": ["all.commercial.lab"]}, if_match=rev).status_code == 409
    assert current_rev(editor) == rev  # nothing above left a revision
    assert editor.delete("/api/v1/tags/x", if_match=rev).status_code == 404


def test_moved_member_keeps_its_tag(editor, viewer, seeded):
    rev = current_rev(editor)
    rev = editor.put("/api/v1/tags/canary", json={"members": ["all.commercial.prod.dc1.web.web01"]}, if_match=rev).json()["revision"]
    web01 = _node_id(editor, "all.commercial.prod.dc1.web.web01")
    rev = editor.post(f"/api/v1/nodes/{web01}/move", json={"parent_path": "all.commercial.prod.dc1.db"}, if_match=rev).json()["revision"]
    assert viewer.get("/api/v1/tags/canary").json()["members"] == ["all.commercial.prod.dc1.db.web01"]
    assert viewer.get("/api/v1/export").json()["tags"]["canary"] == ["web01"]

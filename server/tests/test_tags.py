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
    assert editor.put("/api/v1/tags/bad.name", json={}, if_match=rev).status_code == 422
    assert editor.put("/api/v1/tags/tier_prod", json={}, if_match=rev).status_code == 422  # automatic tag group prefix
    assert editor.put("/api/v1/tags/tier-prod", json={}, if_match=rev).status_code == 422  # ... after the hyphen maps to '_'
    assert editor.put("/api/v1/tags/x", json={"members": ["all.nowhere"]}, if_match=rev).status_code == 404
    # a tag whose group is a hierarchy group's name is refused, members or not, hyphen or not
    for name in ("commercial_prod", "commercial-prod"):
        for body in ({"members": ["all.commercial.lab"]}, {}):
            r = editor.put(f"/api/v1/tags/{name}", json=body, if_match=rev)
            assert r.status_code == 422 and "hierarchy group of all.commercial.prod" in r.json()["detail"], r.text
            r = editor.put(f"/api/v1/tags/{name}", json=body, params={"dry_run": "true"})
            assert r.status_code == 422, r.text  # the dry run refuses it the same way
    assert current_rev(editor) == rev  # nothing above left a revision
    assert editor.delete("/api/v1/tags/x", if_match=rev).status_code == 404


def test_moved_member_keeps_its_tag(editor, viewer, seeded):
    rev = current_rev(editor)
    rev = editor.put("/api/v1/tags/canary", json={"members": ["all.commercial.prod.dc1.web.web01"]}, if_match=rev).json()["revision"]
    web01 = _node_id(editor, "all.commercial.prod.dc1.web.web01")
    rev = editor.post(f"/api/v1/nodes/{web01}/move", json={"parent_path": "all.commercial.prod.dc1.db"}, if_match=rev).json()["revision"]
    assert viewer.get("/api/v1/tags/canary").json()["members"] == ["all.commercial.prod.dc1.db.web01"]
    assert viewer.get("/api/v1/export").json()["tags"]["canary"] == ["web01"]


def test_hyphenated_tag_is_emitted_like_a_node_name(editor, viewer, seeded):
    """One name rule: a tag may hold '-', and its group maps it to '_' as a node's does."""
    rev = current_rev(editor)
    dry = editor.put("/api/v1/tags/gpu-canary", json={"members": ["all.commercial.prod.dc1.web.web01"]}, params={"dry_run": "true"}).json()
    assert [(h["host"], h["joined"]) for h in dry["impact"]["hosts"]] == [("web01", ["gpu_canary"])]
    r = editor.put("/api/v1/tags/gpu-canary", json={"members": ["all.commercial.prod.dc1.web.web01"]}, if_match=rev)
    assert r.status_code == 200 and r.json()["tag"]["name"] == "gpu-canary"
    rev = r.json()["revision"]
    export = viewer.get("/api/v1/export").json()
    assert export["tags"]["gpu_canary"] == ["web01"] and "gpu-canary" not in export["tags"]
    assert viewer.get("/api/v1/tags/gpu-canary").json()["members"] == ["all.commercial.prod.dc1.web.web01"]
    # the other spelling of the same group is refused at write time, with or without members
    for body in ({}, {"members": ["all.commercial.prod.dc1.web.web02"]}):
        r = editor.put("/api/v1/tags/gpu_canary", json=body, if_match=rev)
        assert r.status_code == 422 and "which tag 'gpu-canary' already is" in r.json()["detail"], r.text
    # the tag's own name is no conflict: converging it again works
    r = editor.put("/api/v1/tags/gpu-canary", json={"description": "d", "members": ["all.commercial.prod.dc1.web.web02"]}, if_match=rev)
    assert r.status_code == 200 and r.json()["changed"] is True
    rev = r.json()["revision"]
    # an inventory_name counts as the hierarchy group it names
    lab = _node_id(editor, "all.commercial.lab")
    rev = editor.patch(f"/api/v1/nodes/{lab}", json={"inventory_name": "lab-pool"}, if_match=rev).json()["revision"]
    r = editor.put("/api/v1/tags/lab-pool", json={}, if_match=rev)
    assert r.status_code == 422 and "hierarchy group of all.commercial.lab" in r.json()["detail"]
    assert current_rev(editor) == rev
    r = editor.delete("/api/v1/tags/gpu-canary", if_match=rev)
    assert r.status_code == 200


def test_export_refuses_two_tags_for_one_group(session, seeded):
    """The backstop for writes that skip the tag check: two tags that map to one group never
    merge silently."""
    import pytest

    from ais.models import Node, Tag, TagMember
    from ais.resolver import build_export, load_snapshot

    web01 = session.scalar(select(Node).where(Node.path == "all.commercial.prod.dc1.web.web01"))
    for name in ("gpu-canary", "gpu_canary"):
        tag = Tag(name=name)
        session.add(tag)
        session.flush()
        session.add(TagMember(tag_id=tag.id, node_id=web01.id))
    session.flush()
    with pytest.raises(ValueError, match="tags 'gpu-canary' and 'gpu_canary' are both emitted as group 'gpu_canary'"):
        build_export(load_snapshot(session))


def test_revert_cannot_bring_back_a_tag_whose_group_is_taken(editor, seeded):
    """A revert recreates a deleted tag through ``converge_tag``, so it meets the same check."""
    rev = current_rev(editor)
    rev = editor.put("/api/v1/tags/gpu_canary", json={"members": ["all.commercial.prod.dc1.web"]}, if_match=rev).json()["revision"]
    deleted = editor.delete("/api/v1/tags/gpu_canary", if_match=rev).json()["revision"]
    rev = editor.put("/api/v1/tags/gpu-canary", json={"members": ["all.commercial.prod.dc1.web"]}, if_match=deleted).json()["revision"]
    r = editor.post(f"/api/v1/revisions/{deleted}/revert", json={}, if_match=rev)
    assert r.status_code == 422 and "which tag 'gpu-canary' already is" in r.text, r.text
    assert current_rev(editor) == rev

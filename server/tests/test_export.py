from __future__ import annotations

import pytest

from ais.importer import import_tree
from ais.resolver import build_export, load_snapshot, resolve


def test_export_structure(session, seeded):
    snap = load_snapshot(session)
    data = build_export(snap)
    assert data["revision"] == seeded.id
    g = data["groups"]
    assert g["all"]["children"] == ["commercial", "federal"]
    assert g["commercial_prod_dc1_web"]["hosts"] == ["web01", "web02"]
    # fold emitted at contributing node, cumulative
    assert g["commercial_prod_dc1_web"]["vars"]["packages"] == ["vim", "curl", "awscli", "nginx"]
    # non-contributing node emits nothing for the key
    assert "packages" not in g["commercial_prod"]["vars"]
    # replace var emitted as-is
    assert g["commercial_prod_dc1"]["vars"]["dns_servers"] == ["10.1.0.2", "10.1.0.3"]
    # tags are var-less groups of hosts
    assert data["tags"]["tier_prod"] == ["db01", "fed-edge01", "fed-web01", "web01", "web02"]


def test_subtree_export_keeps_ancestor_chain(session, seeded):
    snap = load_snapshot(session, subtree="all.federal.prod")
    data = build_export(snap)
    assert set(data["hosts"]) == {"fed-edge01", "fed-web01"}
    assert "commercial" not in data["groups"]
    assert data["groups"]["all"]["children"] == ["federal"]
    assert data["groups"]["federal"]["vars"]["compliance_profile"] == "fedramp-moderate"


def test_mock_host_render(session, seeded):
    snap = load_snapshot(session)
    out = resolve(snap, path="all.commercial.lab.dc1.web", overrides={"http_port": 9000})
    assert out["host"] is None
    assert out["resolved"]["log_level"] == "debug"
    assert out["resolved"]["http_port"] == 9000
    assert [p["group"] for p in out["provenance"]["http_port"]] == ["commercial_lab_dc1_web", "<override>"]
    assert out["resolved"]["packages"] == ["vim", "curl", "awscli"]


def test_revision_pinning(session, seeded):
    rev2 = import_tree(session, {"all": {"vars": {"ntp_server": "ntp2.example.com"}}})
    session.commit()
    assert resolve(load_snapshot(session, revision=seeded.id), host="web01")["resolved"]["ntp_server"] == "time.example.com"
    assert resolve(load_snapshot(session, revision=rev2.id), host="web01")["resolved"]["ntp_server"] == "ntp2.example.com"


def test_group_name_collision_rejected(session):
    import_tree(session, {"all": {"children": {"a_b": {"children": {"c": {}}}, "a": {"children": {"b_c": {}}}}}})
    session.commit()
    with pytest.raises(ValueError, match="collision"):
        build_export(load_snapshot(session))


def test_bad_kind_order_rejected(session):
    from ais.importer import ImportError_

    with pytest.raises(ImportError_):
        import_tree(session, {"all": {"children": {"x": {"kind": "tier", "children": {"y": {"kind": "business"}}}}}})


def test_export_unknown_subtree_detail_is_plain_text(viewer, seeded):
    # The same KeyError quoting as the render (issue #49).
    r = viewer.get("/api/v1/export", params={"subtree": "all.nope"})
    assert r.status_code == 404 and r.json()["detail"] == "subtree 'all.nope' does not exist"

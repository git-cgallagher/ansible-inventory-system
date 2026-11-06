"""Bounded node lookup for pickers (issue #27): ``GET /api/v1/nodes?q=&kind=&limit=``.

Ranking (exact name, name prefix, path prefix, name substring, path substring), case, LIKE
wildcards in ``q``, ``kind``, ``subtree``, ``revision``, the limit, and that ``/nodes`` with
none of the lookup parameters still lists every node."""

from __future__ import annotations

from tests.conftest import current_rev


def _paths(r) -> list[str]:
    assert r.status_code == 200, r.text
    return [n["path"] for n in r.json()]


def _names(r) -> list[str]:
    return [p.rsplit(".", 1)[-1] for p in _paths(r)]


def test_without_lookup_parameters_every_node_is_listed(viewer, seeded):
    rows = viewer.get("/api/v1/nodes").json()
    assert len(rows) == 20
    assert [n["path"] for n in rows] == sorted(n["path"] for n in rows)
    sub = viewer.get("/api/v1/nodes", params={"subtree": "all.commercial.lab"}).json()
    assert [n["path"] for n in sub] == [
        "all.commercial.lab",
        "all.commercial.lab.dc1",
        "all.commercial.lab.dc1.web",
        "all.commercial.lab.dc1.web.lab-web01",
    ]


def test_tiers_exact_then_prefix_then_substring(viewer, seeded):
    # "web": the three groups named web (exact), then web01/web02 (name prefix), then
    # lab-web01 and fed-web01 (name substring); nothing starts a path with "web".
    r = viewer.get("/api/v1/nodes", params={"q": "web"})
    assert _paths(r) == [
        "all.commercial.lab.dc1.web",
        "all.commercial.prod.dc1.web",
        "all.federal.prod.dc2.web",
        "all.commercial.prod.dc1.web.web01",
        "all.commercial.prod.dc1.web.web02",
        "all.commercial.lab.dc1.web.lab-web01",
        "all.federal.prod.dc2.web.fed-web01",
        # path substring last: hosts under a web group whose own name lacks "web"
        "all.federal.prod.dc2.web.edge",
        "all.federal.prod.dc2.web.edge.fed-edge01",
    ]


def test_kind_and_limit(viewer, seeded):
    r = viewer.get("/api/v1/nodes", params={"q": "web", "kind": "host"})
    assert _names(r) == ["web01", "web02", "lab-web01", "fed-web01", "fed-edge01"]
    r = viewer.get("/api/v1/nodes", params={"q": "web", "kind": "host", "limit": 2})
    assert _names(r) == ["web01", "web02"]
    # several kinds: repeat the parameter
    r = viewer.get("/api/v1/nodes", params=[("q", "dc"), ("kind", "datacenter"), ("kind", "tier")])
    assert _paths(r) == ["all.commercial.lab.dc1", "all.commercial.prod.dc1", "all.federal.prod.dc2"]
    # limit alone (no q) is a bounded listing in path order
    r = viewer.get("/api/v1/nodes", params={"limit": 3})
    assert _paths(r) == ["all", "all.commercial", "all.commercial.lab"]
    assert viewer.get("/api/v1/nodes", params={"q": "web", "limit": 0}).status_code == 422
    assert viewer.get("/api/v1/nodes", params={"q": "web", "limit": 101}).status_code == 422
    assert viewer.get("/api/v1/nodes", params={"q": "web", "kind": "rack"}).status_code == 422


def test_path_prefix_and_case(viewer, seeded):
    r = viewer.get("/api/v1/nodes", params={"q": "ALL.Federal.prod.dc2.WEB", "kind": "host"})
    assert _names(r) == ["fed-edge01", "fed-web01"]
    r = viewer.get("/api/v1/nodes", params={"q": "WEB0", "kind": "host"})
    assert _names(r) == ["web01", "web02", "lab-web01", "fed-web01"]


def test_like_wildcards_match_themselves(viewer, editor, seeded):
    rev = current_rev(editor)
    assert editor.post("/api/v1/nodes", json={"parent_path": "all.commercial.prod.dc1.web", "name": "web_x1", "kind": "host"}, if_match=rev).status_code == 201
    # "_" is a single-character wildcard in LIKE: unescaped, "web_" would match web01 too.
    assert _names(viewer.get("/api/v1/nodes", params={"q": "web_", "kind": "host"})) == ["web_x1"]
    assert viewer.get("/api/v1/nodes", params={"q": "%"}).json() == []
    assert viewer.get("/api/v1/nodes", params={"q": "\\"}).json() == []


def test_subtree_and_revision(viewer, editor, seeded):
    r = viewer.get("/api/v1/nodes", params={"q": "web", "kind": "host", "subtree": "all.commercial"})
    assert _names(r) == ["web01", "web02", "lab-web01"]
    # the subtree root itself matches
    assert _paths(viewer.get("/api/v1/nodes", params={"q": "lab", "subtree": "all.commercial.lab", "limit": 1})) == ["all.commercial.lab"]
    # an unknown subtree matches nothing (like the unbounded list)
    assert viewer.get("/api/v1/nodes", params={"q": "web", "subtree": "all.nowhere"}).json() == []

    rev = current_rev(editor)
    web02 = editor.get("/api/v1/nodes/all.commercial.prod.dc1.web.web02").json()
    r = editor.post(f"/api/v1/nodes/{web02['id']}/move", json={"name": "www02"}, if_match=rev)
    assert r.status_code == 200, r.text
    assert _names(viewer.get("/api/v1/nodes", params={"q": "web0", "kind": "host", "subtree": "all.commercial.prod"})) == ["web01"]
    assert _names(viewer.get("/api/v1/nodes", params={"q": "www", "kind": "host"})) == ["www02"]
    # at the revision before the rename the old name is there and the new one is not
    old = viewer.get("/api/v1/nodes", params={"q": "web0", "kind": "host", "subtree": "all.commercial.prod", "revision": rev})
    assert _names(old) == ["web01", "web02"]
    assert viewer.get("/api/v1/nodes", params={"q": "www", "revision": rev}).json() == []
    assert viewer.get("/api/v1/nodes", params={"q": "web", "revision": 999}).status_code == 404


def test_exact_name_first_however_many_partial_matches(viewer, editor, seeded):
    # The host module's find_host (issue #43) asks for q=<name>&kind=host and keeps the row
    # whose name equals it: that row must come back even when more hosts than the limit start
    # with or contain the name and sort before it in path order (lab sorts before prod).
    for name in ["web01a", "web01b", "web01c", "xweb01", "WEB01"]:
        rev = current_rev(editor)
        r = editor.post("/api/v1/nodes", json={"parent_path": "all.commercial.lab.dc1.web", "name": name, "kind": "host"}, if_match=rev)
        assert r.status_code == 201, r.text
    # tier 1 is equality ignoring case: WEB01 (lab) and web01 (prod), in path order
    r = viewer.get("/api/v1/nodes", params={"q": "web01", "kind": "host", "limit": 2})
    assert _paths(r) == ["all.commercial.lab.dc1.web.WEB01", "all.commercial.prod.dc1.web.web01"]
    r = viewer.get("/api/v1/nodes", params={"q": "web01", "kind": "host"})
    assert _names(r)[:2] == ["WEB01", "web01"]
    assert [n["name"] for n in r.json() if n["name"] == "web01"] == ["web01"]


def test_lookup_needs_a_token(anon, seeded):
    assert anon.get("/api/v1/nodes", params={"q": "web"}).status_code == 401

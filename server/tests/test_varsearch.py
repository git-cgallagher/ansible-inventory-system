"""Variable search (issue 23): where a key is set (``GET /vars``), which hosts have which
resolved value (``GET /vars/hosts``), audit rows by key alone, and the ``ais vars`` CLI.

The host values must be exactly what ``resolve`` (and therefore a playbook, see
``test_parity.py``) gives, for every strategy, on a tree deeper than six levels."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from alembic import command
from sqlalchemy import text

from ais import db as aisdb
from ais.models import NodeKind
from ais.resolver import emitted_vars, load_snapshot, resolve
from tests.conftest import AuthClient, current_rev

# Depth 8 (all → b → tier → dc → g1 → g2 → g3 → host), every strategy, the key set in two
# branches with different values, a host that sets the key itself, and hosts without it.
DEEP = {
    "variables": {
        "pkgs": {"merge_strategy": "unique_append"},
        "lst": {"merge_strategy": "append"},
        "cfg": {"merge_strategy": "merge"},
        "drv": {"merge_strategy": "replace"},
    },
    "all": {
        "vars": {"pkgs": ["a"], "lst": [1], "cfg": {"x": 1, "n": {"a": 1}}},
        "children": {
            "b1": {
                "vars": {"pkgs": ["b"], "lst": [2], "drv": "560.35"},
                "children": {
                    "prod": {
                        "children": {
                            "dc1": {
                                "vars": {"cfg": {"n": {"b": 2}}},
                                "children": {
                                    "g1": {
                                        "vars": {"drv": "550.90", "lst": [1]},
                                        "children": {
                                            "g2": {
                                                "vars": {"pkgs": ["a", "c"]},
                                                "children": {
                                                    "g3": {
                                                        "hosts": {
                                                            "h1": {"drv": 550.9, "cfg": {"x": 9}, "lst": [3]},
                                                            "h2": {},
                                                        },
                                                    },
                                                },
                                            },
                                        },
                                        "hosts": {"h3": {"pkgs": ["d"]}},
                                    },
                                },
                                "hosts": {"h4": {}, "h5": {}},
                            },
                        },
                    },
                },
            },
            "b2": {
                "vars": {"cfg": {"x": 2}},
                "children": {"dev": {"children": {"dc9": {"hosts": {"h6": {}, "h7": {"drv": "550.90"}}}}}},
            },
        },
    },
}
HOSTS = ["h1", "h2", "h3", "h4", "h5", "h6", "h7"]
KEYS = ["pkgs", "lst", "cfg", "drv"]


@pytest.fixture()
def deep(session, editor):
    r = editor.post("/api/v1/import", json={"tree": DEEP}, if_match=current_rev(editor))
    assert r.status_code == 201, r.text
    return r.json()["revision"]


def _where(client: AuthClient, key: str, **params):
    r = client.get("/api/v1/vars", params={"key": key, **params})
    assert r.status_code == 200, r.text
    return r.json()


def _hosts(client: AuthClient, key: str, **params):
    r = client.get("/api/v1/vars/hosts", params={"key": key, **params})
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------------ where set


def test_where_set_lists_each_node_that_sets_the_key_itself(deep, viewer):
    out = _where(viewer, "drv")
    assert (out["strategy"], out["declared"], out["revision"], out["total"]) == ("replace", True, deep, 4)
    assert [(n["path"], n["kind"], n["value"]) for n in out["nodes"]] == [
        ("all.b1", "business", "560.35"),
        ("all.b1.prod.dc1.g1", "group", "550.90"),
        ("all.b1.prod.dc1.g1.g2.g3.h1", "host", 550.9),
        ("all.b2.dev.dc9.h7", "host", "550.90"),
    ]
    assert all("effective" not in n and "above_subtree" not in n for n in out["nodes"])
    nope = _where(viewer, "nope")
    assert (nope["total"], nope["nodes"], nope["strategy"], nope["declared"]) == (0, [], "replace", False)


def test_where_set_gives_each_setters_fold_like_the_exporter(deep, viewer, session):
    """``effective`` is the cumulative fold the node emits: the exporter's ``emitted_vars``."""
    snap = load_snapshot(session)
    for key in KEYS:
        for n in _where(viewer, key)["nodes"]:
            emitted = emitted_vars(snap, snap.by_path[n["path"]])[key]
            assert n.get("effective", n["value"]) == emitted, (key, n["path"])
    pkgs = {n["path"]: n for n in _where(viewer, "pkgs")["nodes"]}
    assert pkgs["all.b1.prod.dc1.g1.g2"]["value"] == ["a", "c"]
    assert pkgs["all.b1.prod.dc1.g1.g2"]["effective"] == ["a", "b", "c"]  # unique_append
    lst = {n["path"]: n for n in _where(viewer, "lst")["nodes"]}
    assert lst["all.b1.prod.dc1.g1.g2.g3.h1"]["effective"] == [1, 2, 1, 3]  # append keeps repeats
    cfg = {n["path"]: n for n in _where(viewer, "cfg")["nodes"]}
    assert cfg["all.b1.prod.dc1.g1.g2.g3.h1"]["effective"] == {"x": 9, "n": {"a": 1, "b": 2}}  # deep merge


def test_where_set_subtree_keeps_the_ancestors_that_set_the_key(deep, viewer):
    out = _where(viewer, "drv", subtree="all.b1.prod.dc1.g1.g2")
    assert out["subtree"] == "all.b1.prod.dc1.g1.g2"
    assert [(n["path"], n["above_subtree"]) for n in out["nodes"]] == [
        ("all.b1", True),
        ("all.b1.prod.dc1.g1", True),
        ("all.b1.prod.dc1.g1.g2.g3.h1", False),
    ]
    # a sibling branch whose name is a prefix of another's is not "below" it
    assert [n["path"] for n in _where(viewer, "drv", subtree="all.b2")["nodes"]] == ["all.b2.dev.dc9.h7"]


def test_where_set_value_matches_each_nodes_own_value_as_json(deep, viewer):
    def paths(value):
        return [n["path"] for n in _where(viewer, "drv", value=value)["nodes"]]

    assert paths('"550.90"') == ["all.b1.prod.dc1.g1", "all.b2.dev.dc9.h7"]  # the string
    assert paths("550.90") == ["all.b1.prod.dc1.g1.g2.g3.h1"]  # the number
    assert paths("560.35x") == []  # not JSON: a plain string
    assert [n["path"] for n in _where(viewer, "pkgs", value='["a","c"]')["nodes"]] == ["all.b1.prod.dc1.g1.g2"]  # own, not folded
    assert [n["path"] for n in _where(viewer, "cfg", value='{"n": {"b": 2}}')["nodes"]] == ["all.b1.prod.dc1"]
    assert _where(viewer, "drv", value='"550.90"')["value"] == "550.90"


# --------------------------------------------------------------- host values


def test_host_values_equal_resolve_for_every_host_and_strategy(deep, viewer, session):
    snap = load_snapshot(session)
    for key in KEYS:
        out = _hosts(viewer, key)
        got = {h["host"]: h for h in out["hosts"]}
        for name in HOSTS:
            resolved = resolve(snap, host=name)["resolved"]
            if key in resolved:
                assert got[name]["value"] == resolved[key], (key, name)
                assert got[name]["path"] == snap.host(name).path
            else:
                assert name not in got, (key, name)
        assert out["with_key"] + out["without_key"] == len(HOSTS)
        assert out["total"] == out["with_key"] == len(got)
    drv = {h["host"]: h for h in _hosts(viewer, "drv")["hosts"]}
    assert drv["h2"]["from"] == "all.b1.prod.dc1.g1" and "sources" not in drv["h2"]
    assert drv["h1"]["from"] == "all.b1.prod.dc1.g1.g2.g3.h1"
    assert drv["h4"]["from"] == "all.b1"
    pkgs = {h["host"]: h for h in _hosts(viewer, "pkgs")["hosts"]}
    assert pkgs["h3"]["sources"] == ["all", "all.b1", "all.b1.prod.dc1.g1.h3"] and pkgs["h3"]["value"] == ["a", "b", "d"]


def test_host_values_match_the_whole_resolved_value_and_summarize(deep, viewer):
    out = _hosts(viewer, "drv", value='"550.90"')
    assert [h["host"] for h in out["hosts"]] == ["h2", "h3", "h7"]  # sorted by path
    assert (out["total"], out["with_key"], out["without_key"]) == (3, 6, 1)  # h6 has no drv
    assert {(str(v["value"]), v["hosts"]) for v in out["values"]} == {("550.90", 3), ("560.35", 2), ("550.9", 1)}
    assert out["values"][0] == {"value": "550.90", "hosts": 3} and out["values_truncated"] is False
    # a fold variable matches its folded result, not one contribution
    assert [h["host"] for h in _hosts(viewer, "pkgs", value='["a","b","c"]')["hosts"]] == ["h1", "h2"]
    assert _hosts(viewer, "pkgs", value='["a","c"]')["total"] == 0
    assert [h["host"] for h in _hosts(viewer, "cfg", value='{"x": 2, "n": {"a": 1}}')["hosts"]] == ["h6", "h7"]


def test_host_values_in_a_subtree_include_what_ancestors_set(deep, viewer):
    out = _hosts(viewer, "drv", subtree="all.b1.prod.dc1.g1.g2.g3")
    assert [(h["host"], h["value"]) for h in out["hosts"]] == [("h1", 550.9), ("h2", "550.90")]
    assert (out["with_key"], out["without_key"]) == (2, 0)
    host = _hosts(viewer, "pkgs", subtree="all.b1.prod.dc1.g1.h3")  # a host as the subtree
    assert [h["host"] for h in host["hosts"]] == ["h3"]


def test_paging_is_by_path_and_counts_cover_the_whole_scope(deep, viewer):
    pages = [_hosts(viewer, "cfg", limit=3, offset=o) for o in (0, 3, 6)]
    assert [len(p["hosts"]) for p in pages] == [3, 3, 1]
    assert all(p["total"] == 7 for p in pages)
    names = [h["host"] for p in pages for h in p["hosts"]]
    assert names == [h["host"] for h in _hosts(viewer, "cfg")["hosts"]] and len(set(names)) == 7
    nodes = _where(viewer, "drv", limit=1, offset=1)
    assert (nodes["total"], [n["path"] for n in nodes["nodes"]]) == (4, ["all.b1.prod.dc1.g1"])
    assert viewer.get("/api/v1/vars", params={"key": "drv", "limit": 1001}).status_code == 422
    assert viewer.get("/api/v1/vars/hosts", params={"key": "drv", "offset": -1}).status_code == 422


def test_revision_pinning_sees_the_values_of_that_revision(deep, editor, viewer):
    g1 = editor.get("/api/v1/nodes/all.b1.prod.dc1.g1").json()["id"]
    r = editor.put(f"/api/v1/nodes/{g1}/vars/drv", json={"value": "555.00"}, if_match=deep)
    assert r.status_code == 200
    now = r.json()["revision"]
    r = editor.post("/api/v1/nodes", json={"parent_path": "all.b1.prod.dc1.g1", "name": "h8", "kind": "host"}, if_match=now)
    assert r.status_code == 201
    assert _where(viewer, "drv", value='"555.00"')["total"] == 1
    old = _where(viewer, "drv", revision=deep, value='"550.90"')
    assert [n["path"] for n in old["nodes"]] == ["all.b1.prod.dc1.g1", "all.b2.dev.dc9.h7"] and old["revision"] == deep
    assert [h["host"] for h in _hosts(viewer, "drv", value='"555.00"')["hosts"]] == ["h2", "h3", "h8"]
    assert [h["host"] for h in _hosts(viewer, "drv", revision=deep, value='"550.90"')["hosts"]] == ["h2", "h3", "h7"]
    # h8 did not exist yet, and a subtree created later is unknown at the old revision
    assert viewer.get("/api/v1/vars/hosts", params={"key": "drv", "revision": deep, "subtree": "all.b1.prod.dc1.g1.h8"}).status_code == 404


def test_errors_and_permissions(deep, viewer, anon):
    assert viewer.get("/api/v1/vars").status_code == 422  # key is required
    assert viewer.get("/api/v1/vars", params={"key": ""}).status_code == 422
    for url in ("/api/v1/vars", "/api/v1/vars/hosts"):
        assert anon.get(url, params={"key": "drv"}).status_code == 401
        r = viewer.get(url, params={"key": "drv", "subtree": "all.nope"})
        assert r.status_code == 404 and "all.nope" in r.json()["detail"]
        assert viewer.get(url, params={"key": "drv", "revision": 9999}).status_code == 404
    # searching writes nothing
    assert current_rev(viewer) == deep


def test_seed_tree_at_depth_six(seeded, viewer, session):
    """The seed's deepest host (fed-edge01, depth 6) through every seeded key."""
    snap = load_snapshot(session)
    assert max(n.depth for n in snap.nodes.values() if n.kind is NodeKind.host) == 6
    keys = {k for vs in snap.vars_by_node.values() for k in vs}
    hosts = [n.name for n in snap.nodes.values() if n.kind is NodeKind.host]
    for key in sorted(keys):
        got = {h["host"]: h["value"] for h in _hosts(viewer, key)["hosts"]}
        want = {h: resolve(snap, host=h)["resolved"][key] for h in hosts if key in resolve(snap, host=h)["resolved"]}
        assert got == want, key
    assert [n["path"] for n in _where(viewer, "http_port")["nodes"]] == [
        "all.commercial.lab.dc1.web",
        "all.commercial.prod.dc1.web",
        "all.federal.prod.dc2.web",
    ]


# --------------------------------------------------------------- audit by key


def test_audit_by_key_alone_finds_every_nodes_rows_and_the_definition(deep, editor, viewer, session):
    rev = deep
    g1 = editor.get("/api/v1/nodes/all.b1.prod.dc1.g1").json()["id"]
    h6 = editor.get("/api/v1/nodes/all.b2.dev.dc9.h6").json()["id"]
    rev = editor.put(f"/api/v1/nodes/{g1}/vars/drv", json={"value": "555.00"}, if_match=rev).json()["revision"]
    rev = editor.put(f"/api/v1/nodes/{h6}/vars/drv", json={"value": "550.90"}, if_match=rev).json()["revision"]
    rev = editor.put("/api/v1/variables/drv", json={"merge_strategy": "replace", "description": "GPU driver"}, if_match=rev).json()["revision"]
    rev = editor.put(f"/api/v1/nodes/{g1}/vars/other", json={"value": 1}, if_match=rev).json()["revision"]
    rev = editor.delete(f"/api/v1/nodes/{h6}/vars/drv", if_match=rev).json()["revision"]
    # move the group: its later rows have a new id in target_id, the key stays
    rev = editor.post(f"/api/v1/nodes/{g1}/move", json={"name": "g1b"}, if_match=rev).json()["revision"]
    g1b = editor.get("/api/v1/nodes/all.b1.prod.dc1.g1b").json()["id"]
    rev = editor.put(f"/api/v1/nodes/{g1b}/vars/drv", json={"value": "556.00"}, if_match=rev).json()["revision"]

    r = editor.get("/api/v1/audit", params={"key": "drv"})
    assert r.status_code == 200, r.text
    rows = r.json()
    assert [(a["action"], a["target_id"]) for a in rows] == [
        ("variable.set", f"{g1b}/drv"),
        ("variable.unset", f"{h6}/drv"),
        ("definition.set", "drv"),
        ("variable.set", f"{h6}/drv"),
        ("variable.set", f"{g1}/drv"),
    ]
    assert all(a["key"] == "drv" for a in rows)
    # paging newest first, and the other filters combine with it
    assert [a["id"] for a in editor.get("/api/v1/audit", params={"key": "drv", "limit": 2, "before": rows[1]["id"]}).json()] == [rows[2]["id"], rows[3]["id"]]
    assert len(editor.get("/api/v1/audit", params={"key": "drv", "action": "variable.set"}).json()) == 3
    # subtree: the path the row recorded when it was written
    in_b2 = editor.get("/api/v1/audit", params={"key": "drv", "subtree": "all.b2"}).json()
    assert [a["action"] for a in in_b2] == ["variable.unset", "variable.set"]
    in_g1b = editor.get("/api/v1/audit", params={"key": "drv", "subtree": "all.b1.prod.dc1.g1b"}).json()
    assert [a["target_id"] for a in in_g1b] == [f"{g1b}/drv"]
    moved = editor.get("/api/v1/audit", params={"subtree": "all.b1.prod.dc1.g1b"}).json()
    assert {a["action"] for a in moved} == {"node.move", "variable.set"}
    # with node, still that node's rows across the move
    assert len(editor.get("/api/v1/audit", params={"node": "all.b1.prod.dc1.g1b", "key": "drv"}).json()) == 2
    # audit stays editor+
    assert viewer.get("/api/v1/audit", params={"key": "drv"}).status_code == 403


def test_revert_rows_carry_the_key(deep, editor):
    g1 = editor.get("/api/v1/nodes/all.b1.prod.dc1.g1").json()["id"]
    r = editor.put(f"/api/v1/nodes/{g1}/vars/drv", json={"value": "555.00"}, if_match=deep)
    rev = r.json()["revision"]
    r = editor.post(f"/api/v1/revisions/{rev}/revert", json={}, if_match=rev)
    assert r.status_code in (200, 201), r.text
    rows = editor.get("/api/v1/audit", params={"key": "drv"}).json()
    assert [(a["action"], a["after"]["value"]) for a in rows] == [("variable.set", "550.90"), ("variable.set", "555.00")]


def test_0007_backfills_the_key_of_existing_rows(tmp_path):
    url = os.environ.get("AIS_TEST_DATABASE_URL") or f"sqlite:///{tmp_path}/mig.db"
    eng = aisdb.make_engine(url)
    from ais.models import Base

    with eng.begin() as conn:
        Base.metadata.drop_all(conn)
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
    cfg = aisdb.alembic_config()
    with eng.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "0006")
        for target_type, target_id in (("node_variable", "12/gpu/driver"), ("variable_definition", "gpu_driver"), ("node", "12"), ("user", "a@example.com")):
            conn.execute(
                text("INSERT INTO audit_log (ts, actor, action, target_type, target_id, source) VALUES (CURRENT_TIMESTAMP, 't', 'x', :t, :i, 'api')"),
                {"t": target_type, "i": target_id},
            )
    aisdb.init_db(eng)
    with eng.connect() as conn:
        keys = conn.execute(text("SELECT target_type, key FROM audit_log ORDER BY id")).all()
    assert [tuple(k) for k in keys] == [("node_variable", "gpu/driver"), ("variable_definition", "gpu_driver"), ("node", None), ("user", None)]
    eng.dispose()


# ----------------------------------------------------------------------- CLI


def _ais(db, *args: str) -> str:
    env = dict(os.environ, AIS_DATABASE_URL=f"sqlite:///{db}", AIS_SNAPSHOTS="off")
    out = subprocess.run([sys.executable, "-m", "ais.cli", *args], capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stdout + out.stderr
    return out.stdout


def test_cli_vars(tmp_path):
    from tests.conftest import SEED

    db = tmp_path / "ais.db"
    _ais(db, "import", str(SEED))
    out = _ais(db, "vars", "http_port")
    assert out.splitlines() == [
        "http_port (replace, undeclared) at revision 1: set on 3 node(s)",
        "all.commercial.lab.dc1.web\t8081",
        "all.commercial.prod.dc1.web\t8080",
        "all.federal.prod.dc2.web\t443",
    ]
    out = _ais(db, "vars", "http_port", "--hosts", "--value", "443")
    assert out.splitlines()[0] == "http_port (replace, undeclared) at revision 1: 2 host(s) match, 5 have it, 1 do not"
    assert out.splitlines()[2:] == ["fed-edge01\t443\tfrom all.federal.prod.dc2.web", "fed-web01\t443\tfrom all.federal.prod.dc2.web"]
    out = _ais(db, "vars", "packages", "--subtree", "all.commercial.prod.dc1.web", "--limit", "1")
    assert out.splitlines()[1:] == ['all\t["vim", "curl"]\t(emits ["vim", "curl"])\t(above the subtree)']
    import json

    doc = json.loads(_ais(db, "vars", "packages", "--hosts", "--json", "--subtree", "all.federal"))
    assert {h["host"]: h["value"] for h in doc["hosts"]} == {"fed-edge01": ["vim", "curl", "aide", "haproxy"], "fed-web01": ["vim", "curl", "aide"]}

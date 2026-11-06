"""Profiles (issue 22): named bundles of variables attached to nodes.

The proof first: an estate like the review's (one role subtree repeated in every tier ×
datacenter branch, depth 10), converted from copied role values to one profile attached in
each branch, resolves and exports exactly as before (and through the real plugin); bumping
the driver is one write whose dry run names exactly the hosts that change; its revert puts
everything back. Then the rules one by one: precedence and merge strategies, several
profiles on a node, provenance, pinned revisions, variable search, audit, schema and typo
checks, delete and restore, refusals."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from sqlalchemy import func, select

from ais.models import AuditKey, AuditLog, NodeKind
from ais.resolver import build_export, load_snapshot, resolve
from tests.conftest import ansible_inventory, current_rev

# ------------------------------------------------------------------ the estate

ROLE = {  # what every gpu.a100 group carried before profiles: the copies issue 22 counted
    "gpu_driver": "560.35",
    "node_labels": {"gpu": "a100", "accelerator": "nvidia"},
    "packages": ["nvidia-driver", "cuda-toolkit"],
    "kernel_args": ["iommu=pt"],
}
DEFINITIONS = {
    "node_labels": {"merge_strategy": "merge"},
    "packages": {"merge_strategy": "unique_append"},
    "kernel_args": {"merge_strategy": "append"},
    "gpu_driver": {"merge_strategy": "replace", "json_schema": {"type": "string"}},
}
BRANCHES = [(b, t, dc) for b in ("retail", "gov") for t, dc in (("prod", "use1"), ("staging", "usw2"))]  # 4 tier × dc branches
CANARY = ("gov", "staging", "usw2")  # this branch pins its own driver: own values beat the profile


def _a100(b: str, t: str, dc: str) -> str:
    return f"all.{b}.{t}.{dc}.platform.k8s.cluster-a.workers.gpu.a100"


def _estate(with_role_vars: bool) -> dict[str, Any]:
    """Two businesses, two tier × datacenter branches each; every branch repeats
    ``platform.k8s.cluster-a.workers.gpu.a100`` (hosts at depth 10)."""
    businesses: dict[str, Any] = {}
    for b, t, dc in BRANCHES:
        a100_vars: dict[str, Any] = {"rack": f"{dc}-r7"}
        if with_role_vars:
            a100_vars.update(copy.deepcopy(ROLE))
        if (b, t, dc) == CANARY:
            a100_vars["gpu_driver"] = "565.57"
        hosts = {f"{b}-{t}-{dc}-gpu{i}": {"ansible_host": f"10.{len(b)}.{len(t)}.{i}"} for i in (1, 2)}
        hosts[f"{b}-{t}-{dc}-gpu1"]["kernel_args"] = ["hugepages=16"]
        workers = {"vars": {"node_labels": {"pool": "workers"}, "packages": ["containerd"], "kernel_args": ["quiet"]}, "children": {"gpu": {"children": {"a100": {"vars": a100_vars, "hosts": hosts}}}}}
        tree = {"platform": {"kind": "group", "children": {"k8s": {"children": {"cluster-a": {"children": {"workers": workers}}}}}}}
        biz = businesses.setdefault(b, {"vars": {"packages": ["base"]}, "children": {}})
        biz["children"].setdefault(t, {"children": {}})["children"][dc] = {"vars": {"site": dc}, "children": tree}
    return {"variables": DEFINITIONS, "all": {"vars": {"kernel_args": ["console=ttyS0"]}, "children": businesses}}


def _hosts() -> list[str]:
    return [f"{b}-{t}-{dc}-gpu{i}" for b, t, dc in BRANCHES for i in (1, 2)]


def _plugin(session) -> str:
    session.expire_all()
    data = build_export(load_snapshot(session))
    data.pop("revision")
    return json.dumps(data, sort_keys=True)


def _resolved(session) -> str:
    session.expire_all()
    snap = load_snapshot(session)
    out = {}
    for n in snap.nodes.values():
        if n.kind is NodeKind.host:
            r = resolve(snap, host=n.name)["resolved"]
            r.pop("inventory_revision")
            out[n.name] = r
    return json.dumps(out, sort_keys=True)


def _id(client, path: str) -> int:
    r = client.get(f"/api/v1/nodes/{path}")
    assert r.status_code == 200, (path, r.text)
    return r.json()["id"]


def _convert(editor) -> int:
    """The duplicated role values become one profile, attached in every branch."""
    rev = current_rev(editor)
    r = editor.put("/api/v1/profiles/gpu-a100", json={"description": "A100 GPU workers", "vars": ROLE}, if_match=rev)
    assert r.status_code == 200, r.text
    rev = r.json()["revision"]
    for branch in BRANCHES:
        node = _id(editor, _a100(*branch))
        r = editor.put(f"/api/v1/nodes/{node}/profiles/gpu-a100", json={}, if_match=rev)
        assert r.status_code == 200 and r.json()["profiles"] == ["gpu-a100"], r.text
        rev = r.json()["revision"]
        for key in ROLE:
            if key == "gpu_driver" and branch == CANARY:
                continue  # the canary's own pin stays
            r = editor.delete(f"/api/v1/nodes/{node}/vars/{key}", if_match=rev)
            assert r.status_code == 200, r.text
            rev = r.json()["revision"]
    return rev


@pytest.fixture()
def estate(session, editor):
    r = editor.post("/api/v1/import", json={"tree": _estate(with_role_vars=True)}, if_match=current_rev(editor))
    assert r.status_code == 201, r.text
    return r.json()["revision"]


def test_proof_one_profile_replaces_the_copies(editor, session, estate):
    # (a) every host's resolved vars and the plugin export are identical before and after
    plugin, resolved = _plugin(session), _resolved(session)
    rev = _convert(editor)
    assert _resolved(session) == resolved
    assert _plugin(session) == plugin
    # ... and identical to an estate that never had the copies, plus the profile
    copies = sum(1 for b in BRANCHES for k in ROLE if not (k == "gpu_driver" and b == CANARY))
    assert copies == 15  # the 15 copied assignments are now 4 role values in one place
    vars_left = editor.get("/api/v1/vars", params={"key": "gpu_driver"}).json()
    own = [n for n in vars_left["nodes"] if "profile" not in n]
    assert [n["path"] for n in own] == [_a100(*CANARY)]
    assert {n["source"] for n in vars_left["nodes"] if "profile" in n} == {f"profile:gpu-a100 @ {_a100(*b)}" for b in BRANCHES}

    # (b) bumping the driver is one write; its dry run lists exactly the hosts that change
    expected = sorted(h for h in _hosts() if not h.startswith("-".join(CANARY)))
    dry = editor.put("/api/v1/profiles/gpu-a100/vars/gpu_driver", json={"value": "570.86"}, params={"dry_run": "true"})
    assert dry.status_code == 200, dry.text
    impact = dry.json()["impact"]
    assert sorted(h["host"] for h in impact["hosts"]) == expected and impact["total"] == 6
    assert all(h["changed"] == {"gpu_driver": {"from": "560.35", "to": "570.86"}} for h in impact["hosts"])
    assert current_rev(editor) == rev
    before_bump = (_plugin(session), _resolved(session))
    r = editor.put("/api/v1/profiles/gpu-a100/vars/gpu_driver", json={"value": "570.86"}, if_match=rev)
    assert r.status_code == 200 and r.json()["revision"] == rev + 1
    bumped = r.json()["revision"]
    rows = editor.get("/api/v1/audit", params={"revision": bumped}).json()
    assert [(row["action"], row["target_id"]) for row in rows] == [("profile.update", "gpu-a100")]  # audited once
    hosts = editor.get("/api/v1/vars/hosts", params={"key": "gpu_driver", "value": '"570.86"'}).json()
    assert sorted(h["host"] for h in hosts["hosts"]) == expected and {h["profile"] for h in hosts["hosts"]} == {"gpu-a100"}

    # (c) the revert round-trips, and reverting the revert redoes the bump
    after_bump = (_plugin(session), _resolved(session))
    r = editor.post(f"/api/v1/revisions/{bumped}/revert", json={}, if_match=bumped)
    assert r.status_code == 201, r.text
    assert (_plugin(session), _resolved(session)) == before_bump
    back = r.json()["revision"]
    assert editor.post(f"/api/v1/revisions/{back}/revert", json={}, if_match=back).status_code == 201
    assert (_plugin(session), _resolved(session)) == after_bump


def test_proof_through_the_real_plugin(editor, session, server, inventory_file):
    """The parity rule on the converted estate: ``ansible-inventory --host`` equals the
    server's render for every host (the plugin never sees a profile)."""
    r = editor.post("/api/v1/import", json={"tree": _estate(with_role_vars=True)}, if_match=current_rev(editor))
    assert r.status_code == 201, r.text
    _convert(editor)
    # a second profile on the same node, below the first, and one higher up with fold keys
    rev = editor.put("/api/v1/profiles/k8s-node", json={"vars": {"node_labels": {"gpu": "none", "k8s": "yes"}, "kernel_args": ["k8s"], "kubelet_max_pods": 110}}, if_match=current_rev(editor)).json()["revision"]
    node = _id(editor, _a100(*BRANCHES[0]))
    rev = editor.put(f"/api/v1/nodes/{node}/profiles/k8s-node", json={"position": 0}, if_match=rev).json()["revision"]
    workers = _id(editor, _a100(*BRANCHES[1]).rsplit(".", 2)[0])
    editor.put(f"/api/v1/nodes/{workers}/profiles/k8s-node", json={}, if_match=rev)
    session.expire_all()
    snap = load_snapshot(session)
    for host in _hosts():
        ours = resolve(snap, host=host)["resolved"]
        assert ansible_inventory(inventory_file, "--host", host) == ours, host
    first = resolve(snap, host=_hosts()[0])["resolved"]
    assert first["node_labels"] == {"gpu": "a100", "accelerator": "nvidia", "k8s": "yes", "pool": "workers"}  # gpu-a100 beats k8s-node
    assert first["kernel_args"] == ["console=ttyS0", "quiet", "k8s", "iommu=pt", "hugepages=16"]


# ------------------------------------------------------------------- the rules


def _put(editor, url: str, body: Any, **kw) -> int:
    r = editor.put(url, json=body, if_match=current_rev(editor), **kw)
    assert r.status_code in (200, 201), r.text
    return r.json()["revision"]


def test_precedence_strategies_and_order(editor, session, seeded):
    web = _id(editor, "all.commercial.prod.dc1.web")
    _put(editor, "/api/v1/profiles/a", {"vars": {"http_port": 1, "log_level": "a", "packages": ["pa"], "sysctl": {"vm.swappiness": 30, "a": 1}}})
    _put(editor, "/api/v1/profiles/b", {"vars": {"log_level": "b", "packages": ["pb", "pa"], "sysctl": {"a": 2}}})
    _put(editor, f"/api/v1/nodes/{web}/profiles", {"profiles": ["a", "b"]})
    session.expire_all()
    out = resolve(load_snapshot(session), host="web01")
    v = out["resolved"]
    assert v["http_port"] == 8080  # the node's own value beats its profiles
    assert v["log_level"] == "b"  # later profile beats earlier; both beat prod's "warning"
    assert v["packages"] == ["vim", "curl", "awscli", "pa", "pb", "nginx"]  # unique_append: a, b, then own
    assert v["sysctl"]["vm.swappiness"] == 30 and v["sysctl"]["a"] == 2 and "net.core.somaxconn" not in v["sysctl"]  # web-baseline went: the list is exact
    prov = out["provenance"]["log_level"]
    assert [(p["path"], p.get("profile")) for p in prov] == [("all.commercial.prod", None), ("all.commercial.prod.dc1.web", "a"), ("all.commercial.prod.dc1.web", "b")]
    assert prov[-1]["source"] == "profile:b @ all.commercial.prod.dc1.web"
    render = editor.post("/api/v1/render", json={"host": "web01", "template": False}).json()
    assert render["profiles"] == {"all.commercial.prod.dc1.web": ["a", "b"]}
    assert editor.get("/api/v1/nodes/all.commercial.prod.dc1.web").json()["profiles"] == ["a", "b"]


def test_pinned_revisions_see_the_profile_as_it_was(editor, seeded):
    first = current_rev(editor)
    bumped = _put(editor, "/api/v1/profiles/web-baseline/vars/nginx_worker_processes", {"value": 16})
    assert editor.post("/api/v1/render", json={"host": "web01", "revision": first, "template": False}).json()["resolved"]["nginx_worker_processes"] == 4
    assert editor.post("/api/v1/render", json={"host": "web01", "template": False}).json()["resolved"]["nginx_worker_processes"] == 16
    old = editor.get("/api/v1/profiles/web-baseline", params={"revision": first}).json()
    assert old["vars"]["nginx_worker_processes"] == 4 and old["revision"] == first
    assert [a["path"] for a in old["attached"]] == ["all.commercial.prod.dc1.web", "all.federal.prod.dc2.web"]
    _put(editor, "/api/v1/profiles/web-baseline", {"description": "renamed description"})
    assert editor.get("/api/v1/profiles/web-baseline", params={"revision": bumped}).json()["description"] == "Settings every web tier shares"
    export = editor.get("/api/v1/export", params={"revision": first}).json()
    assert export["groups"]["commercial_prod_dc1_web"]["vars"]["nginx_worker_processes"] == 4


def test_list_and_get(editor, viewer, seeded):
    rows = viewer.get("/api/v1/profiles").json()
    assert rows == [{"name": "web-baseline", "description": "Settings every web tier shares", "keys": ["nginx_worker_processes", "sysctl"], "attached": 2}]
    assert viewer.get("/api/v1/profiles/nope").status_code == 404
    assert viewer.put("/api/v1/profiles/x", json={}, if_match=current_rev(viewer)).status_code == 403


def test_refusals(editor, seeded):
    rev = current_rev(editor)
    web01 = _id(editor, "all.commercial.prod.dc1.web.web01")
    web = _id(editor, "all.commercial.prod.dc1.web")
    assert editor.put("/api/v1/profiles/bad.name", json={}, if_match=rev).status_code == 422
    assert editor.put("/api/v1/profiles/x", json={}).status_code == 428
    assert editor.put("/api/v1/profiles/x", json={}, if_match=rev - 1).status_code == 409
    r = editor.put(f"/api/v1/nodes/{web01}/profiles/web-baseline", json={}, if_match=rev)
    assert r.status_code == 422 and "host" in r.json()["detail"]
    assert editor.put(f"/api/v1/nodes/{web}/profiles", json={"profiles": ["web-baseline", "web-baseline"]}, if_match=rev).status_code == 422
    assert editor.put(f"/api/v1/nodes/{web}/profiles", json={"profiles": ["nope"]}, if_match=rev).status_code == 404
    assert editor.delete(f"/api/v1/nodes/{web}/profiles/nope", if_match=rev).status_code == 404
    r = editor.delete("/api/v1/profiles/web-baseline", if_match=rev)
    assert r.status_code == 409 and r.json()["detail"]["nodes"] == ["all.commercial.prod.dc1.web", "all.federal.prod.dc2.web"]
    assert current_rev(editor) == rev
    # idempotent re-puts: no revision
    assert editor.put(f"/api/v1/nodes/{web}/profiles/web-baseline", json={}).json()["changed"] is False
    assert editor.put("/api/v1/profiles/web-baseline/vars/nginx_worker_processes", json={"value": 4}).json()["changed"] is False
    r = editor.delete("/api/v1/profiles/web-baseline", params={"detach": "true"}, if_match=rev)
    assert r.status_code == 200 and r.json()["detached"] == ["all.commercial.prod.dc1.web", "all.federal.prod.dc2.web"]
    assert editor.get("/api/v1/nodes/all.commercial.prod.dc1.web").json()["profiles"] == []


def test_schema_applies_to_profile_values(editor, seeded):
    _put(editor, "/api/v1/variables/nginx_worker_processes", {"json_schema": {"type": "integer", "minimum": 1}})
    rev = current_rev(editor)
    r = editor.put("/api/v1/profiles/web-baseline/vars/nginx_worker_processes", json={"value": 0}, if_match=rev)
    assert r.status_code == 422 and "profile:web-baseline" in r.json()["detail"]
    # a definition that a profile's live value would break is refused, like a node's
    r = editor.put("/api/v1/variables/nginx_worker_processes", json={"json_schema": {"type": "integer", "maximum": 2}}, if_match=rev)
    assert r.status_code == 422 and "profile:web-baseline" in r.json()["detail"]
    # an import's profile values too
    tree = {"profiles": {"other": {"vars": {"nginx_worker_processes": "many"}}}, "all": {}}
    assert editor.post("/api/v1/import", json={"tree": tree}, if_match=rev).status_code == 422


def test_typo_warnings_and_strict_mode(editor, seeded, monkeypatch):
    r = editor.put("/api/v1/profiles/web-baseline/vars/sysctll", json={"value": {}}, if_match=current_rev(editor))
    assert r.status_code == 200
    assert [(w["key"], w["path"], w["did_you_mean"]) for w in r.json()["warnings"]] == [("sysctll", "profile:web-baseline", "sysctl")]
    monkeypatch.setenv("AIS_STRICT_KEYS", "true")
    r = editor.put("/api/v1/profiles/web-baseline/vars/brand_new", json={"value": 1}, if_match=current_rev(editor))
    assert r.status_code == 422 and r.json()["detail"]["keys"][0]["paths"] == ["profile:web-baseline"]
    # a key a profile already holds is not new: a node may use it in strict mode
    web = _id(editor, "all.commercial.prod.dc1.web")
    assert editor.put(f"/api/v1/nodes/{web}/vars/nginx_worker_processes", json={"value": 2}, if_match=current_rev(editor)).status_code == 200


def test_audit_by_key_and_by_node(editor, session, seeded):
    rev = _put(editor, "/api/v1/profiles/web-baseline", {"vars": {"nginx_worker_processes": 2, "sysctl": {"net.core.somaxconn": 4096}, "gzip": True}})
    session.expire_all()
    row = session.scalar(select(AuditLog).where(AuditLog.revision == rev))
    assert row.action == "profile.update" and set(session.scalars(select(AuditKey.key).where(AuditKey.audit_id == row.id))) == {"gzip", "nginx_worker_processes"}
    assert [r["revision"] for r in editor.get("/api/v1/audit", params={"key": "gzip"}).json()] == [rev]
    assert editor.get("/api/v1/audit", params={"key": "sysctl", "action": "profile."}).json() == []  # unchanged keys are not listed
    web = _id(editor, "all.commercial.prod.dc1.web")
    rev = _put(editor, f"/api/v1/nodes/{web}/profiles/web-baseline", {"position": 0})  # already there, at 0: no-op
    rev = editor.delete(f"/api/v1/nodes/{web}/profiles/web-baseline", if_match=current_rev(editor)).json()["revision"]
    rows = editor.get("/api/v1/audit", params={"node": "all.commercial.prod.dc1.web"}).json()
    assert rows[0]["action"] == "profile.detach" and rows[0]["before"] == {"path": "all.commercial.prod.dc1.web", "profiles": ["web-baseline"]}


def test_variable_search_reports_profile_sources(editor, viewer, seeded):
    found = viewer.get("/api/v1/vars", params={"key": "nginx_worker_processes"}).json()
    assert [(n["path"], n.get("profile")) for n in found["nodes"]] == [("all.commercial.prod.dc1.web", "web-baseline"), ("all.federal.prod.dc2.web", "web-baseline")]
    assert found["profiles"] == [{"name": "web-baseline", "value": 4}]
    sysctl = viewer.get("/api/v1/vars", params={"key": "sysctl", "subtree": "all.commercial.prod.dc1"}).json()
    web = next(n for n in sysctl["nodes"] if n.get("profile"))
    assert web["effective"] == {"net.ipv4.ip_forward": 0, "vm.swappiness": 10, "net.core.somaxconn": 4096}
    hosts = viewer.get("/api/v1/vars/hosts", params={"key": "sysctl"}).json()
    web01 = next(h for h in hosts["hosts"] if h["host"] == "web01")
    assert web01["sources"] == ["all", "all.commercial.prod", "profile:web-baseline @ all.commercial.prod.dc1.web"]
    assert web01["profile"] == "web-baseline"


def test_varsearch_matches_resolve_with_profiles(editor, session, estate):
    _convert(editor)
    session.expire_all()
    snap = load_snapshot(session)
    for key in ROLE:
        hosts = editor.get("/api/v1/vars/hosts", params={"key": key}).json()["hosts"]
        for h in hosts:
            assert h["value"] == resolve(snap, host=h["host"])["resolved"][key], (key, h)
        assert len(hosts) == 8


def test_restore_reattaches_and_never_rolls_a_profile_back(editor, session, seeded):
    path = "all.commercial.prod.dc1"
    rev = editor.delete(f"/api/v1/nodes/{_id(editor, path)}", params={"recursive": "true"}, if_match=current_rev(editor)).json()["revision"]
    rev = _put(editor, "/api/v1/profiles/web-baseline/vars/nginx_worker_processes", {"value": 12})
    r = editor.post("/api/v1/nodes/restore", json={"path": path}, if_match=rev)
    assert r.status_code == 201, r.text
    assert editor.get("/api/v1/nodes/all.commercial.prod.dc1.web").json()["profiles"] == ["web-baseline"]
    assert editor.post("/api/v1/render", json={"host": "web01", "template": False}).json()["resolved"]["nginx_worker_processes"] == 12  # today's value
    # a profile deleted since: refused, nothing written
    rev = editor.delete(f"/api/v1/nodes/{_id(editor, path)}", params={"recursive": "true"}, if_match=r.json()["revision"]).json()["revision"]
    rev = editor.delete("/api/v1/profiles/web-baseline", params={"detach": "true"}, if_match=rev).json()["revision"]
    r = editor.post("/api/v1/nodes/restore", json={"path": path}, if_match=rev)
    assert r.status_code == 409 and "web-baseline" in r.json()["detail"] and current_rev(editor) == rev


def test_import_is_additive_for_profile_values_and_exact_for_lists(editor, seeded):
    tree = {
        "profiles": {"web-baseline": {"vars": {"gzip": True}}, "edge": {"description": "edge", "vars": {"tls": "1.3"}}},
        "all": {"children": {"federal": {"children": {"prod": {"children": {"dc2": {"children": {"web": {"profiles": ["edge", "web-baseline"]}}}}}}}}},
    }
    r = editor.post("/api/v1/import", json={"tree": tree}, if_match=current_rev(editor))
    assert r.status_code == 201 and r.json()["profiles"] == ["edge", "web-baseline"] and r.json()["attachments"] == 1
    assert editor.get("/api/v1/profiles/web-baseline").json()["vars"] == {"gzip": True, "nginx_worker_processes": 4, "sysctl": {"net.core.somaxconn": 4096}}
    assert editor.get("/api/v1/nodes/all.federal.prod.dc2.web").json()["profiles"] == ["edge", "web-baseline"]
    again = editor.post("/api/v1/import", json={"tree": tree}, if_match=r.json()["revision"])
    assert again.json()["changed"] is False
    # an attachment to an unknown profile, profiles on a host entry's level are refused
    bad = {"all": {"children": {"federal": {"profiles": ["missing"]}}}}
    assert editor.post("/api/v1/import", json={"tree": bad}, if_match=r.json()["revision"]).status_code == 422


def test_audit_rows_are_one_per_write(editor, session, seeded):
    before = session.scalar(select(func.count()).select_from(AuditLog))
    _put(editor, "/api/v1/profiles/p1", {"vars": {"a": 1, "b": 2}})
    _put(editor, f"/api/v1/nodes/{_id(editor, 'all.federal')}/profiles", {"profiles": ["p1"]})
    editor.delete("/api/v1/profiles/p1", params={"detach": "true"}, if_match=current_rev(editor))
    session.expire_all()
    rows = list(session.scalars(select(AuditLog.action).where(AuditLog.id > 0).order_by(AuditLog.id)))[before:]
    assert rows == ["profile.create", "profile.attach", "profile.delete"]

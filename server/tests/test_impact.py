"""Blast-radius preview (issue #25): every write route's ``dry_run`` reports ``impact``.

The oracle is independent of ``ais.impact``: the full export and ``resolve`` for every host,
before and after the real write. Each case runs the dry run first (no ``If-Match``) and
checks that it wrote nothing, then the real write, then compares the dry run's ``impact``
with what actually changed, host by host: status, path, every resolved key (not only the
keys the preview chose to look at) and every group."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ais import impact
from ais.models import AuditLog
from ais.resolver import build_export, load_snapshot, resolve
from ais.varsearch import canonical
from tests.conftest import current_rev

DC1 = "all.commercial.prod.dc1"
WEB = f"{DC1}.web"


def _id(client, path: str) -> int:
    r = client.get(f"/api/v1/nodes/{path}")
    assert r.status_code == 200, (path, r.text)
    return r.json()["id"]


def _world(engine) -> dict[str, tuple[str, dict[str, Any], set[str]]]:
    """host -> (path, resolved vars, groups) from the exporter and the resolver."""
    with Session(engine) as s:
        snap = load_snapshot(s)
        exp = build_export(snap)
        out = {}
        for name, h in exp["hosts"].items():
            resolved = dict(resolve(snap, host=name)["resolved"])
            resolved.pop("inventory_revision")
            groups = {g for g, d in exp["groups"].items() if g != "all" and h["path"].startswith(d["path"] + ".")}
            groups |= {t for t, members in exp["tags"].items() if name in members}
            out[name] = (h["path"], resolved, groups)
        return out


def _expected(before, after) -> dict[str, dict[str, Any]]:
    """What the impact must list, keyed by host."""
    out: dict[str, dict[str, Any]] = {}
    for name in set(before) | set(after):
        if name not in after:
            out[name] = {"host": name, "path": before[name][0], "status": "removed"}
            continue
        if name not in before:
            out[name] = {"host": name, "path": after[name][0], "status": "added"}
            continue
        (bp, bv, bg), (ap, av, ag) = before[name], after[name]
        entry: dict[str, Any] = {
            "host": name,
            "path": ap,
            "status": "changed",
            "added": {k: v for k, v in av.items() if k not in bv},
            "removed": {k: v for k, v in bv.items() if k not in av},
            "changed": {k: {"from": bv[k], "to": v} for k, v in av.items() if k in bv and canonical(bv[k]) != canonical(v)},
        }
        if ag != bg:
            entry["joined"], entry["left"] = sorted(ag - bg), sorted(bg - ag)
        if ap != bp:
            entry["moved_from"] = bp
        if entry["added"] or entry["removed"] or entry["changed"] or ag != bg or ap != bp:
            out[name] = entry
    return out


def _audits(engine) -> int:
    with Session(engine) as s:
        return s.scalar(select(func.count()).select_from(AuditLog))


def _call(client, method: str, url: str, body: Any, **kw):
    if method in ("PUT", "POST", "PATCH"):
        return getattr(client, method.lower())(url, json=body, **kw)
    return client.delete(url, **kw)


def _check(editor, engine, method: str, url: str, body: Any = None, *, query: dict | None = None, in_body: bool = False) -> dict[str, Any]:
    """Dry run, prove it wrote nothing, write for real, compare. Returns the impact."""
    rev = current_rev(editor)
    world = _world(engine)
    audits = _audits(engine)
    params = dict(query or {})
    if in_body:
        dry = _call(editor, method, url, {**body, "dry_run": True}, params=params)
    else:
        dry = _call(editor, method, url, body, params={**params, "dry_run": "true"})
    assert dry.status_code == 200, dry.text
    d = dry.json()
    assert d["dry_run"] is True and d["changed"] is True and d["revision"] == rev and dry.headers["ETag"] == f'"{rev}"'
    assert current_rev(editor) == rev and _audits(engine) == audits and _world(engine) == world  # wrote nothing

    real = _call(editor, method, url, body, params=params, if_match=rev)
    assert real.status_code in (200, 201), real.text
    r = real.json()
    legacy = {"hosts", "hosts_changed"} if url.endswith("/revert") else set()  # revert's pre-impact preview fields
    assert r["dry_run"] is False and set(d) == set(r) | {"impact"} | legacy  # same body, plus impact
    after = _world(engine)

    found = d["impact"]
    expected = _expected(world, after)
    got = {h["host"]: h for h in found["hosts"]}
    assert got == expected
    assert found["total"] == len(expected) and not found["truncated"]
    assert [h["path"] for h in found["hosts"]] == sorted(h["path"] for h in found["hosts"])
    counts = {
        "added": sum(e["status"] == "added" for e in expected.values()),
        "removed": sum(e["status"] == "removed" for e in expected.values()),
        "vars_changed": sum(bool(e.get("added") or e.get("removed") or e.get("changed")) for e in expected.values()),
        "regrouped": sum("joined" in e for e in expected.values()),
        "moved": sum("moved_from" in e for e in expected.values()),
    }
    assert {k: found[k] for k in counts} == counts
    joined: dict[str, int] = {}
    left: dict[str, int] = {}
    for name, e in expected.items():
        came = e.get("joined", []) if e["status"] == "changed" else sorted(after[name][2]) if e["status"] == "added" else []
        went = e.get("left", []) if e["status"] == "changed" else sorted(world[name][2]) if e["status"] == "removed" else []
        for g in came:
            joined[g] = joined.get(g, 0) + 1
        for g in went:
            left[g] = left.get(g, 0) + 1
    assert found["groups"] == [{"group": g, "joined": joined.get(g, 0), "left": left.get(g, 0)} for g in sorted(set(joined) | set(left))]
    assert found["groups_total"] == len(found["groups"])
    return found


# ------------------------------------------------------------------- values


def test_put_var_replace_at_a_group(editor, engine, seeded):
    found = _check(editor, engine, "PUT", f"/api/v1/nodes/{_id(editor, WEB)}/vars/http_port", {"value": 9000})
    assert found["total"] == 2 and found["hosts"][0]["changed"] == {"http_port": {"from": 8080, "to": 9000}}


def test_put_var_at_the_root_reaches_every_host_that_inherits_it(editor, engine, seeded):
    found = _check(editor, engine, "PUT", f"/api/v1/nodes/{_id(editor, 'all')}/vars/ntp_server", {"value": "ntp.example.com"})
    assert found["total"] == len(_world(engine)) == 6


def test_put_fold_var(editor, engine, seeded):
    found = _check(editor, engine, "PUT", f"/api/v1/nodes/{_id(editor, 'all.commercial')}/vars/packages", {"value": ["awscli", "jq"]})
    assert found["vars_changed"] == 4  # every commercial host: the fold carries it down


def test_put_new_key_and_shadowed_key(editor, engine, seeded):
    _check(editor, engine, "PUT", f"/api/v1/nodes/{_id(editor, DC1)}/vars/rack", {"value": "r1"})
    # web01/web02 set http_port at web, so a value at dc1 changes nobody there
    found = _check(editor, engine, "PUT", f"/api/v1/nodes/{_id(editor, DC1)}/vars/http_port", {"value": 1})
    assert {h["host"] for h in found["hosts"]} == {"db01"}


def test_delete_var_and_host_var(editor, engine, seeded):
    _check(editor, engine, "DELETE", f"/api/v1/nodes/{_id(editor, DC1)}/vars/dns_servers")
    found = _check(editor, engine, "PUT", f"/api/v1/nodes/{_id(editor, WEB + '.web01')}/vars/ansible_host", {"value": "10.1.1.99"})
    assert found["total"] == 1


# ---------------------------------------------------------------- structure


def test_create_host_and_group(editor, engine, seeded):
    found = _check(editor, engine, "POST", "/api/v1/nodes", {"parent_path": WEB, "name": "web03", "kind": "host", "vars": {"ansible_host": "10.1.1.13"}})
    assert (found["added"], found["hosts"][0]["host"]) == (1, "web03")
    found = _check(editor, engine, "POST", "/api/v1/nodes", {"parent_path": DC1, "name": "cache", "vars": {"http_port": 6379}})
    assert found["total"] == 0  # a new group holds no host yet


def test_create_dry_run_nulls_the_ids_it_would_allocate(editor, seeded):
    r = editor.post("/api/v1/nodes", json={"parent_path": WEB, "name": "web09", "kind": "host"}, params={"dry_run": "true"})
    assert r.status_code == 200 and r.json()["node"]["id"] is None and r.json()["node"]["path"] == f"{WEB}.web09"


def test_patch_inventory_name_regroups_and_meta_changes_nobody(editor, engine, seeded):
    found = _check(editor, engine, "PATCH", f"/api/v1/nodes/{_id(editor, DC1)}", {"inventory_name": "dc1_commercial"})
    assert found["regrouped"] == 3 and {"group": "dc1_commercial", "joined": 3, "left": 0} in found["groups"]
    found = _check(editor, engine, "PATCH", f"/api/v1/nodes/{_id(editor, DC1)}", {"meta": {"owner": "team-a"}})
    assert found["total"] == 0


def test_move_with_keys_on_both_old_and_new_ancestors(editor, engine, seeded):
    found = _check(editor, engine, "POST", f"/api/v1/nodes/{_id(editor, WEB)}/move", {"parent_path": "all.federal.prod.dc2", "name": "web2"})
    web01 = next(h for h in found["hosts"] if h["host"] == "web01")
    assert web01["moved_from"] == f"{WEB}.web01" and "dns_servers" in web01["changed"] and "packages" in web01["changed"]
    assert found["moved"] == 2 and found["regrouped"] == 2


def test_move_dry_run_nulls_new_ids(editor, seeded):
    r = editor.post(f"/api/v1/nodes/{_id(editor, WEB)}/move", json={"name": "www"}, params={"dry_run": "true"})
    assert r.status_code == 200 and {m["id"] for m in r.json()["moved"]} == {None} and r.json()["moved"][0]["old_id"]


def test_delete_and_restore(editor, engine, seeded):
    found = _check(editor, engine, "DELETE", f"/api/v1/nodes/{_id(editor, DC1)}", query={"recursive": "true"})
    assert (found["removed"], found["total"]) == (3, 3)
    found = _check(editor, engine, "POST", "/api/v1/nodes/restore", {"path": DC1}, in_body=True)
    assert found["added"] == 3


# -------------------------------------------------------------- definitions


def test_strategy_change_reaches_the_hosts_with_more_than_one_contributor(editor, engine, seeded):
    found = _check(editor, engine, "PUT", "/api/v1/variables/packages", {"merge_strategy": "replace"})
    assert found["vars_changed"] == 6
    _check(editor, engine, "DELETE", "/api/v1/variables/sysctl")
    _check(editor, engine, "PUT", "/api/v1/variables/dns_servers", {"merge_strategy": "append"})


def test_schema_only_change_changes_no_host(editor, engine, seeded):
    found = _check(editor, engine, "PUT", "/api/v1/variables/http_port", {"json_schema": {"type": "integer"}})
    assert found == impact.empty()


# --------------------------------------------------------------------- tags


def test_tag_writes(editor, engine, seeded):
    found = _check(editor, engine, "PUT", "/api/v1/tags/canary", {"members": [WEB, "all.federal.prod.dc2.web.edge"]})
    assert found["regrouped"] == 3 and found["groups"] == [{"group": "canary", "joined": 3, "left": 0}]
    found = _check(editor, engine, "PUT", "/api/v1/tags/canary", {"members": [f"{WEB}.web01"]})
    assert found["groups"] == [{"group": "canary", "joined": 0, "left": 2}]
    _check(editor, engine, "DELETE", "/api/v1/tags/canary")


# ---------------------------------------------------- import, revert, cap


def test_import_and_revert(editor, engine, seeded):
    tree = {"all": {"children": {"commercial": {"vars": {"log_level": "error"}, "children": {"prod": {"children": {"dc1": {"children": {"web": {"vars": {"http_port": 8090}, "hosts": {"web05": {}}}}}}}}}}}}
    found = _check(editor, engine, "POST", "/api/v1/import", {"tree": tree}, in_body=True)
    assert found["added"] == 1 and found["vars_changed"] == 2  # web01, web02; prod and lab shadow commercial's log_level
    moved = editor.post(f"/api/v1/nodes/{_id(editor, WEB)}/move", json={"name": "frontend"}, if_match=current_rev(editor)).json()["revision"]
    editor.put(f"/api/v1/nodes/{_id(editor, DC1 + '.frontend')}/vars/http_port", json={"value": 1}, if_match=moved)
    _check(editor, engine, "POST", f"/api/v1/revisions/{moved}/revert", {"force": True}, in_body=True)


def test_cap_lists_200_and_counts_every_host(editor, engine, seeded):
    hosts = {f"bulk{i:03d}": {} for i in range(250)}
    tree = {"all": {"children": {"commercial": {"children": {"prod": {"children": {"dc1": {"children": {"bulk": {"hosts": hosts}}}}}}}}}}
    rev = editor.post("/api/v1/import", json={"tree": tree}, if_match=current_rev(editor)).json()["revision"]
    r = editor.put(f"/api/v1/nodes/{_id(editor, DC1 + '.bulk')}/vars/http_port", json={"value": 1}, params={"dry_run": "true"})
    found = r.json()["impact"]
    assert (found["total"], found["vars_changed"], len(found["hosts"]), found["truncated"], found["limit"]) == (250, 250, 200, True, 200)
    assert current_rev(editor) == rev


# --------------------------------------------------------- validation parity


def test_dry_run_refuses_exactly_what_the_write_refuses(editor, engine, seeded, monkeypatch):
    rev = current_rev(editor)
    web = _id(editor, WEB)

    def both(method: str, url: str, body: Any = None, **params):
        dry = _call(editor, method, url, body, params={**params, "dry_run": "true"})
        real = _call(editor, method, url, body, params=params, if_match=rev)
        assert dry.status_code == real.status_code and dry.json()["detail"] == real.json()["detail"], (dry.text, real.text)
        return dry.status_code

    assert both("DELETE", f"/api/v1/nodes/{_id(editor, DC1)}") == 409  # delete guard
    assert both("POST", f"/api/v1/nodes/{web}/move", {"parent_path": "all.commercial.lab.dc1"}) == 409  # path taken
    assert both("PATCH", f"/api/v1/nodes/{_id(editor, DC1)}", {"inventory_name": "commercial_prod"}) == 409  # group name collision
    assert both("DELETE", f"/api/v1/nodes/{web}/vars/nope") == 404
    editor.put("/api/v1/variables/http_port", json={"json_schema": {"type": "integer"}}, if_match=rev)
    rev = current_rev(editor)
    assert both("PUT", f"/api/v1/nodes/{web}/vars/http_port", {"value": "eighty"}) == 422  # schema
    assert both("PUT", "/api/v1/variables/http_port", {"json_schema": {"type": "string"}}) == 422  # live values
    assert both("PUT", "/api/v1/tags/all", {"members": [WEB]}) == 422  # reserved name
    monkeypatch.setenv("AIS_STRICT_KEYS", "1")
    assert both("PUT", f"/api/v1/nodes/{web}/vars/brand_new_key", {"value": 1}) == 422  # strict mode
    assert both("POST", "/api/v1/nodes", {"parent_path": WEB, "name": "web07", "kind": "host", "vars": {"brand_new_key": 1}}) == 422
    assert current_rev(editor) == rev


def test_dry_runs_take_no_revision_id(editor, seeded):
    """A dry run's revision is ``base + 1`` set explicitly under the write lock, so on
    PostgreSQL it uses up no sequence value: the next real write is still ``base + 1``."""
    rev = current_rev(editor)
    web = _id(editor, WEB)
    assert editor.put(f"/api/v1/nodes/{web}/vars/http_port", json={"value": 1}, params={"dry_run": "true"}).status_code == 200
    assert editor.post("/api/v1/import", json={"tree": {"all": {"vars": {"x_dry": 1}}}, "dry_run": True}).status_code == 200
    assert editor.post(f"/api/v1/nodes/{web}/move", json={"name": "www"}, params={"dry_run": "true"}).status_code == 200
    assert editor.delete(f"/api/v1/nodes/{_id(editor, DC1)}", params={"dry_run": "true", "recursive": "true"}).status_code == 200
    r = editor.put(f"/api/v1/nodes/{web}/vars/http_port", json={"value": 1}, if_match=rev)
    assert r.status_code == 200 and r.json()["revision"] == rev + 1
    deleted = editor.delete(f"/api/v1/nodes/{_id(editor, DC1)}", params={"recursive": "true"}, if_match=rev + 1).json()["revision"]
    assert editor.post("/api/v1/nodes/restore", json={"path": DC1, "dry_run": True}).status_code == 200
    assert editor.post(f"/api/v1/revisions/{deleted}/revert", json={"dry_run": True}).status_code == 200
    assert editor.post("/api/v1/nodes/restore", json={"path": DC1}, if_match=deleted).json()["revision"] == deleted + 1


def test_stale_if_match_refuses_a_dry_run(editor, seeded):
    rev = current_rev(editor)
    web = _id(editor, WEB)
    r = editor.put(f"/api/v1/nodes/{web}/vars/http_port", json={"value": 1}, params={"dry_run": "true"}, if_match=rev - 1)
    assert r.status_code == 409 and r.json()["detail"]["error"] == "stale revision"
    r = editor.put(f"/api/v1/nodes/{web}/vars/http_port", json={"value": 1}, params={"dry_run": "true"}, if_match=rev)
    assert r.status_code == 200 and r.json()["revision"] == rev


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("PUT", "/api/v1/variables/http_port", {"merge_strategy": "replace"}),
        ("PUT", "/api/v1/tags/none", {"members": []}),
    ],
)
def test_no_op_dry_run_has_the_same_shape(editor, seeded, method, path, body):
    rev = editor.put(path, json=body, if_match=current_rev(editor)).json()["revision"]
    r = editor.put(path, json=body, params={"dry_run": "true"})
    assert r.status_code == 200 and r.json()["changed"] is False and r.json()["impact"] == impact.empty()
    assert r.json()["revision"] == rev


def test_no_op_var_put_dry_run(editor, seeded):
    web = _id(editor, WEB)
    r = editor.put(f"/api/v1/nodes/{web}/vars/http_port", json={"value": 8080}, params={"dry_run": "true"})
    assert r.status_code == 200 and r.json()["changed"] is False and r.json()["dry_run"] is True and r.json()["impact"]["total"] == 0

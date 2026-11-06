"""Tree export (``ais.tree``, ``GET /api/v1/tree``, ``ais export-tree``) round-trips
through import: whatever is exported, imported into an empty database or back over a
deleted subtree, resolves every host exactly as before.

The oracle is the plugin export (``build_export``, keyed by names and paths, so new node
ids don't matter) plus per-host ``resolve``/``snapshots.diff``. The tree below is built for
it: depth 8, all four merge strategies with ``append`` contributing at five levels (only
``append`` exposes an export of folded instead of own values), JSON Schemas, hyphenated
names, a ``group`` directly under ``all`` (re-imported without ``kind`` it would become a
business), an ``inventory_name``, stored Jinja and YAML-ambiguous strings, and tags."""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
from typing import Any

import pytest
import yaml
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from ais import db as aisdb
from ais import snapshots
from ais.importer import ImportError_, import_tree_report
from ais.models import Node, NodeKind
from ais.resolver import build_export, load_snapshot, resolve
from ais.tree import TreeExportError, dump_yaml, export_tree
from tests.conftest import current_rev

EAST = "all.commercial.prod.dc-east-1"
POD = f"{EAST}.web-tier.app-a.shard-01.pod-x"

ODD_STRINGS = {
    "motd": "{{ inventory_hostname }} in {{ site_code | default('none') }}",
    "guarded": "{{ lookup('pipe', 'id') }}",
    "yes_word": "yes",
    "null_word": "null",
    "empty": "",
    "octal_like": "0o17",
    "float_like": "1e3",
    "date_like": "2026-11-05",
    "multiline": "line one\nline two\n",
    "unicode": "Zürich – 東京",
    "colon": "key: value # not a comment",
}

TREE: dict[str, Any] = {
    "variables": {
        "packages": {"merge_strategy": "unique_append", "description": "OS packages"},
        "sysctl": {"merge_strategy": "merge"},
        "audit_rules": {"merge_strategy": "append", "description": "audit rules, every level adds"},
        "dns_servers": {"merge_strategy": "replace", "json_schema": {"type": "array", "items": {"type": "string"}}},
        "http_port": {"merge_strategy": "replace", "json_schema": {"type": "integer", "minimum": 1, "maximum": 65535}},
        "unused_decl": {"merge_strategy": "replace", "description": "declared, set nowhere"},
    },
    "all": {
        "vars": {
            "ntp_server": "time.example.com",
            "packages": ["vim", "curl"],
            "sysctl": {"vm.swappiness": 60, "net": {"ipv4": {"ip_forward": 0}}},
            "audit_rules": ["base", "base"],
            "dns_servers": ["10.0.0.2"],
            **ODD_STRINGS,
        },
        "children": {
            "ops-tools": {
                "kind": "group",
                "vars": {"audit_rules": ["ops"]},
                "hosts": {"ops-bastion01": {"ansible_host": "10.0.9.1"}},
            },
            "commercial": {
                "vars": {"packages": ["awscli"], "audit_rules": ["biz"]},
                "children": {
                    "prod": {
                        "vars": {"sysctl": {"vm.swappiness": 10}},
                        "children": {
                            "dc-east-1": {
                                "inventory_name": "east1",
                                "vars": {"site_code": "E1", "audit_rules": ["dc"], "dns_servers": ["10.1.0.2", "10.1.0.3"]},
                                "children": {
                                    "web-tier": {
                                        "vars": {"http_port": 8080, "packages": ["nginx"]},
                                        "children": {
                                            "app-a": {
                                                "vars": {"sysctl": {"net": {"ipv4": {"tcp_syncookies": 1}}}},
                                                "children": {
                                                    "shard-01": {
                                                        "vars": {"audit_rules": ["shard", "base"], "sysctl": {"kernel.shmmax": 1}},
                                                        "children": {
                                                            "pod-x": {
                                                                "vars": {"packages": ["nginx", "vim", "jq"], "audit_rules": ["pod"]},
                                                                "hosts": {
                                                                    "web-01": {"ansible_host": "10.1.1.11", "audit_rules": ["host"], "http_port": 8443},
                                                                    "web-02": {},
                                                                },
                                                            }
                                                        },
                                                    }
                                                },
                                            }
                                        },
                                    },
                                },
                                "hosts": {"db-01": {"ansible_host": "10.1.2.11", "packages": ["postgresql-16"]}},
                            },
                            "dc-west": {
                                "vars": {"site_code": "W1"},
                                "children": {"web": {"hosts": {"west-web-01": {"ansible_host": "10.2.1.11"}}}},
                            },
                        },
                    },
                    "lab": {"children": {"dc-lab": {"hosts": {"lab-01": {}}}}},
                },
            },
        },
    },
    "tags": {
        "patch_wave_1": {"description": "first patch wave", "members": [f"{EAST}.web-tier", "all.ops-tools.ops-bastion01"]},
        "canary": {"members": [f"{POD}.web-01"]},
        "west-only": {"members": ["all.commercial.prod.dc-west"]},
    },
}


def _plugin_view(session) -> dict[str, Any]:
    data = build_export(load_snapshot(session))
    data.pop("revision")
    return data


def _resolved(session, revision: int | None = None) -> dict[str, dict[str, Any]]:
    snap = load_snapshot(session, revision)
    out = {}
    for n in snap.nodes.values():
        if n.kind is NodeKind.host:
            r = resolve(snap, host=n.name)["resolved"]
            r.pop("inventory_revision")
            out[n.name] = r
    return out


def _through_yaml(tree: dict[str, Any]) -> dict[str, Any]:
    return yaml.safe_load(dump_yaml(tree))


@pytest.fixture()
def built(session):
    result = import_tree_report(session, TREE)
    session.commit()
    return result.revision.id


@pytest.fixture()
def other(tmp_path):
    """A second, empty database (always SQLite) to import into."""
    eng = aisdb.make_engine(f"sqlite:///{tmp_path}/other.db")
    aisdb.init_db(eng)
    with sessionmaker(bind=eng, expire_on_commit=False)() as s:
        yield s
    eng.dispose()


# ------------------------------------------------------------------ the format


def test_export_carries_own_values_kinds_and_declarations(session, built):
    tree = export_tree(session)
    root = tree["all"]
    assert "kind" not in root and root["vars"]["audit_rules"] == ["base", "base"]
    assert root["children"]["ops-tools"]["kind"] == "group"  # would re-import as a business without it
    east = root["children"]["commercial"]["children"]["prod"]["children"]["dc-east-1"]
    assert east["kind"] == "datacenter" and east["inventory_name"] == "east1"
    pod = east["children"]["web-tier"]["children"]["app-a"]["children"]["shard-01"]["children"]["pod-x"]
    # own values, never the fold: the plugin export has the cumulative lists here
    assert pod["vars"] == {"audit_rules": ["pod"], "packages": ["nginx", "vim", "jq"]}
    assert pod["hosts"]["web-01"] == {"ansible_host": "10.1.1.11", "audit_rules": ["host"], "http_port": 8443}
    assert pod["hosts"]["web-02"] == {}
    folded = build_export(load_snapshot(session))["hosts"]["web-01"]["vars"]["audit_rules"]
    assert folded == ["base", "base", "biz", "dc", "shard", "base", "pod", "host"]
    # every definition on a whole-tree export, unused ones included; schemas as stored
    assert tree["variables"] == TREE["variables"]
    assert tree["tags"] == {
        "canary": {"members": [f"{POD}.web-01"]},
        "patch_wave_1": {"description": "first patch wave", "members": [f"{EAST}.web-tier", "all.ops-tools.ops-bastion01"]},
        "west-only": {"members": ["all.commercial.prod.dc-west"]},
    }


def test_stored_strings_are_verbatim_through_yaml(session, built):
    back = _through_yaml(export_tree(session))
    for key, value in ODD_STRINGS.items():
        assert back["all"]["vars"][key] == value, key


def test_yaml_is_deterministic(session, built):
    assert dump_yaml(export_tree(session)) == dump_yaml(export_tree(session))
    assert list(export_tree(session)) == ["variables", "all", "tags"]


def test_subtree_export_spine_is_structure_only(session, built):
    tree = export_tree(session, subtree=EAST)
    assert "vars" not in tree["all"]
    commercial = tree["all"]["children"]["commercial"]
    assert commercial == {"kind": "business", "children": {"prod": commercial["children"]["prod"]}}
    assert list(commercial["children"]["prod"]["children"]) == ["dc-east-1"]
    assert "vars" not in commercial["children"]["prod"]
    # only the declarations of keys the document sets, only members inside the subtree
    assert set(tree["variables"]) == {"audit_rules", "dns_servers", "http_port", "packages", "sysctl"}
    assert tree["tags"] == {
        "canary": {"members": [f"{POD}.web-01"]},
        "patch_wave_1": {"description": "first patch wave", "members": [f"{EAST}.web-tier"]},
    }


# -------------------------------------------------------------- round trips


def test_whole_tree_round_trips_into_an_empty_database(session, built, other):
    before_plugin, before_hosts = _plugin_view(session), _resolved(session)
    text = dump_yaml(export_tree(session))
    result = import_tree_report(other, yaml.safe_load(text))
    other.commit()
    assert result.tags_changed == ["canary", "patch_wave_1", "west-only"]
    assert "west_only" in _plugin_view(other)["tags"]  # a hyphenated tag is emitted as group west_only
    assert _plugin_view(other) == before_plugin
    assert _resolved(other) == before_hosts
    assert dump_yaml(export_tree(other)) == text  # and it exports to the same bytes


def test_reimporting_an_export_is_a_noop(session, built):
    for tree in (export_tree(session), export_tree(session, subtree=EAST), export_tree(session, subtree=EAST, ancestor_vars=True)):
        result = import_tree_report(session, _through_yaml(tree))
        assert not result.changed, result.summary()
        session.rollback()


def test_subtree_with_ancestor_vars_is_self_contained(session, built, other):
    before = _resolved(session)
    tree = export_tree(session, subtree=EAST, ancestor_vars=True)
    assert tree["all"]["vars"]["ntp_server"] == "time.example.com"
    import_tree_report(other, _through_yaml(tree))
    other.commit()
    after = _resolved(other)
    assert set(after) == {"db-01", "web-01", "web-02"}
    assert after == {h: before[h] for h in after}


def test_restore_a_deleted_subtree_from_a_past_revision(editor, session, built):
    """The restore story (issue 20 builds on it), in process: export at R, import."""
    R = built
    before_plugin, before_hosts = _plugin_view(session), _resolved(session)
    at_r = export_tree(session, R, EAST)
    east = session.scalar(select(Node).where(Node.path == EAST, Node.deleted_rev.is_(None)))
    assert editor.delete(f"/api/v1/nodes/{east.id}", params={"recursive": "true"}, if_match=R).status_code == 200
    session.expire_all()
    assert EAST not in load_snapshot(session).by_path
    with pytest.raises(KeyError):
        export_tree(session, subtree=EAST)  # gone now
    assert export_tree(session, R, EAST) == at_r  # still there at R, tags included
    result = import_tree_report(session, _through_yaml(at_r), message="restore dc-east-1")
    session.commit()
    assert result.nodes_created == 8 and result.tags_changed == ["canary", "patch_wave_1"]
    assert _plugin_view(session) == before_plugin
    assert _resolved(session) == before_hosts
    now = result.revision.id
    for host in before_hosts:
        assert snapshots.diff(session, host, R, now)["identical"], host


# --------------------------------------------------------------------- clone


def test_clone_with_as_and_without_hosts(session, built):
    tree = export_tree(session, subtree=EAST, as_path="all.commercial.prod.dc-east-2", hosts=False)
    dc = tree["all"]["children"]["commercial"]["children"]["prod"]["children"]
    assert list(dc) == ["dc-east-2"] and "hosts" not in dc["dc-east-2"]
    # tag members follow the new root; host members stay behind with the hosts
    assert tree["tags"] == {"patch_wave_1": {"description": "first patch wave", "members": ["all.commercial.prod.dc-east-2.web-tier"]}}
    # the clone keeps inventory_name, which would collide with the source's group name
    del dc["dc-east-2"]["inventory_name"]
    import_tree_report(session, _through_yaml(tree))
    build_export(load_snapshot(session))  # still exports
    session.commit()
    snap = load_snapshot(session)
    old = resolve(snap, path=POD)["resolved"]
    new = resolve(snap, path=POD.replace("dc-east-1", "dc-east-2"))["resolved"]
    assert old == new
    assert "all.commercial.prod.dc-east-2.web-tier" in [snap.nodes[i].path for i in snap.tags["patch_wave_1"]]


def test_clone_into_new_and_existing_parents(session, built):
    # a new business and tier: the importer infers their kinds from depth
    tree = export_tree(session, subtree=EAST, as_path="all.newbiz.stage.dc-new", hosts=False)
    assert tree["all"]["children"]["newbiz"] == {"children": {"stage": {"children": {"dc-new": tree["all"]["children"]["newbiz"]["children"]["stage"]["children"]["dc-new"]}}}}
    # an existing parent of a non-default kind (a group under all): its live kind is used
    group_clone = export_tree(session, subtree=f"{EAST}.web-tier", as_path="all.ops-tools.web-copy", hosts=False)
    assert group_clone["all"]["children"]["ops-tools"]["kind"] == "group"
    del tree["all"]["children"]["newbiz"]["children"]["stage"]["children"]["dc-new"]["inventory_name"]
    import_tree_report(session, _through_yaml(tree))
    import_tree_report(session, _through_yaml(group_clone))
    build_export(load_snapshot(session))
    session.commit()
    kinds = {n.path: n.kind for n in load_snapshot(session).nodes.values()}
    assert kinds["all.newbiz"] is NodeKind.business and kinds["all.newbiz.stage"] is NodeKind.tier
    assert kinds["all.ops-tools.web-copy.app-a.shard-01.pod-x"] is NodeKind.group


def test_empty_database_is_a_clear_404(viewer):
    r = viewer.get("/api/v1/tree")
    assert r.status_code == 404 and "the tree is empty" in r.json()["detail"]


def test_as_refusals(session, built):
    with pytest.raises(TreeExportError, match="root cannot be re-rooted"):
        export_tree(session, as_path="all.x")
    with pytest.raises(TreeExportError, match="below 'all'"):
        export_tree(session, subtree=EAST, as_path="other.x")
    with pytest.raises(TreeExportError, match="below 'all'"):
        export_tree(session, subtree=EAST, as_path="all")
    with pytest.raises(TreeExportError, match="invalid node name"):
        export_tree(session, subtree=EAST, as_path="all.bad name")
    with pytest.raises(TreeExportError, match="do not combine"):
        export_tree(session, subtree=EAST, as_path="all.x.y.z", ancestor_vars=True)


# ---------------------------------------------------------------------- HTTP


def test_http_restore_and_errors(editor, viewer, anon, session, built):
    R = built
    before = editor.get("/api/v1/export").json()
    r = viewer.get("/api/v1/tree", params={"subtree": EAST})
    assert r.status_code == 200 and r.headers["X-AIS-Revision"] == str(R)
    assert r.json() == export_tree(session, R, EAST)
    assert viewer.get("/api/v1/tree", params={"subtree": EAST}, headers={"If-None-Match": r.headers["ETag"]}).status_code == 304
    y = viewer.get("/api/v1/tree", params={"subtree": EAST, "format": "yaml"})
    assert y.headers["content-type"].startswith("application/yaml") and yaml.safe_load(y.text) == r.json()
    assert y.headers["ETag"] != r.headers["ETag"]

    east = viewer.get(f"/api/v1/nodes/{EAST}").json()["id"]
    gone = editor.delete(f"/api/v1/nodes/{east}", params={"recursive": "true"}, if_match=R).json()["revision"]
    assert viewer.get("/api/v1/tree", params={"subtree": EAST}).status_code == 404
    back = viewer.get("/api/v1/tree", params={"subtree": EAST, "revision": R})
    assert back.status_code == 200 and back.json() == r.json()
    imp = editor.post("/api/v1/import", json={"tree": back.json(), "message": "restore"}, if_match=gone)
    assert imp.status_code == 201 and imp.json()["tags"] == ["canary", "patch_wave_1"]
    after = editor.get("/api/v1/export").json()
    assert {**after, "revision": 0} == {**before, "revision": 0}

    assert anon.get("/api/v1/tree").status_code == 401
    assert viewer.get("/api/v1/tree", params={"revision": 99999}).status_code == 404
    assert viewer.get("/api/v1/tree", params={"subtree": "all.nope"}).status_code == 404
    assert viewer.get("/api/v1/tree", params={"as": "all.x"}).status_code == 422
    assert viewer.get("/api/v1/tree", params={"format": "xml"}).status_code == 422
    clone = viewer.get("/api/v1/tree", params={"subtree": EAST, "as": "all.commercial.prod.dc-east-9"}).json()
    # same host names twice: the importer's export check refuses the clone (409), writes nothing
    rev = current_rev(editor)
    assert editor.post("/api/v1/import", json={"tree": clone}, if_match=rev).status_code == 409
    assert current_rev(editor) == rev


# ------------------------------------------------------------------- tags in import


def test_import_tags_is_additive_and_validated(editor, session, seeded):
    rev = current_rev(editor)
    web = "all.commercial.prod.dc1.web"
    tree = {"all": {}, "tags": {"wave": {"description": "w", "members": [web]}}}
    dry = editor.post("/api/v1/import", json={"tree": tree, "dry_run": True}).json()
    assert dry["changed"] and dry["tags"] == ["wave"] and editor.get("/api/v1/tags/wave").status_code == 404
    rev = editor.post("/api/v1/import", json={"tree": tree}, if_match=rev).json()["revision"]
    assert editor.get("/api/v1/tags/wave").json() == {"name": "wave", "description": "w", "members": [web]}
    assert editor.post("/api/v1/import", json={"tree": tree}, if_match=rev).json()["changed"] is False
    # additive: a second member joins, the first stays; no description key keeps it
    more = {"all": {}, "tags": {"wave": {"members": ["all.federal"]}}}
    rev = editor.post("/api/v1/import", json={"tree": more}, if_match=rev).json()["revision"]
    assert editor.get("/api/v1/tags/wave").json() == {"name": "wave", "description": "w", "members": [web, "all.federal"]}
    for bad, msg in (
        ({"tier_x": {}}, "reserved"),
        ({"all": {}}, "reserved"),
        ({"bad.name": {}}, "invalid tag name"),
        ({"tier-x": {}}, "reserved"),  # its group would be tier_x
        ({"commercial-prod": {"members": ["all.federal"]}}, "hierarchy group of all.commercial.prod"),
        ({"commercial_prod": {}}, "hierarchy group of all.commercial.prod"),  # members or not
        ({"wave-": {}, "wave_": {}}, "which tag 'wave-' already is"),
        ({"wave": {"members": ["all.nope"]}}, "does not exist"),
        ({"wave": {"members": "all.federal"}}, "list of node paths"),
        (["wave"], "must be a mapping"),
    ):
        r = editor.post("/api/v1/import", json={"tree": {"all": {}, "tags": bad}}, if_match=rev)
        assert r.status_code == 422 and msg in r.json()["detail"], (bad, r.text)
    # a hyphen maps to '_' in the group name, so 'wave' and an existing tag collide only that way
    r = editor.post("/api/v1/import", json={"tree": {"all": {}, "tags": {"wa-ve": {}, "wa_ve": {}}}}, if_match=rev)
    assert r.status_code == 422 and "group 'wa_ve'" in r.json()["detail"]
    assert current_rev(editor) == rev


def test_import_tag_members_created_by_the_same_import(session, seeded):
    tree = {"all": {"children": {"federal": {"children": {"stage": {"kind": "tier"}}}}}, "tags": {"new_t": {"members": ["all.federal.stage"]}}}
    result = import_tree_report(session, tree)
    assert result.nodes_created == 1 and result.tags_changed == ["new_t"]
    with pytest.raises(ImportError_, match="expected a mapping"):
        import_tree_report(session, {"all": {}, "tags": {"x": ["all"]}})


# ----------------------------------------------------------------------- CLI


def _ais(db: pathlib.Path, *args: str, ok: bool = True) -> subprocess.CompletedProcess:
    env = dict(os.environ, AIS_DATABASE_URL=f"sqlite:///{db}", AIS_SNAPSHOTS="off")
    out = subprocess.run([sys.executable, "-m", "ais.cli", *args], capture_output=True, text=True, env=env)
    assert (out.returncode == 0) is ok, out.stdout + out.stderr
    return out


def test_cli_export_tree_round_trip(tmp_path):
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    src = tmp_path / "tree.yml"
    src.write_text(yaml.safe_dump(TREE))
    _ais(a, "import", str(src))
    out = _ais(a, "export-tree")
    assert out.stderr == "exported all at revision 1\n"
    (tmp_path / "backup.yml").write_text(out.stdout)
    assert "3 tags changed" in _ais(b, "import", str(tmp_path / "backup.yml")).stdout
    assert _ais(b, "export-tree").stdout == out.stdout
    ea = yaml.safe_load(_ais(a, "export").stdout)
    eb = yaml.safe_load(_ais(b, "export").stdout)
    assert ea == eb  # both at revision 1
    sub = _ais(a, "export-tree", "--subtree", EAST, "--as", "all.commercial.prod.dc-east-2", "--no-hosts", "--format", "json")
    assert yaml.safe_load(sub.stdout)["all"]["children"]["commercial"]["children"]["prod"]["children"].keys() == {"dc-east-2"}
    assert "does not exist at revision 1" in _ais(a, "export-tree", "--subtree", "all.nope", ok=False).stderr
    assert "revision 7 does not exist" in _ais(a, "export-tree", "--revision", "7", ok=False).stderr
    assert "root cannot be re-rooted" in _ais(a, "export-tree", "--as", "all.x", ok=False).stderr

"""Issue 24: undeclared keys that look like typos of known ones are warned about on every
write path, and strict mode (``AIS_STRICT_KEYS``) refuses new undeclared keys.

The seed declares ``packages``, ``sysctl`` and ``dns_servers``; ``ansible_host`` is
undeclared but set on six hosts (widely used at the default threshold of five)."""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import pytest
from sqlalchemy import func, select

from ais import keycheck
from ais.keycheck import Candidates, distance, normalize
from ais.models import AuditLog
from tests.conftest import SEED, current_rev

PROD = "all.commercial.prod"
WEB = "all.commercial.prod.dc1.web"


def _id(client, path: str) -> int:
    r = client.get(f"/api/v1/nodes/{path}")
    assert r.status_code == 200, (path, r.text)
    return r.json()["id"]


def _audits(session) -> int:
    session.expire_all()
    return session.scalar(select(func.count()).select_from(AuditLog))


def _declare(client, name: str) -> None:
    r = client.put(f"/api/v1/variables/{name}", json={"merge_strategy": "replace"}, if_match=current_rev(client))
    assert r.status_code == 200, r.text


def _put(client, path: str, key: str, value, **kw):
    return client.put(f"/api/v1/nodes/{_id(client, path)}/vars/{key}", json={"value": value}, if_match=current_rev(client), **kw)


# ------------------------------------------------------------------- distance


@pytest.mark.parametrize(
    "a, b, limit, expected",
    [
        ("log_level", "log_level", 2, 0),
        ("log_levle", "log_level", 2, 1),  # adjacent swap is one edit
        ("log_lvel", "log_level", 2, 1),  # deletion
        ("log_levvel", "log_level", 2, 1),  # insertion
        ("log_lebel", "log_level", 2, 1),  # substitution
        ("lgo_levle", "log_level", 2, 2),  # two swaps
        ("ca", "abc", 3, 3),  # optimal string alignment, not unrestricted Damerau (that says 2)
        ("", "", 2, 0),
        ("", "ab", 2, 2),
        ("", "abc", 2, 3),  # over the limit: limit + 1
        ("gpu_driver", "gpu_drivers_extra", 2, 3),  # length alone rules it out
        ("abcdefgh", "hgfedcba", 2, 3),  # early exit once every cell of a row is over
        ("café_mode", "cafe_mode", 2, 1),  # characters, not bytes
    ],
)
def test_distance(a, b, limit, expected):
    assert distance(a, b, limit) == expected
    assert distance(b, a, limit) == expected


def test_normalize_and_length_limits():
    assert normalize("Log-Level") == "log_level"
    cands = Candidates({"log_level", "os", "region", "packages"}, {}, threshold=5)
    assert cands.suggest("Log-Level").name == "log_level"  # equal after normalizing: always close
    assert cands.suggest("OS").name == "os"
    assert cands.suggest("ip") is None  # 2 characters: normalization only (os/ip are 2 apart)
    assert cands.suggest("regoin").name == "region"  # 6 characters: one edit
    assert cands.suggest("rgeoin") is None  # 6 characters, two edits
    assert cands.suggest("pakcgaes").name == "packages"  # two swaps
    assert cands.suggest("packxyzs") is None  # three edits
    assert cands.suggest("pakcages").name == "packages"
    assert cands.suggest("packages") is None  # declared: never a typo


def test_widely_used_candidates_are_tighter_and_must_be_more_used():
    uses = {"ansible_host": 20, "gpu_driver": 20, "gpu_drvier": 1, "ansible_port": 1, "dns_server": 20, "dns_servre": 20}
    cands = Candidates(set(), uses, threshold=5)
    assert cands.suggest("gpu_drvier").name == "gpu_driver"  # one swap away from a key 20 nodes set
    assert cands.suggest("ansible_port") is None  # two edits from ansible_host: only declared keys get two
    assert cands.suggest("dns_servre") is None  # both on 20 nodes: neither is the suspect
    assert cands.suggest("gpu_driver") is None
    assert Candidates(set(), uses, threshold=0).suggest("gpu_drvier") is None  # AIS_TYPO_MIN_NODES=0: off
    assert Candidates(set(), uses, threshold=21).suggest("gpu_drvier") is None
    # declared beats undeclared at the same distance; then by name
    both = Candidates({"log_levels"}, {"log_level": 9}, threshold=5)
    s = both.suggest("log_levle")
    assert (s.name, s.declared, s.distance) == ("log_level", False, 1)  # closer wins first
    tie = Candidates({"log_levem"}, {"log_level": 9}, threshold=5)
    assert tie.suggest("log_levex").name == "log_levem"


# --------------------------------------------------------------- write paths


def test_put_var_warns_on_a_near_miss_of_a_declared_key(editor, session, seeded):
    _declare(editor, "log_level")
    r = _put(editor, PROD, "log_levle", "debug")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["changed"] is True and body["value"] == "debug"  # stored anyway
    assert body["warnings_total"] == 1
    assert body["warnings"] == [
        {
            "key": "log_levle",
            "path": PROD,
            "did_you_mean": "log_level",
            "declared": True,
            "message": f"log_levle at {PROD} is not declared; did you mean log_level (declared)?",
        }
    ]
    # declared keys and unrelated new keys: nothing to say
    assert _put(editor, PROD, "log_level", "error").json()["warnings"] == []
    assert _put(editor, PROD, "owner_team", "sre").json()["warnings"] == []
    # the same key again, now set on one node: still a near miss
    assert _put(editor, WEB, "log_levle", "info").json()["warnings"][0]["path"] == WEB
    # an idempotent re-put writes nothing and says nothing
    again = _put(editor, WEB, "log_levle", "info").json()
    assert again["changed"] is False and "warnings" not in again


def test_put_var_warns_on_a_near_miss_of_a_widely_used_key(editor, seeded):
    r = _put(editor, f"{WEB}.web01", "ansible_hots", "10.1.1.99").json()
    assert [(w["did_you_mean"], w["declared"]) for w in r["warnings"]] == [("ansible_host", False)]
    assert "not declared either, set on 6 nodes" in r["warnings"][0]["message"]
    # ansible_port is two edits from ansible_host: not flagged against an undeclared key
    assert _put(editor, f"{WEB}.web01", "ansible_port", 2222).json()["warnings"] == []


def test_create_node_vars_warn(editor, seeded):
    r = editor.post(
        "/api/v1/nodes",
        json={"parent_path": WEB, "name": "web03", "kind": "host", "vars": {"ansible_hots": "10.1.1.13", "packges": ["git"]}},
        if_match=current_rev(editor),
    )
    assert r.status_code == 201, r.text
    assert [(w["key"], w["did_you_mean"]) for w in r.json()["warnings"]] == [("ansible_hots", "ansible_host"), ("packges", "packages")]


def test_import_warns_per_key_and_path_capped_with_a_count(editor, seeded, monkeypatch):
    monkeypatch.setattr(keycheck, "MAX_WARNINGS", 3)
    tree = {
        "all": {
            "vars": {"sysctrl": {"vm.swappiness": 5}},
            "children": {
                "commercial": {
                    "children": {
                        "prod": {
                            "vars": {"packges": ["a"]},
                            "children": {
                                "dc1": {
                                    "vars": {"packges": ["b"]},
                                    "children": {"web": {"vars": {"packges": ["c"]}, "hosts": {"web01": {"packges": ["d"]}}}},
                                }
                            },
                        }
                    }
                }
            },
        }
    }
    dry = editor.post("/api/v1/import", json={"tree": tree, "dry_run": True})
    assert dry.status_code == 200, dry.text
    # every key once before any key twice; sorted by key, then path
    assert [(w["key"], w["path"]) for w in dry.json()["warnings"]] == [("packges", PROD), ("sysctrl", "all"), ("packges", f"{PROD}.dc1")]
    assert dry.json()["warnings_total"] == 5
    real = editor.post("/api/v1/import", json={"tree": tree}, if_match=current_rev(editor))
    assert real.status_code == 201 and real.json()["warnings"] == dry.json()["warnings"] and real.json()["warnings_total"] == 5
    # re-importing writes nothing, so it warns about nothing
    again = editor.post("/api/v1/import", json={"tree": tree}, if_match=current_rev(editor)).json()
    assert again["changed"] is False and again["warnings"] == [] and again["warnings_total"] == 0


def test_import_that_declares_the_key_does_not_warn(editor, seeded):
    tree = {"variables": {"packges": {"merge_strategy": "replace"}}, "all": {"vars": {"packges": ["x"]}}}
    r = editor.post("/api/v1/import", json={"tree": tree}, if_match=current_rev(editor))
    assert r.status_code == 201 and r.json()["warnings"] == []


def test_restore_and_revert_warn(editor, seeded):
    _declare(editor, "log_level")
    r = _put(editor, f"{PROD}.dc1.db", "log_levle", "debug")
    assert r.json()["warnings"]
    rev = current_rev(editor)
    dropped = editor.delete(f"/api/v1/nodes/{_id(editor, f'{PROD}.dc1.db')}", params={"recursive": "true"}, if_match=rev)
    assert dropped.status_code == 200
    dry = editor.post("/api/v1/nodes/restore", json={"path": f"{PROD}.dc1.db", "dry_run": True})
    assert [w["key"] for w in dry.json()["warnings"]] == ["log_levle"]
    restored = editor.post("/api/v1/nodes/restore", json={"path": f"{PROD}.dc1.db"}, if_match=current_rev(editor))
    assert restored.status_code == 201 and restored.json()["warnings"][0]["path"] == f"{PROD}.dc1.db"
    # revert the restore (deletes again: nothing written, nothing to say), then revert that
    undo = editor.post(f"/api/v1/revisions/{restored.json()['revision']}/revert", json={}, if_match=current_rev(editor))
    assert undo.status_code == 201 and undo.json()["warnings"] == []
    redo = editor.post(f"/api/v1/revisions/{undo.json()['revision']}/revert", json={"dry_run": True})
    assert redo.status_code == 200 and [w["key"] for w in redo.json()["warnings"]] == ["log_levle"]


# ------------------------------------------------------------------ strict mode


@pytest.fixture()
def strict(monkeypatch):
    monkeypatch.setenv("AIS_STRICT_KEYS", "true")


def test_strict_refuses_a_new_undeclared_key_with_the_hint(editor, viewer, session, seeded, strict):
    _declare(editor, "log_level")
    rev, rows = current_rev(editor), _audits(session)
    r = _put(editor, PROD, "log_levle", "debug")
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "undeclared key"
    assert detail["keys"] == [{"key": "log_levle", "paths": [PROD], "nodes": 1, "did_you_mean": "log_level"}]
    assert "log_levle (did you mean log_level?)" in detail["message"]
    assert "PUT /api/v1/variables/log_levle" in detail["message"] and "variable_definition" in detail["message"]
    assert current_rev(editor) == rev and _audits(session) == rows  # nothing written, nothing audited
    # a new key with no near miss is refused too, without a hint
    r = _put(editor, PROD, "owner_team", "sre")
    assert r.status_code == 422 and "did_you_mean" not in r.json()["detail"]["keys"][0]
    # declared first, it goes through
    _declare(editor, "owner_team")
    assert _put(editor, PROD, "owner_team", "sre").status_code == 200


def test_strict_keeps_existing_undeclared_keys_working(editor, viewer, seeded, strict):
    # ntp_server and log_level are in the seed, undeclared: new values and new nodes are fine
    assert _put(editor, PROD, "ntp_server", "ntp.example.com").status_code == 200
    assert _put(editor, f"{PROD}.dc1", "log_level", "error").status_code == 200
    r = editor.post("/api/v1/nodes", json={"parent_path": WEB, "name": "web09", "kind": "host", "vars": {"ansible_host": "10.1.1.19"}}, if_match=current_rev(editor))
    assert r.status_code == 201, r.text
    # reads, renders and deletes
    assert viewer.get("/api/v1/vars", params={"key": "log_level"}).json()["total"] == 4
    assert viewer.post("/api/v1/render", json={"host": "web01"}).json()["resolved"]["log_level"] == "error"
    assert editor.delete(f"/api/v1/nodes/{_id(editor, PROD)}/vars/log_level", if_match=current_rev(editor)).status_code == 200


def test_strict_import_and_create_node(editor, seeded, strict):
    bad = {"all": {"vars": {"brand_new": 1, "ntp_server": "ntp9.example.com"}}}
    for body in ({"tree": bad, "dry_run": True}, {"tree": bad}):
        r = editor.post("/api/v1/import", json=body, if_match=current_rev(editor))
        assert r.status_code == 422 and [k["key"] for k in r.json()["detail"]["keys"]] == ["brand_new"]
    declared = {"variables": {"brand_new": {"merge_strategy": "replace"}}, **bad}
    assert editor.post("/api/v1/import", json={"tree": declared}, if_match=current_rev(editor)).status_code == 201
    r = editor.post("/api/v1/nodes", json={"parent_path": WEB, "name": "web08", "kind": "host", "vars": {"fresh_key": 1}}, if_match=current_rev(editor))
    assert r.status_code == 422 and r.json()["detail"]["keys"][0]["key"] == "fresh_key"
    assert editor.get(f"/api/v1/nodes/{WEB}.web08").status_code == 404


def test_strict_never_refuses_a_restore(editor, seeded, monkeypatch):
    # a key only the deleted subtree had: after the delete it is live nowhere
    assert _put(editor, f"{PROD}.dc1.db", "db_only_key", 1).status_code == 200
    rev = current_rev(editor)
    assert editor.delete(f"/api/v1/nodes/{_id(editor, f'{PROD}.dc1.db')}", params={"recursive": "true"}, if_match=rev).status_code == 200
    monkeypatch.setenv("AIS_STRICT_KEYS", "true")
    r = editor.post("/api/v1/nodes/restore", json={"path": f"{PROD}.dc1.db"}, if_match=current_rev(editor))
    assert r.status_code == 201, r.text


@pytest.mark.parametrize("value, on", [("true", True), ("1", True), ("YES", True), ("on", True), ("false", False), ("", False), ("nope", False)])
def test_strict_setting(value, on, monkeypatch):
    monkeypatch.setenv("AIS_STRICT_KEYS", value)
    assert keycheck.strict_keys() is on


# ------------------------------------------------------------------------ lint


def test_lint_lists_existing_likely_typos(editor, viewer, seeded):
    _declare(editor, "log_level")
    _put(editor, f"{PROD}.dc1", "log_levle", "x")
    _put(editor, f"{WEB}.web02", "ansible_hots", "10.1.1.12")
    r = viewer.get("/api/v1/vars/lint")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2 and body["strict"] is False and body["revision"] == current_rev(viewer)
    assert body["keys"] == [
        {"key": "ansible_hots", "did_you_mean": "ansible_host", "declared": False, "nodes": 1, "paths": [f"{WEB}.web02"]},
        {"key": "log_levle", "did_you_mean": "log_level", "declared": True, "nodes": 1, "paths": [f"{PROD}.dc1"]},
    ]


def _ais(db: pathlib.Path, *args: str, code: int = 0, **env: str) -> subprocess.CompletedProcess:
    environ = {k: v for k, v in os.environ.items() if k != "AIS_STRICT_KEYS"}
    environ.update(AIS_DATABASE_URL=f"sqlite:///{db}", AIS_SNAPSHOTS="off", **env)
    out = subprocess.run([sys.executable, "-m", "ais.cli", *args], capture_output=True, text=True, env=environ)
    assert out.returncode == code, out.stdout + out.stderr
    return out


def test_cli_import_warns_strict_refuses_and_lint(tmp_path):
    db = tmp_path / "ais.db"
    _ais(db, "import", str(SEED))
    assert _ais(db, "lint").stderr.strip() == "0 likely typo(s) at revision 1"
    typo = tmp_path / "typo.yml"
    typo.write_text("all:\n  children:\n    commercial:\n      vars: {packges: [git], brand_new: 1}\n")
    out = _ais(db, "import", str(typo), code=1, AIS_STRICT_KEYS="true")
    assert "import refused: strict mode (AIS_STRICT_KEYS)" in out.stderr and "packges (did you mean packages?)" in out.stderr
    out = _ais(db, "import", str(typo))
    assert out.stderr == "warning: packges at all.commercial is not declared; did you mean packages (declared)?\n"
    assert out.stdout.startswith("imported as revision 2")
    out = _ais(db, "lint", code=1)
    assert out.stdout.startswith("packges: did you mean packages (declared)? set on 1 node(s): all.commercial")
    assert '"did_you_mean": "packages"' in _ais(db, "lint", "--json", code=1).stdout

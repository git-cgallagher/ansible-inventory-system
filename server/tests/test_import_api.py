"""POST /import: additive, idempotent (a no-op leaves no revision), dry run."""

from __future__ import annotations

from sqlalchemy import func, select

from ais.models import AuditLog
from tests.conftest import current_rev

TREE = {
    "variables": {"queue_size": {"merge_strategy": "replace", "description": "q"}},
    "all": {"children": {"commercial": {"children": {"prod": {"children": {"dc1": {"children": {"mq": {"vars": {"queue_size": 10}, "hosts": {"mq01": {"ansible_host": "10.1.9.1"}}}}}}}}}}},
}


def _audits(session):
    return session.scalar(select(func.count()).select_from(AuditLog))


def test_dry_run_then_real_then_noop(editor, viewer, session, seeded):
    rev = current_rev(editor)
    dry = editor.post("/api/v1/import", json={"tree": TREE, "dry_run": True})  # no If-Match needed
    assert dry.status_code == 200
    assert dry.json() == {"revision": rev, "changed": True, "dry_run": True, "nodes_created": 2, "variables_set": 2, "definitions": ["queue_size"]}
    assert current_rev(editor) == rev and viewer.get("/api/v1/nodes/all.commercial.prod.dc1.mq").status_code == 404
    real = editor.post("/api/v1/import", json={"tree": TREE}, if_match=rev)
    assert real.status_code == 201 and real.json() == {**dry.json(), "revision": rev + 1, "dry_run": False}
    before = _audits(session)
    again = editor.post("/api/v1/import", json={"tree": TREE}, if_match=rev + 1)
    assert again.status_code == 200
    assert again.json() == {"revision": rev + 1, "changed": False, "dry_run": False, "nodes_created": 0, "variables_set": 0, "definitions": []}
    assert current_rev(editor) == rev + 1 and _audits(session) == before
    assert editor.post("/api/v1/import", json={"tree": TREE, "dry_run": True}).json()["changed"] is False


def test_import_refusals(editor, seeded):
    rev = current_rev(editor)
    assert editor.post("/api/v1/import", json={"tree": {"nope": {}}}, if_match=rev).status_code == 422
    bad_kind = {"all": {"children": {"commercial": {"kind": "tier"}}}}  # exists as business
    assert editor.post("/api/v1/import", json={"tree": bad_kind}, if_match=rev).status_code == 422
    bad_strategy = {"variables": {"x": {"merge_strategy": "zip"}}, "all": {}}
    assert editor.post("/api/v1/import", json={"tree": bad_strategy}, if_match=rev).status_code == 422
    assert current_rev(editor) == rev

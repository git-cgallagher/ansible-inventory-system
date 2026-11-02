"""Snapshot history: stored only on change, by a background pass, never on export or
render; lookups exact for every revision (stored, passed, or computed on demand)."""

from __future__ import annotations

import time

from sqlalchemy import func, select

from ais import snapshots
from ais.models import HostSnapshot, SnapshotPass
from ais.resolver import load_snapshot, resolve, resolved_vars
from tests.conftest import current_rev

HOSTS = ["db01", "fed-edge01", "fed-web01", "lab-web01", "web01", "web02"]


def _rows(session) -> list[tuple[str, int]]:
    session.expire_all()
    return sorted((r.host_name, r.revision) for r in session.scalars(select(HostSnapshot)))


def _pass(session):
    result = snapshots.run_pass(session)
    session.commit()
    return result


def _web(client) -> int:
    return client.get("/api/v1/nodes/all.commercial.prod.dc1.web").json()["id"]


def test_resolved_vars_equals_resolve(session, seeded):
    snap = load_snapshot(session)
    for name in HOSTS:
        assert resolved_vars(snap, snap.host(name)) == resolve(snap, host=name)["resolved"]


def test_export_and_render_store_nothing(viewer, session, seeded):
    viewer.get("/api/v1/export")
    viewer.post("/api/v1/render", json={"host": "web01"})
    assert _rows(session) == []


def test_pass_stores_every_host_once_then_only_changes(editor, viewer, session, seeded):
    first = _pass(session)
    assert (first.revision, first.hosts_checked, first.rows_written) == (seeded.id, 6, 6)
    assert _pass(session) is None  # nothing new to pass

    rev0 = current_rev(editor)
    rev1 = editor.put(f"/api/v1/nodes/{_web(editor)}/vars/http_port", json={"value": 9090}, if_match=rev0).json()["revision"]
    second = _pass(session)
    assert (second.revision, second.rows_written) == (rev1, 2)  # web01 + web02 only
    assert _rows(session) == sorted([(h, seeded.id) for h in HOSTS] + [("web01", rev1), ("web02", rev1)])
    assert [s["revision"] for s in viewer.get("/api/v1/hosts/web01/snapshots").json()] == [seeded.id, rev1]
    assert [s["revision"] for s in viewer.get("/api/v1/hosts/db01/snapshots").json()] == [seeded.id]

    # db01 did not change at rev1: its state there is the row stored at seeded.id
    view = viewer.get(f"/api/v1/hosts/db01/snapshots/{rev1}").json()
    assert view["revision"] == rev1 and view["stored_revision"] == seeded.id
    assert view["resolved"]["inventory_revision"] == rev1  # reported at the asked revision


def test_burst_coalesces_and_skipped_revisions_compute_on_demand(editor, viewer, session, seeded):
    _pass(session)
    rev = current_rev(editor)
    web = _web(editor)
    ports = {}
    for port in (9001, 9002, 9003):  # three writes, one pass
        rev = editor.put(f"/api/v1/nodes/{web}/vars/http_port", json={"value": port}, if_match=rev).json()["revision"]
        ports[rev] = port
    result = _pass(session)
    assert result.revision == max(ports) and result.rows_written == 2
    assert session.scalar(select(func.count()).select_from(SnapshotPass)) == 2
    for r, port in ports.items():  # every revision answers exactly, passed or not
        view = viewer.get(f"/api/v1/hosts/web01/snapshots/{r}").json()
        assert view["resolved"]["http_port"] == port
        assert (view["stored_revision"] is None) == (r != max(ports))
    assert _rows(session).count(("web01", min(ports))) == 0  # on-demand results are never stored


def test_diff_between_revisions(editor, viewer, session, seeded):
    rev0 = current_rev(editor)
    _pass(session)
    web = _web(editor)
    rev1 = editor.put(f"/api/v1/nodes/{web}/vars/http_port", json={"value": 9090}, if_match=rev0).json()["revision"]
    rev2 = editor.put(f"/api/v1/nodes/{web}/vars/tls", json={"value": True}, if_match=rev1).json()["revision"]
    _pass(session)
    rev3 = editor.delete(f"/api/v1/nodes/{web}/vars/http_port", if_match=rev2).json()["revision"]
    d = viewer.get("/api/v1/diff", params={"host": "web01", "from": rev0, "to": rev2}).json()
    assert d["changed"] == {"http_port": {"from": 8080, "to": 9090}} and d["added"] == {"tls": True} and d["removed"] == {}
    assert d["identical"] is False and d["from_hash"] != d["to_hash"]
    d = viewer.get("/api/v1/diff", params={"host": "web01", "from": rev1, "to": rev3}).json()  # neither passed
    assert d["removed"] == {"http_port": 9090} and d["added"] == {"tls": True}
    same = viewer.get("/api/v1/diff", params={"host": "db01", "from": rev0, "to": rev3}).json()
    assert same["identical"] is True and same["from_hash"] == same["to_hash"]
    assert viewer.get("/api/v1/diff", params={"host": "web01", "from": rev0, "to": 9999}).status_code == 404
    assert viewer.get("/api/v1/diff", params={"host": "ghost", "from": rev0, "to": rev1}).status_code == 404


def test_deleted_host_has_no_snapshot_after_deletion(editor, viewer, session, seeded):
    rev0 = current_rev(editor)
    _pass(session)
    web01 = editor.get("/api/v1/nodes/all.commercial.prod.dc1.web.web01").json()["id"]
    rev1 = editor.delete(f"/api/v1/nodes/{web01}", if_match=rev0).json()["revision"]
    _pass(session)  # rev1 passed; web01's latest row is still the one at rev0
    assert viewer.get(f"/api/v1/hosts/web01/snapshots/{rev0}").status_code == 200
    assert viewer.get(f"/api/v1/hosts/web01/snapshots/{rev1}").status_code == 404
    assert viewer.get("/api/v1/diff", params={"host": "web01", "from": rev0, "to": rev1}).status_code == 404
    assert viewer.get("/api/v1/hosts/nope/snapshots").status_code == 404


def test_move_keeps_history_by_name(editor, viewer, session, seeded):
    rev0 = current_rev(editor)
    _pass(session)
    web = _web(editor)
    rev1 = editor.post(f"/api/v1/nodes/{web}/move", json={"parent_path": "all.federal.prod.dc2", "name": "www"}, if_match=rev0).json()["revision"]
    result = _pass(session)
    assert result.revision == rev1
    hist = [s["revision"] for s in viewer.get("/api/v1/hosts/web01/snapshots").json()]
    assert hist == [rev0, rev1]  # moved under federal: compliance_profile and more changed
    d = viewer.get("/api/v1/diff", params={"host": "web01", "from": rev0, "to": rev1}).json()
    assert d["changed"]["compliance_profile"] == {"from": "cis-level1", "to": "fedramp-moderate"}


def test_status_endpoint(viewer, session, seeded):
    before = viewer.get("/api/v1/snapshots/status").json()
    assert before["behind"] is True and before["passed"] == 0 and before["worker"] is False
    _pass(session)
    after = viewer.get("/api/v1/snapshots/status").json()
    assert after["behind"] is False and after["passed"] == seeded.id and after["rows"] == 6
    assert after["last_pass"]["hosts_checked"] == 6


def test_worker_passes_after_a_write(editor, session, seeded, monkeypatch):
    monkeypatch.setenv("AIS_SNAPSHOT_POLL_SECONDS", "30")  # prove the wake-up, not the poll
    worker = snapshots.SnapshotWorker()
    worker.debounce = 0.05
    monkeypatch.setattr(snapshots, "worker", worker)
    worker.start()
    try:
        deadline = time.time() + 10
        while time.time() < deadline and _status(session)["passed"] != seeded.id:
            time.sleep(0.05)
        assert _status(session)["passed"] == seeded.id  # caught up at start
        rev = editor.put(f"/api/v1/nodes/{_web(editor)}/vars/http_port", json={"value": 7070}, if_match=current_rev(editor)).json()["revision"]
        deadline = time.time() + 10
        while time.time() < deadline and _status(session)["passed"] != rev:
            time.sleep(0.05)
        assert _status(session)["passed"] == rev
        assert ("web01", rev) in _rows(session)
    finally:
        worker.stop()


def _status(session):
    session.expire_all()
    session.commit()
    return snapshots.status(session)


def _dedup_sql() -> str:
    import importlib.util
    import pathlib

    path = pathlib.Path(snapshots.__file__).parent / "migrations" / "versions" / "0005_snapshot_on_change.py"
    spec = importlib.util.spec_from_file_location("ais_migration_0005", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.DEDUP_SQL


def test_dedup_keeps_lookups_exact(engine, session, seeded):
    """Rows written the old way (one per host per exported revision) that repeat the host's
    previous row are dropped by 0005's statement; lookups stay exact."""
    from sqlalchemy import text

    from ais.importer import import_tree
    from ais.models import Revision

    snap = load_snapshot(session)
    rev2 = import_tree(session, {"all": {"vars": {"ntp_server": "ntp2.example.com"}}})  # changes every host
    session.commit()
    rev3 = Revision(actor="test", message="no-op")
    session.add(rev3)
    session.commit()
    for rev in (seeded.id, rev2.id, rev3.id):  # the old behaviour: a full row set per export
        s = load_snapshot(session, rev)
        for name in HOSTS:
            node = s.host(name)
            res = resolve(s, host=name)["resolved"]
            session.add(HostSnapshot(host_node_id=node.id, host_name=name, revision=rev, resolved=res, resolved_hash=snapshots.resolved_hash(res)))
    session.commit()
    assert len(_rows(session)) == 18
    session.execute(text(_dedup_sql()))
    session.commit()
    assert {r for _, r in _rows(session)} == {seeded.id, rev2.id}  # the rev3 copies are gone
    view = snapshots.get_snapshot(session, "web01", rev3.id)
    assert view.stored_revision is None and view.resolved["ntp_server"] == "ntp2.example.com"

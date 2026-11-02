"""Depth and path-length limits: one check behind import, create and move, and an
importer whose depth is bounded by that check rather than Python's recursion limit."""

from __future__ import annotations

import os

import pytest

from ais.importer import MAX_PATH_BYTES, ImportError_, import_tree
from ais.resolver import build_export, load_snapshot, resolve
from tests.conftest import current_rev


def _chain(levels: int, name: str = "g") -> dict:
    """all → biz → prod → dc1 (depth 3) → ``levels`` nested groups → host ``h1``.
    Group ``{name}{i}`` sits at depth 4 + i; the host at depth 4 + ``levels``."""
    inner: dict | None = None
    inner_name = ""
    for i in reversed(range(levels)):
        group: dict = {"vars": {f"v{i}": i, "packages": [f"p{i}"]}}
        if inner is None:
            group["hosts"] = {"h1": {"x": 1}}
        else:
            group["children"] = {inner_name: inner}
        inner, inner_name = group, f"{name}{i}"
    return {
        "variables": {"packages": {"merge_strategy": "unique_append"}},
        "all": {"children": {"biz": {"children": {"prod": {"children": {"dc1": {"children": {inner_name: inner}}}}}}}},
    }


def test_default_limit_allows_32_and_refuses_33(session):
    import_tree(session, _chain(28))  # the host lands at depth 4 + 28 = 32
    session.commit()
    snap = load_snapshot(session)
    assert resolve(snap, host="h1")["resolved"]["packages"] == [f"p{i}" for i in range(28)]
    with pytest.raises(ImportError_, match="maximum depth 32"):
        import_tree(session, _chain(29))


@pytest.mark.skipif(
    (os.environ.get("AIS_TEST_DATABASE_URL") or "").startswith("postgresql"),
    reason="lifts the path cap, so on PostgreSQL it hits the ltree index limit the cap exists for",
)
def test_deep_import_is_not_bounded_by_recursion(session, monkeypatch):
    # The recursive importer died near depth 1000; the explicit stack does not. Lift both
    # limits so this tests the walk alone (in practice the path cap binds first).
    from ais import importer

    monkeypatch.setenv("AIS_MAX_DEPTH", "1300")
    monkeypatch.setattr(importer, "MAX_PATH_BYTES", 10**6)
    import_tree(session, _chain(1200))
    session.commit()
    snap = load_snapshot(session)
    data = build_export(snap)
    assert data["hosts"]["h1"]["path"].count(".") == 1204
    assert len(resolve(snap, host="h1")["resolved"]["packages"]) == 1200


def test_path_byte_limit(session, monkeypatch):
    monkeypatch.setenv("AIS_MAX_DEPTH", "100")
    long = "n" * 250
    with pytest.raises(ImportError_, match=f"over the {MAX_PATH_BYTES}-byte limit"):
        import_tree(session, _chain(9, name=long))


def test_import_order_is_unchanged(session, seeded):
    """The explicit stack visits nodes in the old recursive order, so ids come out
    parent-before-child and siblings in document order."""
    from sqlalchemy import select

    from ais.models import Node

    rows = session.scalars(select(Node).order_by(Node.id)).all()
    paths = [n.path for n in rows]
    for n in rows:
        if n.parent_id is not None:
            parent = next(p for p in rows if p.id == n.parent_id)
            assert paths.index(parent.path) < paths.index(n.path)
    assert paths.index("all.commercial.prod.dc1.web") < paths.index("all.commercial.prod.dc1.db")
    assert paths.index("all.commercial.prod.dc1.web.web01") < paths.index("all.commercial.prod.dc1.db")


def test_create_refuses_past_the_limit(editor, seeded, monkeypatch):
    monkeypatch.setenv("AIS_MAX_DEPTH", "4")
    rev = current_rev(editor)
    # web is depth 4, so a child would be depth 5; a sibling of web is depth 4 and fits.
    ok = editor.post("/api/v1/nodes", json={"parent_path": "all.commercial.prod.dc1.web", "name": "deep"}, if_match=rev)
    assert ok.status_code == 422 and "maximum depth 4" in ok.json()["detail"]
    fits = editor.post("/api/v1/nodes", json={"parent_path": "all.commercial.prod.dc1", "name": "cache"}, if_match=rev)
    assert fits.status_code in (200, 201), fits.text


def test_move_checks_the_whole_subtree(editor, seeded, monkeypatch):
    rev = current_rev(editor)
    web = editor.get("/api/v1/nodes/all.commercial.prod.dc1.web").json()
    monkeypatch.setenv("AIS_MAX_DEPTH", "5")
    # web (depth 4) has hosts at depth 5; under web's sibling db it would be depth 5 with hosts at 6.
    r = editor.post(f"/api/v1/nodes/{web['id']}/move", json={"parent_path": "all.commercial.prod.dc1.db"}, if_match=rev)
    assert r.status_code == 422, r.text
    assert "maximum depth 5" in r.json()["detail"]
    assert current_rev(editor) == rev  # refused before anything was written

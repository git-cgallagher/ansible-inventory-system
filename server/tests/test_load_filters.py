"""SQL-side loading: a host's chain resolves exactly like the full tree, and the subtree
filter (``path_ltree`` on PostgreSQL, an escaped prefix elsewhere) keeps the right nodes."""

from __future__ import annotations

import pytest

from ais.importer import import_tree
from ais.models import NodeKind
from ais.resolver import build_export, load_chain, load_snapshot, resolve


def test_chain_resolve_equals_full_tree(session, seeded):
    full = load_snapshot(session)
    hosts = [n.name for n in full.nodes.values() if n.kind is NodeKind.host]
    assert len(hosts) >= 5
    for host in hosts:
        chain = load_chain(session, host=host)
        assert len(chain.nodes) == full.host(host).depth + 1
        assert resolve(chain, host=host) == resolve(full, host=host), host
    for path in ("all", "all.commercial.prod", "all.federal.prod.dc2.web"):
        assert resolve(load_chain(session, path=path), path=path) == resolve(full, path=path)
    with pytest.raises(KeyError):
        load_chain(session, host="nope")
    with pytest.raises(KeyError):
        load_chain(session, path="all.nope")


def test_chain_at_a_pinned_revision(session, seeded):
    rev2 = import_tree(session, {"all": {"children": {"commercial": {"vars": {"compliance_profile": "changed"}}}}})
    session.commit()
    old = resolve(load_chain(session, seeded.id, host="web01"), host="web01")
    new = resolve(load_chain(session, rev2.id, host="web01"), host="web01")
    assert old["resolved"]["compliance_profile"] == "cis-level1"
    assert new["resolved"]["compliance_profile"] == "changed"


def test_subtree_filter_is_not_a_like_pattern(session):
    # "a_b" as a LIKE pattern would also match "axb"; the filter must not.
    import_tree(session, {"all": {"children": {
        "a_b": {"children": {"t": {"hosts": {"h1": {}}}}},
        "axb": {"children": {"t": {"hosts": {"h2": {}}}}},
    }}})
    session.commit()
    snap = load_snapshot(session, subtree="all.a_b")
    assert sorted(n.path for n in snap.nodes.values()) == ["all", "all.a_b", "all.a_b.t", "all.a_b.t.h1"]
    assert sorted(build_export(snap)["hosts"]) == ["h1"]
    with pytest.raises(KeyError):
        load_snapshot(session, subtree="all.nope")

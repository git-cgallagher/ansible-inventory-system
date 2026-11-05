"""Export and resolution.

Everything here reads the tree *as of a revision*: nodes and variables are temporal
rows, so ``load_snapshot(session, revision=n)`` sees exactly the nodes and values
that were live at revision ``n`` (nodes created later are absent, nodes deleted
later are present).

Two jobs:

1. ``build_export`` turns the tree into the structure the inventory plugin feeds to
   Ansible: groups (with children, hosts and vars), hosts (with vars) and var-less
   tag groups. Ansible then merges group vars natively, ancestors first by depth.
   Because the hierarchy is a strict tree, a host's groups form one chain with no
   two groups at the same depth, so Ansible's default precedence is exactly ours.

2. For variables declared ``merge`` / ``append`` / ``unique_append`` (which Ansible
   cannot express per key) the exporter *folds* the value down the path and emits
   the cumulative fold at every contributing node. Every host under a node shares
   its ancestry, so the fold emitted there is correct for all of them, and
   Ansible's plain replace then yields the right value.

``resolve`` walks the same chain with Ansible's own ``combine_vars`` so the server
never carries a second merge implementation. ``tests/test_parity.py`` asserts it
matches ``ansible-inventory --host``.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any

from ansible.utils.vars import combine_vars
from sqlalchemy import and_, or_, select, text
from sqlalchemy.orm import Session

from ais.models import (
    PATH_SEP,
    TAGGED_KINDS,
    MergeStrategy,
    Node,
    NodeKind,
    NodeVariable,
    Revision,
    Tag,
    TagMember,
    VariableDefinition,
)

GROUP_SEP = "_"
_UNSAFE = re.compile(r"[^A-Za-z0-9_]")


# --------------------------------------------------------------------------- fold


def fold(strategy: MergeStrategy, acc: Any, value: Any) -> Any:
    """Combine an accumulated value with the next contribution down the path."""
    if acc is None or strategy is MergeStrategy.replace:
        return copy.deepcopy(value)
    if strategy is MergeStrategy.merge:
        if isinstance(acc, dict) and isinstance(value, dict):
            out = copy.deepcopy(acc)
            for k, v in value.items():
                out[k] = fold(strategy, out.get(k), v) if isinstance(v, dict) else copy.deepcopy(v)
            return out
        return copy.deepcopy(value)  # type mismatch: fall back to replace
    if strategy in (MergeStrategy.append, MergeStrategy.unique_append):
        if isinstance(acc, list) and isinstance(value, list):
            out = list(acc)
            for item in value:
                if strategy is MergeStrategy.append or item not in out:
                    out.append(copy.deepcopy(item))
            return out
        return copy.deepcopy(value)
    raise ValueError(strategy)


# ------------------------------------------------------------------------ loading


@dataclass
class Snapshot:
    """Everything the exporter needs, read as of one revision.

    Read-only once loaded: ``children``/``host`` build indexes on first use and
    ``_emitted`` caches each node's emitted variables, shared between every caller. Nothing
    may mutate what those return; ``build_export`` only serializes the cached dicts and
    ``resolve`` deep-copies the result it hands out. Add a mutation and you corrupt every
    other host of the same export.

    ``ais.impact`` builds one over light rows (any object with ``id``, ``parent_id``,
    ``kind``, ``name``, ``path``, ``inventory_name`` and ``ancestors_paths()``) and only the
    keys it needs, to call ``_emitted``; such a snapshot, like a ``keys=`` one, must never be
    exported."""

    revision: int
    nodes: dict[int, Node]
    by_path: dict[str, Node]
    vars_by_node: dict[int, dict[str, Any]]
    strategies: dict[str, MergeStrategy]
    tags: dict[str, list[int]] = field(default_factory=dict)  # tag name -> node ids
    # Indexes built on first use; a snapshot is read-only once loaded. Without them the
    # exporter scanned every node once per node (quadratic: 24 s for 12k nodes, docs/scale.md).
    _children: dict[int, list[Node]] | None = field(default=None, repr=False)
    _hosts: dict[str, Node] | None = field(default=None, repr=False)
    _emitted: dict[int, dict[str, Any]] = field(default_factory=dict, repr=False)

    def children(self, node: Node) -> list[Node]:
        if self._children is None:
            index: dict[int, list[Node]] = {}
            for n in self.nodes.values():
                if n.parent_id is not None:
                    index.setdefault(n.parent_id, []).append(n)
            for kids in index.values():
                kids.sort(key=lambda n: n.name)
            self._children = index
        return list(self._children.get(node.id, ()))

    def host(self, name: str) -> Node | None:
        if self._hosts is None:
            self._hosts = {n.name: n for n in self.nodes.values() if n.kind is NodeKind.host}
        return self._hosts.get(name)

    def chain(self, node: Node) -> list[Node]:
        """Root-first list of nodes from ``all`` down to ``node``."""
        return [self.by_path[p] for p in node.ancestors_paths()]

    def strategy(self, key: str) -> MergeStrategy:
        return self.strategies.get(key, MergeStrategy.replace)


def current_revision(session: Session) -> int:
    rev = session.scalar(select(Revision.id).order_by(Revision.id.desc()).limit(1))
    return rev or 0


def _live_at(model, rev: int):
    created = model.created_rev <= rev if model is NodeVariable else (Node.created_rev.is_(None)) | (Node.created_rev <= rev)
    return and_(created, (model.deleted_rev.is_(None)) | (model.deleted_rev > rev))


def _under(session: Session, path: str):
    """SQL filter: strictly below ``path``. PostgreSQL uses the GiST-indexed generated
    ``path_ltree`` column (migration-only, not in the ORM model); elsewhere an escaped
    prefix match (``_`` is a LIKE wildcard and legal in names)."""
    if session.get_bind().dialect.name == "postgresql":
        return and_(text("node.path_ltree <@ CAST(:ais_under AS ltree)").bindparams(ais_under=path), Node.path != path)
    return Node.path.startswith(path + PATH_SEP, autoescape=True)


def load_snapshot(
    session: Session,
    revision: int | None = None,
    subtree: str | None = None,
    *,
    paths: list[str] | None = None,
    keys: list[str] | None = None,
) -> Snapshot:
    """The tree as of ``revision`` (default: current). ``subtree`` keeps that node, its
    ancestors and everything below it; ``paths`` keeps exactly those nodes (a host's chain
    for a render). Both filter in SQL, so a 100k-host tree is not read for one host.
    ``keys`` loads only those variables (every node still): a key's emitted value depends
    only on that key's own values along the chain, so ``_emitted`` stays exact for them
    (``ais.varsearch``). Such a snapshot must not be exported: the other keys are missing."""
    rev = revision if revision is not None else current_revision(session)
    node_filter = None
    if subtree:
        root = session.scalar(select(Node).where(Node.path == subtree, _live_at(Node, rev)))
        if root is None:
            raise KeyError(f"subtree {subtree!r} does not exist")
        node_filter = or_(Node.path.in_(root.ancestors_paths()), _under(session, subtree))
    elif paths is not None:
        node_filter = Node.path.in_(paths)
    stmt_nodes = select(Node).where(_live_at(Node, rev))
    if node_filter is not None:
        stmt_nodes = stmt_nodes.where(node_filter)
    nodes = {n.id: n for n in session.scalars(stmt_nodes).all()}
    by_path = {n.path: n for n in nodes.values()}

    stmt = select(NodeVariable).where(_live_at(NodeVariable, rev))
    if keys is not None:
        stmt = stmt.where(NodeVariable.key.in_(keys))
    if node_filter is not None:
        stmt = stmt.join(Node, Node.id == NodeVariable.node_id).where(_live_at(Node, rev), node_filter)
    vars_by_node: dict[int, dict[str, Any]] = {}
    for nv in session.scalars(stmt):
        if nv.node_id in nodes:
            vars_by_node.setdefault(nv.node_id, {})[nv.key] = nv.value

    strategies = {d.name: d.merge_strategy for d in session.scalars(select(VariableDefinition))}

    tags: dict[str, list[int]] = {}
    for tag, member in session.execute(select(Tag, TagMember).join(TagMember, TagMember.tag_id == Tag.id)):
        if member.node_id in nodes:
            tags.setdefault(tag.name, []).append(member.node_id)

    return Snapshot(rev, nodes, by_path, vars_by_node, strategies, tags)


def host_path(session: Session, host: str, revision: int) -> str | None:
    """Path of the host named ``host`` as of ``revision``, or None."""
    return session.scalar(select(Node.path).where(Node.kind == NodeKind.host, Node.name == host, _live_at(Node, revision)))


def load_chain(session: Session, revision: int | None = None, *, host: str | None = None, path: str | None = None) -> Snapshot:
    """The snapshot ``resolve`` needs for one real or mock host: just its ancestor chain.
    A host's emitted variables depend only on its ancestors (the fold runs down the chain),
    so this resolves exactly like the full tree. KeyError when the host/path is unknown."""
    rev = revision if revision is not None else current_revision(session)
    if host is not None:
        anchor = host_path(session, host, rev)
        if anchor is None:
            raise KeyError(f"unknown host {host!r}")
    elif path is not None:
        anchor = path
    else:
        raise ValueError("host or path is required")
    parts = anchor.split(PATH_SEP)
    snap = load_snapshot(session, rev, paths=[PATH_SEP.join(parts[: i + 1]) for i in range(len(parts))])
    if anchor not in snap.by_path:
        raise KeyError(f"unknown path {path!r}")
    return snap


# ----------------------------------------------------------------------- naming


def sanitize(name: str) -> str:
    return _UNSAFE.sub("_", name)


def group_name(node: Node, sep: str = GROUP_SEP) -> str:
    if node.kind is NodeKind.all:
        return "all"
    if node.inventory_name:
        return sanitize(node.inventory_name)
    parts = node.path.split(PATH_SEP)[1:]  # drop "all"
    return sanitize(sep.join(parts))


def tag_group_name(node: Node, sep: str = GROUP_SEP) -> str:
    return sanitize(f"{node.kind.value}{sep}{node.name}")


# ----------------------------------------------------------------------- export


def emitted_vars(snap: Snapshot, node: Node) -> dict[str, Any]:
    """Variables to emit on this node: own replace-vars as-is, fold-vars as the
    cumulative fold from the root to this node (only when this node contributes)."""
    return copy.deepcopy(_emitted(snap, node))


def _emitted(snap: Snapshot, node: Node) -> dict[str, Any]:
    """``emitted_vars``, computed once per node and snapshot. Shared: never mutate it
    (``build_export`` only serializes it; ``resolve`` copies what it hands out)."""
    cached = snap._emitted.get(node.id)
    if cached is not None:
        return cached
    own = snap.vars_by_node.get(node.id, {})
    out: dict[str, Any] = {}
    chain = snap.chain(node)
    for key, value in own.items():
        strategy = snap.strategy(key)
        if strategy is MergeStrategy.replace:
            out[key] = copy.deepcopy(value)
            continue
        acc = None
        for anc in chain:
            if key in snap.vars_by_node.get(anc.id, {}):
                acc = fold(strategy, acc, snap.vars_by_node[anc.id][key])
        out[key] = acc
    snap._emitted[node.id] = out
    return out


def build_export(snap: Snapshot, *, sep: str = GROUP_SEP, emit_tags: bool = True) -> dict[str, Any]:
    groups: dict[str, dict[str, Any]] = {}
    hosts: dict[str, dict[str, Any]] = {}
    seen_groups: dict[str, str] = {}

    for node in snap.nodes.values():
        if node.kind is NodeKind.host:
            if node.name in hosts:
                raise ValueError(f"duplicate host name {node.name!r}")
            hosts[node.name] = {"path": node.path, "vars": _emitted(snap, node)}
            continue
        gname = group_name(node, sep)
        if gname in seen_groups and seen_groups[gname] != node.path:
            raise ValueError(f"group name collision: {gname!r} from {seen_groups[gname]} and {node.path}")
        seen_groups[gname] = node.path
        kids = snap.children(node)
        groups[gname] = {
            "path": node.path,
            "children": [group_name(k, sep) for k in kids if k.kind is not NodeKind.host],
            "hosts": [k.name for k in kids if k.kind is NodeKind.host],
            "vars": _emitted(snap, node),
        }

    tags: dict[str, list[str]] = {}
    if emit_tags:
        # Hosts under each node that needs them (tagged kinds, tag members), in one pass over
        # the hosts and their ancestor paths instead of one scan of every node per tag.
        wanted = {n.path for n in snap.nodes.values() if n.kind in TAGGED_KINDS}
        wanted.update(snap.nodes[nid].path for ids in snap.tags.values() for nid in ids)
        under: dict[str, list[str]] = {}
        for h in snap.nodes.values():
            if h.kind is NodeKind.host:
                for anc in h.ancestors_paths()[:-1]:
                    if anc in wanted:
                        under.setdefault(anc, []).append(h.name)

        def hosts_under(n: Node) -> list[str]:
            return sorted(under.get(n.path, ()))

        for node in snap.nodes.values():
            if node.kind in TAGGED_KINDS:
                tags.setdefault(tag_group_name(node, sep), []).extend(hosts_under(node))
        for tname, node_ids in snap.tags.items():
            members: list[str] = []
            for nid in node_ids:
                n = snap.nodes[nid]
                members.extend([n.name] if n.kind is NodeKind.host else hosts_under(n))
            tags.setdefault(sanitize(tname), []).extend(members)
        for tname in list(tags):
            if tname in groups:
                raise ValueError(f"tag group {tname!r} collides with a hierarchy group")
            tags[tname] = sorted(set(tags[tname]))

    return {"revision": snap.revision, "groups": groups, "hosts": hosts, "tags": tags}


# ---------------------------------------------------------------------- resolve


def resolved_vars(snap: Snapshot, node: Node) -> dict[str, Any]:
    """``resolve(snap, host=...)["resolved"]`` without provenance or the defensive deep
    copy, for the background snapshot pass (100k hosts). The result shares values with
    the snapshot's per-node cache: read it, hash it, serialize it, never mutate it.
    ``tests/test_snapshots.py`` pins it equal to ``resolve``."""
    result: dict[str, Any] = {"inventory_revision": snap.revision}
    for anc in snap.chain(node):
        result = combine_vars(result, _emitted(snap, anc))
    return result


def resolve(
    snap: Snapshot,
    *,
    host: str | None = None,
    path: str | None = None,
    overrides: dict[str, Any] | None = None,
    sep: str = GROUP_SEP,
) -> dict[str, Any]:
    """Effective variables for a real host or a mock host at ``path``.

    Walks the chain root-first applying the same emitted vars the plugin would, with
    Ansible's ``combine_vars`` (replace semantics), so the answer is exactly what a
    playbook sees. ``overrides`` are applied last as if they were host vars.
    """
    if host is not None:
        node = snap.host(host)
        if node is None:
            raise KeyError(f"unknown host {host!r}")
        chain = snap.chain(node)
    elif path is not None:
        anchor = snap.by_path.get(path)
        if anchor is None:
            raise KeyError(f"unknown path {path!r}")
        chain = snap.chain(anchor)
    else:
        raise ValueError("host or path is required")

    result: dict[str, Any] = {"inventory_revision": snap.revision}
    provenance: dict[str, list[dict[str, Any]]] = {
        "inventory_revision": [{"path": "all", "group": "all", "strategy": "replace", "value": snap.revision}]
    }
    for node in chain:
        emitted = _emitted(snap, node)
        result = combine_vars(result, emitted)
        for key in emitted:
            provenance.setdefault(key, []).append(
                {
                    "path": node.path,
                    "group": node.name if node.kind is NodeKind.host else group_name(node, sep),
                    "strategy": snap.strategy(key).value,
                    "value": copy.deepcopy(snap.vars_by_node[node.id][key]),
                }
            )
    if overrides:
        result = combine_vars(result, overrides)
        for key, value in overrides.items():
            provenance.setdefault(key, []).append(
                {"path": None, "group": "<override>", "strategy": "replace", "value": value}
            )

    return {
        "revision": snap.revision,
        "host": host,
        "path": chain[-1].path,
        "chain": [group_name(n, sep) if n.kind is not NodeKind.host else n.name for n in chain],
        "resolved": copy.deepcopy(result),  # values come from the shared per-node cache
        "provenance": provenance,
    }

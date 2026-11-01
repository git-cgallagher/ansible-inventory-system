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
from sqlalchemy import select
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
    """Everything the exporter needs, read as of one revision."""

    revision: int
    nodes: dict[int, Node]
    by_path: dict[str, Node]
    vars_by_node: dict[int, dict[str, Any]]
    strategies: dict[str, MergeStrategy]
    tags: dict[str, list[int]] = field(default_factory=dict)  # tag name -> node ids

    def children(self, node: Node) -> list[Node]:
        return sorted((n for n in self.nodes.values() if n.parent_id == node.id), key=lambda n: n.name)

    def chain(self, node: Node) -> list[Node]:
        """Root-first list of nodes from ``all`` down to ``node``."""
        return [self.by_path[p] for p in node.ancestors_paths()]

    def strategy(self, key: str) -> MergeStrategy:
        return self.strategies.get(key, MergeStrategy.replace)


def current_revision(session: Session) -> int:
    rev = session.scalar(select(Revision.id).order_by(Revision.id.desc()).limit(1))
    return rev or 0


def load_snapshot(session: Session, revision: int | None = None, subtree: str | None = None) -> Snapshot:
    rev = revision if revision is not None else current_revision(session)
    stmt_nodes = select(Node).where(
        (Node.created_rev.is_(None)) | (Node.created_rev <= rev),
        (Node.deleted_rev.is_(None)) | (Node.deleted_rev > rev),
    )
    nodes = {n.id: n for n in session.scalars(stmt_nodes).all()}
    by_path = {n.path: n for n in nodes.values()}

    if subtree:
        root = by_path.get(subtree)
        if root is None:
            raise KeyError(f"subtree {subtree!r} does not exist")
        keep_paths = set(root.ancestors_paths())
        prefix = root.path + PATH_SEP
        selected = {
            n.id: n for n in nodes.values() if n.path in keep_paths or n.path.startswith(prefix)
        }
        nodes = selected
        by_path = {n.path: n for n in nodes.values()}

    stmt = select(NodeVariable).where(
        NodeVariable.created_rev <= rev,
        (NodeVariable.deleted_rev.is_(None)) | (NodeVariable.deleted_rev > rev),
    )
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
    return out


def build_export(snap: Snapshot, *, sep: str = GROUP_SEP, emit_tags: bool = True) -> dict[str, Any]:
    groups: dict[str, dict[str, Any]] = {}
    hosts: dict[str, dict[str, Any]] = {}
    seen_groups: dict[str, str] = {}

    for node in snap.nodes.values():
        if node.kind is NodeKind.host:
            if node.name in hosts:
                raise ValueError(f"duplicate host name {node.name!r}")
            hosts[node.name] = {"path": node.path, "vars": emitted_vars(snap, node)}
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
            "vars": emitted_vars(snap, node),
        }

    tags: dict[str, list[str]] = {}
    if emit_tags:
        def hosts_under(n: Node) -> list[str]:
            prefix = n.path + PATH_SEP
            return sorted(h.name for h in snap.nodes.values() if h.kind is NodeKind.host and h.path.startswith(prefix))

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
        node = next((n for n in snap.nodes.values() if n.kind is NodeKind.host and n.name == host), None)
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
        emitted = emitted_vars(snap, node)
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
        "resolved": result,
        "provenance": provenance,
    }

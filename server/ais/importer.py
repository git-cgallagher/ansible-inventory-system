"""Bulk import of a YAML tree into the database.

Format::

    variables:                  # optional per-variable declarations
      packages: {merge_strategy: unique_append}
    all:
      vars: {ntp_server: time.example.com}
      children:
        commercial:             # kind inferred from depth unless given
          vars: {...}
          children:
            prod:
              children:
                dc1:
                  children:
                    web:
                      vars: {...}
                      hosts:
                        web01: {ansible_host: 10.0.0.1}
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ais import schema

from ais.models import (
    LEVEL_ORDER,
    NAME_RE,
    PATH_SEP,
    MergeStrategy,
    Node,
    NodeKind,
    NodeVariable,
    Revision,
    VariableDefinition,
)


class ImportError_(ValueError):
    pass


def infer_kind(depth: int, declared: str | None) -> NodeKind:
    if declared:
        return NodeKind(declared)
    if depth >= len(LEVEL_ORDER) - 1:
        return NodeKind.group
    return LEVEL_ORDER[depth]


def check_kind(parent: Node, kind: NodeKind) -> None:
    if parent.kind is NodeKind.host:
        raise ImportError_(f"host {parent.path} cannot have children")
    if kind is NodeKind.all:
        raise ImportError_("only the root can be of kind 'all'")
    p_idx = LEVEL_ORDER.index(parent.kind)
    c_idx = LEVEL_ORDER.index(kind)
    if kind is NodeKind.group and parent.kind is NodeKind.group:
        return
    if c_idx <= p_idx:
        raise ImportError_(f"{kind.value} {parent.path!r} child must be deeper than its parent kind {parent.kind.value}")


def check_name(name: str) -> None:
    if not NAME_RE.match(name):
        raise ImportError_(f"invalid node name {name!r}: letters, digits, '_' and '-' only (max 255)")


def live_node_by_path(session: Session, path: str) -> Node | None:
    return session.scalar(select(Node).where(Node.path == path, Node.deleted_rev.is_(None)))


def get_or_create_node(
    session: Session,
    parent: Node | None,
    name: str,
    kind: NodeKind,
    rev: Revision | None = None,
    inventory_name: str | None = None,
) -> Node:
    """Return the live node at ``parent/name``, creating it under ``rev`` if absent.

    An existing live node's kind must match: the importer never changes a node's
    kind or re-parents it.
    """
    check_name(name)
    if parent is None:
        path, depth = name, 0
    else:
        if parent.deleted_rev is not None:
            raise ImportError_(f"parent {parent.path} is deleted")
        check_kind(parent, kind)
        path, depth = f"{parent.path}{PATH_SEP}{name}", parent.depth + 1
    node = live_node_by_path(session, path)
    if node is None:
        node = Node(
            parent_id=parent.id if parent else None,
            kind=kind,
            name=name,
            path=path,
            depth=depth,
            inventory_name=inventory_name,
            meta={},
            created_rev=rev.id if rev is not None else None,
        )
        session.add(node)
        session.flush()
    elif node.kind is not kind:
        raise ImportError_(f"{path} exists as {node.kind.value}, not {kind.value}")
    return node


def set_variables(session: Session, node: Node, values: dict[str, Any], rev: Revision) -> None:
    live = {
        nv.key: nv
        for nv in session.scalars(select(NodeVariable).where(NodeVariable.node_id == node.id, NodeVariable.deleted_rev.is_(None)))
    }
    for key, value in values.items():
        current = live.get(key)
        if current is not None and current.value == value:
            continue
        if current is not None:
            current.deleted_rev = rev.id
        session.add(NodeVariable(node_id=node.id, key=key, value=value, created_rev=rev.id))


@dataclass
class ImportResult:
    """What one import did. ``changed`` is false when the tree already matched: the
    caller may then drop the revision (``POST /import`` does)."""

    revision: Revision
    nodes_created: int = 0
    variables_set: int = 0
    definitions_changed: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.nodes_created or self.variables_set or self.definitions_changed)


def import_tree(session: Session, data: dict[str, Any], *, actor: str = "import", message: str | None = None) -> Revision:
    return import_tree_report(session, data, actor=actor, message=message).revision


def import_tree_report(session: Session, data: dict[str, Any], *, actor: str = "import", message: str | None = None) -> ImportResult:
    """Import ``data`` under one new revision. Additive: creates missing nodes and sets
    the given variables and definitions; never deletes, moves or re-kinds anything."""
    if not isinstance(data, dict) or "all" not in data:
        raise ImportError_("top-level 'all' is required")
    rev = Revision(actor=actor, source="import", message=message or "bulk import")
    session.add(rev)
    session.flush()

    changed_defs: list[str] = []
    for name, spec in (data.get("variables") or {}).items():
        spec = spec or {}
        try:
            strategy = MergeStrategy(spec.get("merge_strategy", "replace"))
        except ValueError as exc:
            raise ImportError_(f"variable {name!r}: {exc}") from exc
        json_schema = spec.get("json_schema")
        if json_schema is not None:
            try:
                schema.check_schema(json_schema)
            except schema.SchemaViolation as exc:
                raise ImportError_(f"variable {name!r}: {exc}") from exc
        definition = session.get(VariableDefinition, name)
        wanted = (strategy, spec.get("description"), json_schema)
        if definition is not None and (definition.merge_strategy, definition.description, definition.json_schema) == wanted:
            continue
        definition = definition or VariableDefinition(name=name)
        definition.merge_strategy, definition.description, definition.json_schema = wanted
        session.add(definition)
        changed_defs.append(name)

    root = get_or_create_node(session, None, "all", NodeKind.all, rev)
    _import_node(session, root, data["all"], rev)
    session.flush()
    try:
        schema.check_live(session, keys=set(changed_defs), created_rev=rev.id)
    except schema.SchemaViolation as exc:
        raise ImportError_(str(exc)) from exc
    return ImportResult(
        revision=rev,
        nodes_created=session.scalar(select(func.count()).select_from(Node).where(Node.created_rev == rev.id)) or 0,
        variables_set=session.scalar(select(func.count()).select_from(NodeVariable).where(NodeVariable.created_rev == rev.id)) or 0,
        definitions_changed=sorted(changed_defs),
    )


def _import_node(session: Session, node: Node, spec: dict[str, Any], rev: Revision) -> None:
    spec = spec or {}
    set_variables(session, node, spec.get("vars") or {}, rev)
    for child_name, child_spec in (spec.get("children") or {}).items():
        child_spec = child_spec or {}
        kind = infer_kind(node.depth + 1, child_spec.get("kind"))
        child = get_or_create_node(session, node, child_name, kind, rev, child_spec.get("inventory_name"))
        _import_node(session, child, child_spec, rev)
    for host_name, host_vars in (spec.get("hosts") or {}).items():
        host = get_or_create_node(session, node, host_name, NodeKind.host, rev)
        set_variables(session, host, host_vars or {}, rev)


def import_file(session: Session, path: str | Path, **kw: Any) -> Revision:
    with open(path) as fh:
        data = yaml.safe_load(fh)
    return import_tree(session, data, **kw)

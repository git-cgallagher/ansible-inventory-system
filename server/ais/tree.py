"""Tree export: a subtree in the import format, at any revision.

``export_tree`` returns exactly the document ``POST /api/v1/import`` (its ``tree``) and
``ais import`` accept: ``variables`` (the declarations), ``all`` (nodes with their kinds,
``inventory_name`` and their **own** variables, never resolved or folded ones) and ``tags``
(member paths). Importing it reproduces the tree it came from, which is what makes backup,
restore (export at revision R, import), cloning (``as_path``) and GitOps one call each.
``tests/test_tree_export.py`` holds that round trip.

It reads through ``load_snapshot``, so a pinned revision sees the tree of that moment,
including a subtree deleted since. Two things are not temporal (AGENTS.md): variable
definitions and tags. An export at revision R carries today's definitions and today's tag
names and descriptions, with the memberships of the nodes that were live at R (``DELETE
/nodes`` leaves membership rows, so those of a deleted subtree come back).

Stored strings are returned verbatim: nothing here templates.
"""

from __future__ import annotations

import copy
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from ais.importer import live_node_by_path
from ais.models import NAME_RE, PATH_SEP, Node, NodeKind, Tag, VariableDefinition
from ais.resolver import Snapshot, load_snapshot

ROOT = "all"


class TreeExportError(ValueError):
    """A parameter the export cannot honour (an invalid ``as_path``, say)."""


def _check_as_path(subtree: str, as_path: str) -> list[str]:
    parts = as_path.split(PATH_SEP)
    if subtree == ROOT:
        raise TreeExportError("the root cannot be re-rooted: export a subtree to use 'as'")
    if len(parts) < 2 or parts[0] != ROOT:
        raise TreeExportError(f"'as' must be a path below 'all', such as all.commercial.prod.dc9, not {as_path!r}")
    for name in parts:
        if not NAME_RE.match(name):
            raise TreeExportError(f"invalid node name {name!r} in 'as': letters, digits, '_' and '-' only (max 255)")
    return parts


def _own_vars(snap: Snapshot, node: Node) -> dict[str, Any]:
    # A copy: the snapshot's values are the ORM rows' own objects.
    return {k: copy.deepcopy(v) for k, v in sorted(snap.vars_by_node.get(node.id, {}).items())}


def _node_doc(node: Node, own: dict[str, Any] | None) -> dict[str, Any]:
    """A non-host node's entry, keys in a fixed order (kind, inventory_name, vars)."""
    doc: dict[str, Any] = {}
    if node.kind is not NodeKind.all:
        doc["kind"] = node.kind.value  # always: a group under `all` would re-import as a business
    if node.inventory_name:
        doc["inventory_name"] = node.inventory_name
    if own:
        doc["vars"] = own
    return doc


def export_tree(
    session: Session,
    revision: int | None = None,
    subtree: str | None = None,
    *,
    as_path: str | None = None,
    ancestor_vars: bool = False,
    hosts: bool = True,
) -> dict[str, Any]:
    """The tree (or the subtree at ``subtree``) as of ``revision``, in the import format.

    A subtree export keeps the chain ``all`` → … → the subtree's parent, so the document
    still starts at ``all``. Those ancestors carry their kind and ``inventory_name`` only,
    unless ``ancestor_vars``: the import is additive, so their values would overwrite the
    destination's (a restore must not roll the parents back). With ``ancestor_vars`` the
    document is self-contained: imported into an empty database, every exported host
    resolves exactly as it did.

    ``as_path`` re-roots the subtree for a clone: the subtree's root becomes that path, every
    node and tag member below moves with it, and the new chain of ancestors is described by
    the live nodes at those paths (their kinds), or left to the importer's depth rule where
    none exists yet. ``hosts=False`` leaves the hosts (and their tag memberships) out, which
    a clone inside one inventory needs (host names are unique across the tree).

    Definitions: every one for a whole-tree export; for a subtree, those of the keys the
    document sets. Tags: those with a member in the exported subtree, with only those members.

    ``KeyError`` when ``subtree`` did not exist at ``revision``; ``TreeExportError`` for a
    bad ``as_path`` (or ``as_path`` with ``ancestor_vars``)."""
    subtree = subtree or ROOT
    new_parts = _check_as_path(subtree, as_path) if as_path else None
    if new_parts and ancestor_vars:
        raise TreeExportError("'as' and ancestor_vars do not combine: a clone keeps its new parents' values")
    snap = load_snapshot(session, revision, None if subtree == ROOT else subtree)
    top = snap.by_path.get(subtree)
    if top is None:  # only the root can be missing here: load_snapshot checked a subtree
        raise KeyError("the tree is empty (nothing has been imported)")

    def rewrite(path: str) -> str:
        if new_parts is None:
            return path
        return PATH_SEP.join(new_parts) + path[len(subtree) :]

    # The ancestors, root first: (name, kind or None, inventory_name, own vars).
    spine: list[tuple[str, NodeKind | None, str | None, dict[str, Any]]] = []
    if new_parts is None:
        for anc in snap.chain(top)[:-1]:
            spine.append((anc.name, anc.kind, anc.inventory_name, _own_vars(snap, anc) if ancestor_vars else {}))
    else:
        for i in range(1, len(new_parts)):
            live = live_node_by_path(session, PATH_SEP.join(new_parts[:i]))
            spine.append((new_parts[i - 1], live.kind if live else None, live.inventory_name if live else None, {}))

    used_keys: set[str] = set()
    doc_root: dict[str, Any] | None = None
    parent_doc: dict[str, Any] | None = None
    for name, kind, inventory_name, own in spine:
        doc: dict[str, Any] = {}
        if kind is not None and kind is not NodeKind.all:
            doc["kind"] = kind.value
        if inventory_name:
            doc["inventory_name"] = inventory_name
        if own:
            doc["vars"] = own
            used_keys.update(own)
        if parent_doc is None:
            doc_root = doc
        else:
            parent_doc["children"] = {name: doc}
        parent_doc = doc

    # The subtree itself, with an explicit stack (the tree may be deep; see importer).
    top_own = _own_vars(snap, top)
    used_keys.update(top_own)
    top_doc = _node_doc(top, top_own)
    if parent_doc is None:
        doc_root = top_doc
    else:
        parent_doc["children"] = {new_parts[-1] if new_parts else top.name: top_doc}
    exported: set[int] = {top.id}
    stack: list[tuple[Node, dict[str, Any]]] = [(top, top_doc)]
    while stack:
        node, doc = stack.pop()
        kids = snap.children(node)  # sorted by name
        groups = [k for k in kids if k.kind is not NodeKind.host]
        if groups:
            children = doc.setdefault("children", {})
            for kid in groups:
                own = _own_vars(snap, kid)
                used_keys.update(own)
                children[kid.name] = _node_doc(kid, own)
                exported.add(kid.id)
                stack.append((kid, children[kid.name]))
        if hosts:
            host_nodes = [k for k in kids if k.kind is NodeKind.host]
            if host_nodes:
                out = doc.setdefault("hosts", {})
                for kid in host_nodes:
                    own = _own_vars(snap, kid)
                    used_keys.update(own)
                    out[kid.name] = own
                    exported.add(kid.id)

    tree: dict[str, Any] = {}
    stmt = select(VariableDefinition).order_by(VariableDefinition.name)
    if subtree != ROOT:
        stmt = stmt.where(VariableDefinition.name.in_(sorted(used_keys))) if used_keys else None
    if stmt is not None:
        definitions: dict[str, Any] = {}
        for d in session.scalars(stmt):
            spec: dict[str, Any] = {"merge_strategy": d.merge_strategy.value}
            if d.description is not None:
                spec["description"] = d.description
            if d.json_schema is not None:
                spec["json_schema"] = copy.deepcopy(d.json_schema)
            definitions[d.name] = spec
        if definitions:
            tree["variables"] = definitions

    tree["all"] = doc_root

    members: dict[str, list[str]] = {}
    for tag_name, node_ids in snap.tags.items():
        paths = sorted(rewrite(snap.nodes[i].path) for i in node_ids if i in exported)
        if paths:
            members[tag_name] = paths
    if members:
        descriptions = {name: d for name, d in session.execute(select(Tag.name, Tag.description).where(Tag.name.in_(sorted(members))))}
        tags: dict[str, Any] = {}
        for tag_name in sorted(members):
            spec = {"members": members[tag_name]}
            if descriptions.get(tag_name) is not None:
                spec = {"description": descriptions[tag_name], **spec}
            tags[tag_name] = spec
        tree["tags"] = tags
    return tree


_Dumper = getattr(yaml, "CSafeDumper", yaml.SafeDumper)  # libyaml when present: 19 MB in pure Python is slow


def dump_yaml(tree: dict[str, Any]) -> str:
    """YAML that ``ais import`` reads back to the same data: safe types only, keys in the
    export's order (stable output for git), no line folding (Jinja stays on one line)."""
    return yaml.dump(tree, Dumper=_Dumper, sort_keys=False, allow_unicode=True, default_flow_style=False, width=1_000_000)

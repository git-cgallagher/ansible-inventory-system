"""Bulk import of a YAML tree into the database.

Format::

    variables:                  # optional per-variable declarations
      packages: {merge_strategy: unique_append}
    profiles:                   # optional: named bundles of variables (issue #22)
      gpu-a100: {description: A100 nodes, vars: {gpu_driver: "560.35"}}
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
                      profiles: [gpu-a100]   # attached, in order (non-host nodes)
                      vars: {...}
                      hosts:
                        web01: {ansible_host: 10.0.0.1}
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ais import schema
from ais.audit import current_change
from ais.resolver import qualified_name, sanitize

from ais.models import (
    LEVEL_ORDER,
    NAME_RE,
    PATH_SEP,
    MergeStrategy,
    Node,
    NodeKind,
    NodeVariable,
    Revision,
    Tag,
    TagMember,
    TAGGED_KINDS,
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


# One name rule for nodes and tags: ``NAME_RE`` (letters, digits, ``_`` and ``-``). A tag is
# emitted as a group named ``resolver.sanitize(name)``, exactly as a node's ``-`` becomes ``_``
# in its group name (``gpu-canary`` -> ``gpu_canary``). The exporter's automatic tag groups are
# ``<kind>_<name>`` (``tier_prod``); a tag whose group name has that shape would silently merge
# into one, so those prefixes (and ``all``) are reserved, checked on the group name.
RESERVED_TAG_PREFIXES = tuple(f"{k.value}_" for k in sorted(TAGGED_KINDS, key=lambda k: k.value))


def tag_name_error(name: str) -> str | None:
    """Why ``name`` cannot be a tag, or None. Shared by ``PUT /tags`` and the importer."""
    if not isinstance(name, str) or not NAME_RE.match(name):
        return f"invalid tag name {name!r}: letters, digits, '_' and '-' only (max 255)"
    group = sanitize(name)
    if group.startswith(RESERVED_TAG_PREFIXES) or group == "all":
        shown = f"{name!r}" if group == name else f"{name!r} (group {group!r})"
        return f"tag name {shown} is reserved (prefixes {', '.join(RESERVED_TAG_PREFIXES)} are automatic tag groups)"
    return None


def hierarchy_groups(session: Session) -> dict[str, str]:
    """Group name -> path of every live non-host node (default separator)."""
    rows = session.execute(select(Node.path, Node.kind, Node.inventory_name).where(Node.deleted_rev.is_(None), Node.kind != NodeKind.host))
    return {qualified_name(path, kind, inventory_name): path for path, kind, inventory_name in rows}


def tag_conflict_error(session: Session, name: str, groups: dict[str, str] | None = None) -> str | None:
    """Why the group tag ``name`` would be emitted as is already taken, or None: by another
    tag whose name maps to the same group (``gpu-canary`` and ``gpu_canary``), or by a live
    hierarchy group (``group_name``, ``inventory_name`` included; ``groups`` from
    ``hierarchy_groups`` when the caller checks several tags). Checked whether or not the tag
    has members, so the tag table never holds two tags for one group. Uses the default group
    separator, like ``check_export``; ``build_export`` refuses the same collisions as a
    backstop (409) for writes that do not come through here (node writes). Shared by
    ``converge_tag`` (``PUT /tags``, revert) and the importer."""
    group = sanitize(name)
    for other in session.scalars(select(Tag.name).where(Tag.name != name)):
        if sanitize(other) == group:
            return f"tag {name!r} would be emitted as group {group!r}, which tag {other!r} already is"
    path = (hierarchy_groups(session) if groups is None else groups).get(group)
    if path is not None:
        return f"tag {name!r} would be emitted as group {group!r}, which is the hierarchy group of {path}"
    return None


def check_name(name: str) -> None:
    if not NAME_RE.match(name):
        raise ImportError_(f"invalid node name {name!r}: letters, digits, '_' and '-' only (max 255)")


# Hard ceiling on a node's ``path`` in UTF-8 bytes, on every backend. On PostgreSQL the
# GiST index on ``path_ltree`` stores two whole paths in each internal key and needs two
# keys per 8 kB page, so long paths fail with "failed to add item to index page" (a 500).
# Measured 2026-11-01 on PostgreSQL 16: one 2,431-byte path fails; 40 sibling paths of
# 2,000 bytes fail; 200 of 1,500 and 500 of 1,024 bytes work. 1,500 keeps a margin. The
# btree indexes on ``path`` (~2,700-byte entries) are never the binding limit.
MAX_PATH_BYTES = 1500
DEFAULT_MAX_DEPTH = 32


def max_depth() -> int:
    """Deepest allowed node (``all`` is depth 0, a business 1, a tier 2, a datacenter 3,
    groups and hosts below). ``AIS_MAX_DEPTH`` overrides the default of 32."""
    try:
        value = int(os.environ.get("AIS_MAX_DEPTH", DEFAULT_MAX_DEPTH))
    except ValueError:
        return DEFAULT_MAX_DEPTH
    return max(value, 4)


def check_position(path: str, depth: int) -> None:
    """Refuse a node that would be deeper than ``max_depth()`` or whose path is longer
    than ``MAX_PATH_BYTES``. The one check behind import, create and move."""
    limit = max_depth()
    if depth > limit:
        raise ImportError_(f"{path}: depth {depth} exceeds the maximum depth {limit} (AIS_MAX_DEPTH)")
    size = len(path.encode("utf-8"))
    if size > MAX_PATH_BYTES:
        raise ImportError_(f"{path[:80]}...: path is {size} bytes, over the {MAX_PATH_BYTES}-byte limit")


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
    check_position(path, depth)
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
    tags_changed: list[str] = field(default_factory=list)
    # What the changed definitions and tags were before (None: did not exist). Definitions and
    # tags are not temporal, so the ``import`` audit row records this as its ``before``;
    # it is what a revert of the import puts back (``ais.revert``).
    definitions_before: dict[str, Any] = field(default_factory=dict)
    tags_before: dict[str, Any] = field(default_factory=dict)
    # Profiles created or changed, and nodes whose profile list changed (temporal rows: a
    # revert reads them back from the rows, the audit row needs no ``before`` for them).
    profiles_changed: list[str] = field(default_factory=list)
    attachments_changed: int = 0

    @property
    def changed(self) -> bool:
        return bool(
            self.nodes_created or self.variables_set or self.definitions_changed or self.tags_changed or self.profiles_changed or self.attachments_changed
        )

    def summary(self) -> dict[str, Any]:
        """The counts ``POST /import``, ``ais import`` and the audit row report."""
        return {
            "nodes_created": self.nodes_created,
            "variables_set": self.variables_set,
            "definitions": self.definitions_changed,
            "tags": self.tags_changed,
            "profiles": self.profiles_changed,
            "attachments": self.attachments_changed,
        }

    def before(self) -> dict[str, Any]:
        """The ``import`` audit row's ``before``: the changed definitions and tags as they
        were (``ais.revert`` needs it; nodes and values are temporal rows already)."""
        return {"definitions": self.definitions_before, "tags": self.tags_before}


def definition_doc(d: VariableDefinition | None) -> dict[str, Any] | None:
    """A definition as the API and the audit log show it (``definition.set`` rows)."""
    if d is None:
        return None
    return {"name": d.name, "merge_strategy": d.merge_strategy.value, "description": d.description, "json_schema": d.json_schema}


def tag_doc(session: Session, tag: Tag | None) -> dict[str, Any] | None:
    """A tag as the API and the audit log show it: name, description, live member paths."""
    if tag is None:
        return None
    members = session.scalars(
        select(Node.path).join(TagMember, TagMember.node_id == Node.id).where(TagMember.tag_id == tag.id, Node.deleted_rev.is_(None))
    )
    return {"name": tag.name, "description": tag.description, "members": sorted(members)}


def import_tree(session: Session, data: dict[str, Any], *, actor: str = "import", message: str | None = None) -> Revision:
    return import_tree_report(session, data, actor=actor, message=message).revision


def import_tree_report(
    session: Session,
    data: dict[str, Any],
    *,
    actor: str = "import",
    message: str | None = None,
    rev: Revision | None = None,
    profile_values: bool = True,
) -> ImportResult:
    """Import ``data`` under one new revision (or under ``rev``, which a revert passes so
    that its restores share its one revision; the counts are then this import's own).
    Additive: creates missing nodes and sets the given variables, definitions and tag
    memberships; never deletes, moves or re-kinds anything, and never removes a tag member.

    Profiles: a missing profile is created; an existing one gets the given ``description``
    and the given variables set (additive: a key the file leaves out stays). A node's
    ``profiles`` list, when given, is set exactly (attach, detach, reorder). With
    ``profile_values=False`` (a restore) the ``profiles:`` block only has to name live
    profiles and never changes them: a shared profile is not rolled back by restoring one
    subtree, the way the export's ancestors are structure only."""
    if not isinstance(data, dict) or "all" not in data:
        raise ImportError_("top-level 'all' is required")
    nodes_before = variables_before = 0
    if rev is None:
        rev = Revision(actor=actor, source="import", message=message or "bulk import", change=current_change())
        session.add(rev)
        session.flush()
    else:
        nodes_before, variables_before = _created_counts(session, rev)

    changed_defs: list[str] = []
    defs_before: dict[str, Any] = {}
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
        defs_before[name] = definition_doc(definition)
        definition = definition or VariableDefinition(name=name)
        definition.merge_strategy, definition.description, definition.json_schema = wanted
        session.add(definition)
        changed_defs.append(name)

    changed_profiles = _import_profiles(session, data.get("profiles"), rev, converge=profile_values)
    root = get_or_create_node(session, None, "all", NodeKind.all, rev)
    attachments = _import_node(session, root, data["all"], rev)
    session.flush()
    tags_before: dict[str, Any] = {}
    changed_tags = _import_tags(session, data.get("tags"), tags_before)
    try:
        schema.check_live(session, keys=set(changed_defs), created_rev=rev.id)
    except schema.SchemaViolation as exc:
        raise ImportError_(str(exc)) from exc
    nodes_after, variables_after = _created_counts(session, rev)
    return ImportResult(
        revision=rev,
        nodes_created=nodes_after - nodes_before,
        variables_set=variables_after - variables_before,
        definitions_changed=sorted(changed_defs),
        tags_changed=sorted(changed_tags),
        definitions_before={k: defs_before[k] for k in sorted(changed_defs)},
        tags_before={k: tags_before[k] for k in sorted(changed_tags)},
        profiles_changed=sorted(changed_profiles),
        attachments_changed=attachments,
    )


def _created_counts(session: Session, rev: Revision) -> tuple[int, int]:
    session.flush()
    return (
        session.scalar(select(func.count()).select_from(Node).where(Node.created_rev == rev.id)) or 0,
        session.scalar(select(func.count()).select_from(NodeVariable).where(NodeVariable.created_rev == rev.id)) or 0,
    )


# Members are looked up in chunks: PostgreSQL caps a statement at 65,535 bind parameters.
_PATH_CHUNK = 5000


def _import_tags(session: Session, spec: Any, before: dict[str, Any] | None = None) -> list[str]:
    """``tags: {name: {description, members: [path, ...]}}``, after the tree (members may
    be nodes the same import created). Additive: creates a missing tag, sets the
    description when the key is given, adds missing members; never removes one (``PUT
    /tags/{name}`` is the declarative way). Returns the names of the tags it changed and
    fills ``before`` with each changed tag as it was (``tag_doc``; None for a new one)."""
    before = {} if before is None else before
    if not spec:
        return []
    if not isinstance(spec, dict):
        raise ImportError_("'tags' must be a mapping of tag name to {description, members}")
    changed: list[str] = []
    groups = hierarchy_groups(session)  # after the tree: the groups this import created count
    for name, tag_spec in spec.items():
        error = tag_name_error(name) or tag_conflict_error(session, name, groups)
        if error:
            raise ImportError_(error)
        tag_spec = tag_spec or {}
        if not isinstance(tag_spec, dict):
            raise ImportError_(f"tag {name!r}: expected a mapping with 'description' and 'members'")
        members = tag_spec.get("members") or []
        if not isinstance(members, list) or not all(isinstance(m, str) for m in members):
            raise ImportError_(f"tag {name!r}: 'members' must be a list of node paths")
        nodes: dict[str, int] = {}
        wanted = sorted(set(members))
        for i in range(0, len(wanted), _PATH_CHUNK):
            chunk = wanted[i : i + _PATH_CHUNK]
            for path, node_id in session.execute(select(Node.path, Node.id).where(Node.path.in_(chunk), Node.deleted_rev.is_(None))):
                nodes[path] = node_id
        missing = [m for m in wanted if m not in nodes]
        if missing:
            raise ImportError_(f"tag {name!r}: node {missing[0]} does not exist")
        tag = session.scalar(select(Tag).where(Tag.name == name))
        was = tag_doc(session, tag)
        dirty = False
        if tag is None:
            tag = Tag(name=name, description=tag_spec.get("description"))
            session.add(tag)
            session.flush()
            dirty = True
        elif "description" in tag_spec and tag.description != tag_spec["description"]:
            tag.description = tag_spec["description"]
            dirty = True
        current = set(session.scalars(select(TagMember.node_id).where(TagMember.tag_id == tag.id)))
        for path in wanted:
            if nodes[path] not in current:
                session.add(TagMember(tag_id=tag.id, node_id=nodes[path]))
                dirty = True
        if dirty:
            changed.append(name)
            before[name] = was
    session.flush()
    return changed


def _import_profiles(session: Session, spec: Any, rev: Revision, *, converge: bool) -> list[str]:
    """``profiles: {name: {description, vars}}``, before the tree (nodes attach them).
    Returns the names it created or changed."""
    from ais import profiles

    if not spec:
        return []
    if not isinstance(spec, dict):
        raise ImportError_("'profiles' must be a mapping of profile name to {description, vars}")
    changed: list[str] = []
    for name, pspec in spec.items():
        error = profiles.name_error(name)
        if error:
            raise ImportError_(error)
        pspec = pspec or {}
        if not isinstance(pspec, dict) or not set(pspec) <= {"description", "vars"}:
            raise ImportError_(f"profile {name!r}: expected a mapping with 'description' and 'vars'")
        values = pspec.get("vars") or {}
        if not isinstance(values, dict):
            raise ImportError_(f"profile {name!r}: 'vars' must be a mapping")
        current = profiles.doc(session, name)
        if not converge:
            if current is None:
                raise ImportError_(f"profile {name!r} does not exist now: recreate it first")
            continue
        wanted = {
            "description": pspec["description"] if "description" in pspec else (current or {}).get("description"),
            "vars": {**((current or {}).get("vars") or {}), **values},
        }
        if current is not None and current["description"] == wanted["description"] and not profiles.changed_keys(current, wanted):
            continue
        try:
            profiles.set_profile(session, rev, name, wanted)
        except profiles.ProfileError as exc:
            raise ImportError_(str(exc)) from exc
        changed.append(name)
    return changed


def _set_profiles(session: Session, node: Node, spec: dict[str, Any], rev: Revision) -> int:
    """A node's ``profiles: [name, ...]``, set exactly when given; 1 when it changed."""
    from ais import profiles

    if "profiles" not in spec:
        return 0
    names = spec.get("profiles") or []
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise ImportError_(f"{node.path}: 'profiles' must be a list of profile names")
    try:
        before, after = profiles.set_node_profiles(session, rev, node, names)
    except profiles.ProfileError as exc:
        raise ImportError_(f"{node.path}: {exc}") from exc
    return int(before != after)


def _import_node(session: Session, root: Node, spec: dict[str, Any], rev: Revision) -> int:
    """Walk the tree depth first with an explicit stack, in the same order the recursive
    version used (a node's vars, then each child subtree in order, then its hosts), so
    depth is bounded by ``check_position`` and never by Python's recursion limit."""
    # ("node", node, spec): set the node's vars, then queue its hosts and children.
    # ("child", parent, (name, spec)): create the child, then handle it as a node.
    # ("hosts", node, spec): create the node's hosts once every child subtree is done.
    stack: list[tuple[str, Node, Any]] = [("node", root, spec or {})]
    attachments = 0
    while stack:
        step, node, item = stack.pop()
        if step == "hosts":
            for host_name, host_vars in (item.get("hosts") or {}).items():
                host = get_or_create_node(session, node, host_name, NodeKind.host, rev)
                set_variables(session, host, host_vars or {}, rev)
            continue
        if step == "child":
            name, child_spec = item
            kind = infer_kind(node.depth + 1, child_spec.get("kind"))
            node = get_or_create_node(session, node, name, kind, rev, child_spec.get("inventory_name"))
            item = child_spec
        set_variables(session, node, item.get("vars") or {}, rev)
        attachments += _set_profiles(session, node, item, rev)
        stack.append(("hosts", node, item))
        for child_name, child_spec in reversed(list((item.get("children") or {}).items())):
            stack.append(("child", node, (child_name, child_spec or {})))
    return attachments


def import_file(session: Session, path: str | Path, **kw: Any) -> Revision:
    with open(path) as fh:
        data = yaml.safe_load(fh)
    return import_tree(session, data, **kw)

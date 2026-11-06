"""Blast-radius preview: what a write would do to the hosts (issue #25).

Every write route's ``dry_run`` (and ``ais import``/``restore``/``revert --dry-run``) runs
the real write in its transaction, asks ``Preview.impact`` what changed for the hosts, and
rolls back. One helper for every kind of write, and nothing route-specific: the scope is
read back from the database.

How it works

1. ``Preview(session)`` is created after the write lock and **before** the write. It notes
   the base revision and the state that is not temporal (variable strategies, tag
   memberships by node id, ``inventory_name`` by node id): those rows are changed in place,
   so they cannot be read "as of the base revision" afterwards. Nodes and values are
   temporal rows and can.
2. The route writes under its new revision R (flushed, not committed).
3. ``impact(R)`` derives what R touched from the rows themselves (``created_rev == R`` /
   ``deleted_rev == R`` for nodes and values) and from the non-temporal state before and
   after, then resolves only the hosts and keys that can have changed, as of the base
   revision and as of R, and diffs them.

Bounding rules (what keeps a write at ``all`` from resolving 100k hosts in full)

* A value written or removed at path P (key k): the hosts under P, key k only.
* A node created, moved, deleted or restored (any node row opened or closed by R): the
  hosts under the topmost such paths, at the base revision and at R. A host present in
  both can only see different values for keys set on a **strict ancestor** of that root,
  or for keys whose rows R opened or closed inside it (a move re-creates every row, so a
  moved subtree counts each key it sets); created and deleted hosts are ``added`` /
  ``removed`` and need no values. Their groups are compared too.
* A merge strategy changed for k: the hosts under the nodes that set k, key k only. A
  schema- or description-only change touches no host.
* Profiles (issue #22; temporal rows, so read like values): a profile value written or
  removed (key k of profile P): the hosts under every node P is attached to, at the base
  revision and at R, key k only. A node's profile list changed (attachment rows opened or
  closed: attach, detach, reorder, and every move or delete): the hosts under that node,
  every key of the profiles involved. "Keys set on a strict ancestor" (structure) and "the
  nodes that set k" (strategies) count a profile attached there as setting its keys.
* An ``inventory_name`` or a tag membership changed: the hosts under those nodes, groups
  only.

Resolving uses light rows (``select`` of six columns, not ORM objects) for every non-host
node live at the revision plus the hosts in scope, and only the keys in scope, wrapped in a
``resolver.Snapshot`` so the fold is ``resolver._emitted``'s (no second fold, the
``ais.varsearch`` rule: a host's value is the emitted value of the deepest node on its
chain that sets the key). Such a snapshot must never be exported. Values are compared as
canonical JSON (``1`` is not ``true``).

The answer (``impact``), the same shape for every write: exact counts (``total`` distinct
hosts affected; ``vars_changed``, ``added``, ``removed``, ``regrouped``, ``moved``), the
affected hosts sorted by path with their diff (``added``/``removed``/``changed`` keys with
``from``/``to``, ``joined``/``left`` groups, ``moved_from``), and per-group counts of hosts
that join or leave it; both lists are capped at ``limit`` (``truncated`` says when), the
counts never are.
"""

from __future__ import annotations

import copy
import heapq
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ais.audit import current_change
from ais.models import (
    PATH_SEP,
    TAGGED_KINDS,
    MergeStrategy,
    Node,
    NodeKind,
    NodeVariable,
    ProfileAttachment,
    ProfileVariable,
    Revision,
    Tag,
    TagMember,
    VariableDefinition,
)
from ais.resolver import Snapshot, _emitted, _live_at, _under, current_revision, group_name, load_profile_layers, sanitize, tag_group_name
from ais.varsearch import canonical

LIMIT = 200  # hosts (and groups) listed; the counts are always exact
_CHUNK = 500  # values per IN list (PostgreSQL caps a statement at 65,535 bind parameters)
_ROOT_QUERIES = 20  # more scope roots than this: read every host once and filter in Python
_COLS = (Node.id, Node.parent_id, Node.kind, Node.name, Node.path, Node.inventory_name)


def _chunks(items: list[Any], size: int = _CHUNK):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _ancestors(path: str) -> list[str]:
    parts = path.split(PATH_SEP)
    return [PATH_SEP.join(parts[: i + 1]) for i in range(len(parts))]


def _roots(paths: set[str]) -> set[str]:
    """The topmost of ``paths``: drop every path that lies under another one."""
    kept: set[str] = set()
    for p in sorted(paths, key=lambda p: p.count(PATH_SEP)):
        if not any(a in kept for a in _ancestors(p)):
            kept.add(p)
    return kept


def _within(path: str, roots: set[str]) -> bool:
    return any(a in roots for a in _ancestors(path))


@dataclass(eq=False)
class _Row:
    """A node as six plain columns; what ``resolver.Snapshot`` and the naming helpers read."""

    id: int
    parent_id: int | None
    kind: NodeKind
    name: str
    path: str
    inventory_name: str | None

    def ancestors_paths(self) -> list[str]:
        return _ancestors(self.path)


@dataclass
class _Fixed:
    """The state that is changed in place, not by temporal rows, as of one moment."""

    strategies: dict[str, MergeStrategy]
    tags: dict[str, set[int]]  # tag name -> member node ids
    names: dict[int, str]  # node id -> inventory_name (set ones only)

    @classmethod
    def read(cls, session: Session) -> _Fixed:
        tags: dict[str, set[int]] = {}
        for name, node_id in session.execute(select(Tag.name, TagMember.node_id).join(TagMember, TagMember.tag_id == Tag.id)):
            tags.setdefault(name, set()).add(node_id)
        return cls(
            strategies={d.name: d.merge_strategy for d in session.scalars(select(VariableDefinition))},
            tags=tags,
            names=dict(session.execute(select(Node.id, Node.inventory_name).where(Node.inventory_name.is_not(None))).all()),  # type: ignore[arg-type]
        )

    def strategy(self, key: str) -> MergeStrategy:
        return self.strategies.get(key, MergeStrategy.replace)


@dataclass
class _Scope:
    roots: set[str] | None  # hosts under these paths; None = every host
    keys: set[str] = field(default_factory=set)
    groups: bool = False
    structural: bool = False  # node rows opened or closed: the hosts differ between the two sides

    @property
    def empty(self) -> bool:
        return self.roots is not None and not self.roots


@dataclass
class _Host:
    path: str
    setters: dict[str, int]  # key -> id of the deepest node on the chain that sets it (shared, read-only)
    groups: frozenset[str]


@dataclass
class _State:
    """One side of the diff: the hosts in scope and how to read a key's value."""

    hosts: dict[str, _Host]
    snap: Snapshot

    def value(self, key: str, node_id: int) -> Any:
        return _emitted(self.snap, self.snap.nodes[node_id])[key]  # shared cache: read-only


def empty(limit: int = LIMIT) -> dict[str, Any]:
    """The impact of a write that changes nothing (a no-op ``PUT``)."""
    return {
        "total": 0,
        "vars_changed": 0,
        "added": 0,
        "removed": 0,
        "regrouped": 0,
        "moved": 0,
        "hosts": [],
        "groups": [],
        "groups_total": 0,
        "limit": limit,
        "truncated": False,
    }


class Preview:
    """Create after the write lock and before the write; call ``impact(rev)`` after it."""

    def __init__(self, session: Session):
        self.session = session
        self.base = current_revision(session)
        self.fixed = _Fixed.read(session)

    def revision(self, *, actor: str, source: str, message: str | None) -> Revision:
        """The dry run's revision, with the id the real write would get on SQLite: ``base + 1``,
        set explicitly so that on PostgreSQL the dry run takes no sequence value (a rolled-back
        insert still would) and the next real write is ``base + 1`` there too. Safe because the
        caller holds the write lock: no other revision can appear meanwhile."""
        rev = Revision(id=self.base + 1, actor=actor, source=source, message=message, change=current_change())
        self.session.add(rev)
        self.session.flush()
        return rev

    def impact(self, rev: int, limit: int = LIMIT) -> dict[str, Any]:
        session = self.session
        session.flush()
        after = _Fixed.read(session)
        scope = _scope(session, self.base, rev, self.fixed, after)
        if scope.empty:
            return empty(limit)
        before_rows = _rows(session, self.base, scope)
        # Unless R opened or closed node rows, both sides have the same nodes: read them once.
        after_rows = _rows(session, rev, scope) if scope.structural else before_rows
        return _diff(_capture(session, self.base, self.fixed, scope, before_rows), _capture(session, rev, after, scope, after_rows), limit)


# ------------------------------------------------------------------- scope


def _paths_of(session: Session, ids: set[int]) -> set[str]:
    out: set[str] = set()
    for chunk in _chunks(sorted(ids)):
        out.update(session.scalars(select(Node.path).where(Node.id.in_(chunk))))
    return out


def _profile_keys(session: Session, names: set[str], rev: int) -> set[str]:
    """Every key the profiles ``names`` set at ``rev``."""
    out: set[str] = set()
    for chunk in _chunks(sorted(names)):
        out.update(session.scalars(select(ProfileVariable.key).where(ProfileVariable.profile.in_(chunk), _live_at(ProfileVariable, rev)).distinct()))
    return out


def _attached_paths(session: Session, names: set[str], rev: int) -> set[str]:
    """Paths of the nodes the profiles ``names`` are attached to at ``rev``."""
    out: set[str] = set()
    for chunk in _chunks(sorted(names)):
        stmt = (
            select(Node.path)
            .join(ProfileAttachment, ProfileAttachment.node_id == Node.id)
            .where(ProfileAttachment.profile.in_(chunk), _live_at(ProfileAttachment, rev), _live_at(Node, rev))
        )
        out.update(session.scalars(stmt))
    return out


def _keys_on(session: Session, paths: set[str], rev: int) -> set[str]:
    """Keys set (live at ``rev``) on the nodes at ``paths``: their own and their profiles'."""
    out: set[str] = set()
    names: set[str] = set()
    for chunk in _chunks(sorted(paths)):
        stmt = (
            select(NodeVariable.key)
            .join(Node, Node.id == NodeVariable.node_id)
            .where(Node.path.in_(chunk), _live_at(Node, rev), _live_at(NodeVariable, rev))
            .distinct()
        )
        out.update(session.scalars(stmt))
        names.update(
            session.scalars(
                select(ProfileAttachment.profile)
                .join(Node, Node.id == ProfileAttachment.node_id)
                .where(Node.path.in_(chunk), _live_at(Node, rev), _live_at(ProfileAttachment, rev))
                .distinct()
            )
        )
    return out | (_profile_keys(session, names, rev) if names else set())


def _setters(session: Session, keys: set[str], rev: int) -> set[str]:
    """Paths of the nodes that set any of ``keys`` at ``rev`` (``ix_nodevar_key_live``),
    their own values or an attached profile's."""
    out: set[str] = set()
    names: set[str] = set()
    for chunk in _chunks(sorted(keys)):
        stmt = (
            select(Node.path)
            .join(NodeVariable, NodeVariable.node_id == Node.id)
            .where(NodeVariable.key.in_(chunk), _live_at(NodeVariable, rev), _live_at(Node, rev))
        )
        out.update(session.scalars(stmt))
        names.update(session.scalars(select(ProfileVariable.profile).where(ProfileVariable.key.in_(chunk), _live_at(ProfileVariable, rev)).distinct()))
    return out | (_attached_paths(session, names, rev) if names else set())


def _scope(session: Session, base: int, rev: int, before: _Fixed, after: _Fixed) -> _Scope:
    paths: set[str] = set()
    keys: set[str] = set()
    groups = False

    # Structure: node rows R opened or closed (create, move, delete, restore, import).
    moved = set(session.scalars(select(Node.path).where(or_(Node.created_rev == rev, Node.deleted_rev == rev))))
    if moved:
        tops = _roots(moved)
        paths |= tops
        groups = True
        above = {a for top in tops for a in _ancestors(top)[:-1]}
        keys |= _keys_on(session, above, base) | _keys_on(session, above, rev)

    # Values: rows R opened or closed. A move re-creates every row of its subtree, so this
    # counts each key set in it, which also catches a value changed inside a subtree that the
    # same revision moves (a revert, an import).
    stmt = (
        select(Node.path, NodeVariable.key)
        .join(Node, Node.id == NodeVariable.node_id)
        .where(or_(NodeVariable.created_rev == rev, NodeVariable.deleted_rev == rev))
        .distinct()
    )
    for path, key in session.execute(stmt):
        paths.add(path)
        keys.add(key)

    # Profile values: the hosts under every node the profile is attached to, on either side.
    touched: set[str] = set()
    for name, key in session.execute(
        select(ProfileVariable.profile, ProfileVariable.key).where(or_(ProfileVariable.created_rev == rev, ProfileVariable.deleted_rev == rev)).distinct()
    ):
        touched.add(name)
        keys.add(key)
    if touched:
        paths |= _attached_paths(session, touched, base) | _attached_paths(session, touched, rev)

    # Profile lists: the hosts under the node, every key of the profiles that came or went
    # (or moved in the order).
    relisted: set[str] = set()
    for path, name in session.execute(
        select(Node.path, ProfileAttachment.profile)
        .join(Node, Node.id == ProfileAttachment.node_id)
        .where(or_(ProfileAttachment.created_rev == rev, ProfileAttachment.deleted_rev == rev))
        .distinct()
    ):
        paths.add(path)
        relisted.add(name)
    if relisted:
        keys |= _profile_keys(session, relisted, base) | _profile_keys(session, relisted, rev)

    # Strategies: the hosts under the nodes that set the key.
    restrategized = {k for k in set(before.strategies) | set(after.strategies) if before.strategy(k) is not after.strategy(k)}
    if restrategized:
        keys |= restrategized
        paths |= _setters(session, restrategized, base) | _setters(session, restrategized, rev)

    # Group names and tag memberships: groups only.
    renamed = {i for i in set(before.names) | set(after.names) if before.names.get(i) != after.names.get(i)}
    retagged: set[int] = set()
    for name in set(before.tags) | set(after.tags):
        retagged |= before.tags.get(name, set()) ^ after.tags.get(name, set())
    if renamed or retagged:
        groups = True
        paths |= _paths_of(session, renamed | retagged)

    roots = _roots(paths)
    return _Scope(None if "all" in roots else roots, keys, groups, structural=bool(moved))


# ----------------------------------------------------------------- capture


def _host_filters(session: Session, scope: _Scope) -> list[Any]:
    """SQL filters selecting the hosts in scope (one query each), or ``[None]``: every host
    (then ``_rows`` filters in Python when the scope has many roots)."""
    if scope.roots is None or len(scope.roots) > _ROOT_QUERIES:
        return [None]
    return [or_(Node.path == r, _under(session, r)) for r in sorted(scope.roots)]


def _rows(session: Session, rev: int, scope: _Scope) -> tuple[list[_Row], list[_Row]]:
    """Every non-host node live at ``rev`` (the chains, the group names) and the hosts in scope."""
    live = _live_at(Node, rev)
    groups = [_Row(*r) for r in session.execute(select(*_COLS).where(live, Node.kind != NodeKind.host))]
    hosts: list[_Row] = []
    for f in _host_filters(session, scope):
        stmt = select(*_COLS).where(live, Node.kind == NodeKind.host)
        hosts.extend(_Row(*r) for r in session.execute(stmt if f is None else stmt.where(f)))
    if scope.roots is not None and len(scope.roots) > _ROOT_QUERIES:
        hosts = [h for h in hosts if _within(h.path, scope.roots)]
    return groups, hosts


def _capture(session: Session, rev: int, fixed: _Fixed, scope: _Scope, rows: tuple[list[_Row], list[_Row]]) -> _State:
    """Every host in scope as of ``rev``: its path, the node each scope key comes from, and
    (when the scope needs them) its groups. ``fixed`` is the non-temporal state as of
    ``rev``; ``rows`` is ``_rows(session, rev, scope)``."""
    group_rows, hosts = rows
    nodes: dict[int, _Row] = {}
    for g in group_rows:  # a copy per side: the inventory_name is that side's
        nodes[g.id] = _Row(g.id, g.parent_id, g.kind, g.name, g.path, fixed.names.get(g.id))
    nodes.update((h.id, h) for h in hosts)
    vars_by_node: dict[int, dict[str, Any]] = {}
    live = _live_at(Node, rev)
    for chunk in _chunks(sorted(scope.keys)):
        stmt = (
            select(NodeVariable.node_id, NodeVariable.key, NodeVariable.value)
            .join(Node, Node.id == NodeVariable.node_id)
            .where(NodeVariable.key.in_(chunk), _live_at(NodeVariable, rev), live)
        )
        # Group values always; host values only in scope (a key set on every host would
        # otherwise read 100k rows for a write to one of them).
        stmts = [stmt.where(Node.kind != NodeKind.host)]
        stmts += [stmt.where(Node.kind == NodeKind.host) if f is None else stmt.where(Node.kind == NodeKind.host, f) for f in _host_filters(session, scope)]
        for one in stmts:
            for node_id, key, value in session.execute(one):
                if node_id in nodes:
                    vars_by_node.setdefault(node_id, {})[key] = value
    tags: dict[str, list[int]] = {name: [i for i in ids if i in nodes] for name, ids in fixed.tags.items()}
    # Profiles attach to non-host nodes only, so every attachment's node is in ``nodes``.
    layers = load_profile_layers(session, rev, nodes, keys=scope.keys) if scope.keys else {}
    snap = Snapshot(rev, nodes, {n.path: n for n in nodes.values()}, vars_by_node, dict(fixed.strategies), tags, layers)  # type: ignore[arg-type]

    keys = sorted(scope.keys)
    setter_memo: dict[int, dict[str, int]] = {}
    no_setters: dict[str, int] = {}

    def setters(n: _Row) -> dict[str, int]:
        """Deepest node on ``n``'s chain (``n`` included) that sets each key. A node that sets
        none of them shares its parent's dict, so hosts under one setter share one object
        (``_diff`` compares each distinct pair once)."""
        got = setter_memo.get(n.id)
        if got is not None:
            return got
        parent = nodes.get(n.parent_id) if n.parent_id is not None else None
        out = setters(parent) if parent is not None else no_setters
        own = vars_by_node.get(n.id)
        attached = layers.get(n.id)
        if own or attached:
            mine = [k for k in keys if snap.sets(n.id, k)]
            if mine:
                out = dict(out)
                out.update((k, n.id) for k in mine)
        if n.kind is not NodeKind.host:
            setter_memo[n.id] = out
        return out

    by_tag: dict[int, list[str]] = {}
    for name, ids in tags.items():
        for i in ids:
            by_tag.setdefault(i, []).append(sanitize(name))
    group_memo: dict[int, frozenset[str]] = {}

    def groups_of(n: _Row) -> frozenset[str]:
        """The groups a host under (or at) ``n`` is in, ``all`` left out: hierarchy groups,
        the automatic tag groups of tagged kinds and the tags of ``n`` and its ancestors."""
        got = group_memo.get(n.id)
        if got is not None:
            return got
        parent = nodes.get(n.parent_id) if n.parent_id is not None else None
        out = set(groups_of(parent)) if parent is not None else set()
        if n.kind not in (NodeKind.all, NodeKind.host):
            out.add(group_name(n))  # type: ignore[arg-type]
        if n.kind in TAGGED_KINDS:
            out.add(tag_group_name(n))  # type: ignore[arg-type]
        out.update(by_tag.get(n.id, ()))
        frozen = frozenset(out)
        if n.kind is not NodeKind.host:
            group_memo[n.id] = frozen
        return frozen

    none: frozenset[str] = frozenset()
    out = {h.name: _Host(h.path, setters(h) if keys else no_setters, groups_of(h) if scope.groups else none) for h in hosts}
    return _State(out, snap)


# -------------------------------------------------------------------- diff

def _diff(before: _State, after: _State, limit: int) -> dict[str, Any]:
    key_memo: dict[tuple[str, int | None, int | None], tuple[str, Any, Any] | None] = {}

    def key_change(k: str, b: int | None, a: int | None) -> tuple[str, Any, Any] | None:
        """How key ``k`` differs between a host whose value comes from node ``b`` (before)
        and node ``a`` (after); None when it does not. Computed once per pair of setters."""
        memo_key = (k, b, a)
        if memo_key in key_memo:
            return key_memo[memo_key]
        if b is None:
            got: tuple[str, Any, Any] | None = ("added", None, after.value(k, a))  # type: ignore[arg-type]
        elif a is None:
            got = ("removed", before.value(k, b), None)
        else:
            bv, av = before.value(k, b), after.value(k, a)
            got = None if canonical(bv) == canonical(av) else ("changed", bv, av)
        key_memo[memo_key] = got
        return got

    pair_memo: dict[tuple[int, int], list[tuple[str, tuple[str, Any, Any]]]] = {}

    def var_changes(b: dict[str, int], a: dict[str, int]) -> list[tuple[str, tuple[str, Any, Any]]]:
        pair = (id(b), id(a))  # setter dicts are shared and alive for the whole diff
        got = pair_memo.get(pair)
        if got is None:
            got = []
            for k in sorted(set(b) | set(a)):
                change = key_change(k, b.get(k), a.get(k))
                if change is not None:
                    got.append((k, change))
            pair_memo[pair] = got
        return got

    affected: list[tuple[str, str, _Host | None, _Host | None, list[tuple[str, tuple[str, Any, Any]]]]] = []
    counts = Counter[str]()
    joined = Counter[str]()
    left = Counter[str]()
    for name in before.hosts.keys() | after.hosts.keys():
        b, a = before.hosts.get(name), after.hosts.get(name)
        if a is None:
            counts["removed"] += 1
            left.update(b.groups)  # type: ignore[union-attr]
            affected.append((b.path, name, b, None, []))  # type: ignore[union-attr]
            continue
        if b is None:
            counts["added"] += 1
            joined.update(a.groups)
            affected.append((a.path, name, None, a, []))
            continue
        changes = var_changes(b.setters, a.setters)
        regrouped = a.groups != b.groups
        if not (changes or regrouped or a.path != b.path):
            continue
        if changes:
            counts["vars_changed"] += 1
        if regrouped:
            counts["regrouped"] += 1
            joined.update(a.groups - b.groups)
            left.update(b.groups - a.groups)
        if a.path != b.path:
            counts["moved"] += 1
        affected.append((a.path, name, b, a, changes))

    def entry(path: str, name: str, b: _Host | None, a: _Host | None, changes: list[tuple[str, tuple[str, Any, Any]]]) -> dict[str, Any]:
        if a is None:
            return {"host": name, "path": path, "status": "removed"}
        if b is None:
            return {"host": name, "path": path, "status": "added"}
        out: dict[str, Any] = {
            "host": name,
            "path": path,
            "status": "changed",
            "added": {k: av for k, (kind, _bv, av) in changes if kind == "added"},
            "removed": {k: bv for k, (kind, bv, _av) in changes if kind == "removed"},
            "changed": {k: {"from": bv, "to": av} for k, (kind, bv, av) in changes if kind == "changed"},
        }
        if a.groups != b.groups:
            out["joined"] = sorted(a.groups - b.groups)
            out["left"] = sorted(b.groups - a.groups)
        if a.path != b.path:
            out["moved_from"] = b.path
        return out

    listed = heapq.nsmallest(limit, affected, key=lambda e: (e[0], e[1]))
    group_rows = [{"group": g, "joined": joined[g], "left": left[g]} for g in sorted(joined.keys() | left.keys())]
    out = empty(limit)
    out.update({k: counts[k] for k in ("vars_changed", "added", "removed", "regrouped", "moved")})
    out.update(
        total=len(affected),
        hosts=[copy.deepcopy(entry(*e)) for e in listed],  # values come from the shared caches
        groups=group_rows[:limit],
        groups_total=len(group_rows),
        truncated=len(affected) > limit or len(group_rows) > limit,
    )
    return out


def summary(impact: dict[str, Any]) -> str:
    """One line for the CLI: the counts that are not zero."""
    parts = [f"{impact['total']} host(s) affected"]
    for key, label in (("vars_changed", "vars changed"), ("added", "added"), ("removed", "removed"), ("regrouped", "change groups"), ("moved", "moved")):
        if impact.get(key):
            parts.append(f"{impact[key]} {label}")
    return ", ".join(parts)

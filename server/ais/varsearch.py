"""Variable search: where a key is set, and which hosts end up with which value.

Two questions an operator asks during an incident, both read-only and both as of one
revision (temporal rows, like everything in ``ais.resolver``):

* ``where_set``: the nodes that set ``key`` in their **own** variables (``GET /api/v1/vars``,
  ``ais vars``). One indexed query (``ix_nodevar_key_live``) joined to the nodes, nothing
  else is read. With ``subtree`` it keeps that node, everything below it and the ancestors
  that set the key (``above_subtree``), because those are what the subtree's hosts inherit.
* ``host_values``: every host's **resolved** value of ``key`` (``GET /api/v1/vars/hosts``,
  ``ais vars --hosts``). It loads the scope with ``load_snapshot(keys=[key])`` and takes each
  host's value from ``resolver._emitted`` at the deepest node of its chain that sets the key.
  That is exactly ``resolve(...)["resolved"][key]``: Ansible replaces top-level keys, and the
  exporter emits a fold variable's cumulative fold at every contributing node, so the deepest
  contributor's emitted value wins (``tests/test_varsearch.py`` checks it host by host against
  ``resolve``). No second resolver: the fold is ``_emitted``'s.

Profiles (issue #22) count as setting a key wherever they are attached: ``where_set`` lists
each attachment of a profile that holds the key as an item with ``profile`` and ``source``
(``profile:gpu-a100 @ <path>``), the node's own value (if any) after it, plus the profiles
that hold the key (``profiles``, attached or not); ``host_values`` says ``profile`` when a
host's value comes from a profile, and a fold's ``sources`` name each profile layer the
same way. The value a profile contributes is the profile's value; the fold runs over the
layers in application order (``Snapshot.layers``).

``value`` matching (both): the text is parsed as JSON (``550.90`` is a number, ``"550.90"`` a
string, ``[1, 2]`` a list); text that is not valid JSON is matched as a plain string
(``nvidia-550``). Values are compared as canonical JSON, so ``1`` does not match ``true`` and
key order in an object does not matter. ``where_set`` compares each node's own value (its
contribution); ``host_values`` compares the whole resolved value, which for ``merge`` /
``append`` / ``unique_append`` is the folded result (every contribution down the chain), not one
node's part of it.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ais.models import MergeStrategy, Node, NodeKind, NodeVariable, ProfileAttachment, ProfileVariable, VariableDefinition
from ais.resolver import _emitted, _live_at, _under, current_revision, fold, load_snapshot, profile_source

MAX_VALUES = 50  # distinct resolved values summarized in ``host_values``'s ``values``


@dataclass(frozen=True)
class Wanted:
    """A parsed ``value=`` query: ``given`` False means no value filter."""

    given: bool = False
    value: Any = None

    def matches(self, value: Any) -> bool:
        return not self.given or canonical(value) == canonical(self.value)


NO_VALUE = Wanted()


def parse_value(text: str | None) -> Wanted:
    """``value=`` as JSON, or as a plain string when it is not valid JSON; empty = no filter
    (send ``""`` to look for the empty string)."""
    if text is None or text == "":
        return NO_VALUE
    try:
        return Wanted(True, json.loads(text))
    except ValueError:
        return Wanted(True, text)


def canonical(value: Any) -> str:
    """Type-exact comparison key: ``True`` != ``1``, object key order ignored."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _strategy(session: Session, key: str) -> tuple[MergeStrategy, bool]:
    d = session.get(VariableDefinition, key)
    return (d.merge_strategy, True) if d is not None else (MergeStrategy.replace, False)


def _scope_root(session: Session, subtree: str, rev: int) -> Node:
    root = session.scalar(select(Node).where(Node.path == subtree, _live_at(Node, rev)))
    if root is None:
        raise KeyError(f"subtree {subtree!r} does not exist at revision {rev}")
    return root


def _head(key: str, rev: int, strategy: MergeStrategy, declared: bool, subtree: str | None, wanted: Wanted) -> dict[str, Any]:
    out: dict[str, Any] = {"revision": rev, "key": key, "strategy": strategy.value, "declared": declared, "subtree": subtree}
    if wanted.given:
        out["value"] = wanted.value
    return out


def where_set(
    session: Session,
    key: str,
    revision: int | None = None,
    *,
    subtree: str | None = None,
    wanted: Wanted = NO_VALUE,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """The nodes that set ``key`` themselves, by path. ``KeyError`` for an unknown subtree.

    Each item: ``id``, ``path``, ``kind``, ``value`` (the node's own value, or with ``profile``
    and ``source`` an attached profile's) and, for a fold
    strategy, ``effective`` (the cumulative fold the node emits, what its hosts get unless a
    deeper node contributes too). With ``subtree``, ancestors of it that set the key are
    listed first with ``above_subtree: true``."""
    rev = revision if revision is not None else current_revision(session)
    strategy, declared = _strategy(session, key)
    stmt = (
        select(Node, NodeVariable.value)
        .join(NodeVariable, NodeVariable.node_id == Node.id)
        .where(NodeVariable.key == key, _live_at(NodeVariable, rev), _live_at(Node, rev))
    )
    ancestors: set[str] = set()
    node_scope = None
    if subtree:
        root = _scope_root(session, subtree, rev)
        ancestors = set(root.ancestors_paths()[:-1])
        node_scope = or_(Node.path.in_(root.ancestors_paths()), _under(session, subtree))
        stmt = stmt.where(node_scope)
    # (node, layer order, profile or None, value): a node's profiles (by position), then its own.
    entries: list[tuple[Node, tuple[int, int], str | None, Any]] = [(node, (1, 0), None, value) for node, value in session.execute(stmt).all()]

    held = dict(session.execute(select(ProfileVariable.profile, ProfileVariable.value).where(ProfileVariable.key == key, _live_at(ProfileVariable, rev))).all())
    if held:
        pstmt = (
            select(Node, ProfileAttachment.profile, ProfileAttachment.position)
            .join(ProfileAttachment, ProfileAttachment.node_id == Node.id)
            .where(ProfileAttachment.profile.in_(sorted(held)), _live_at(ProfileAttachment, rev), _live_at(Node, rev))
        )
        if node_scope is not None:
            pstmt = pstmt.where(node_scope)
        entries += [(node, (0, position), name, held[name]) for node, name, position in session.execute(pstmt).all()]
    entries.sort(key=lambda e: (e[0].path, e[1]))

    # The fold each layer emits. Every ancestor that sets the key is in ``entries`` (the scope
    # keeps the subtree's ancestors), and entries are sorted by path then layer, so an
    # ancestor's fold is ready before its descendants'. Same rule as ``resolver._emitted``.
    effective: list[Any] = []
    if strategy is not MergeStrategy.replace:
        final: dict[str, Any] = {}
        for node, _order, _name, value in entries:
            if node.path in final:
                parent_fold = final[node.path]
            else:
                parent_fold = None
                for p in reversed(node.ancestors_paths()[:-1]):
                    if p in final:
                        parent_fold = final[p]
                        break
            final[node.path] = fold(strategy, parent_fold, value)
            effective.append(final[node.path])

    items: list[dict[str, Any]] = []
    for i, (node, _order, name, value) in enumerate(entries):
        if not wanted.matches(value):
            continue
        item: dict[str, Any] = {"id": node.id, "path": node.path, "kind": node.kind.value, "value": value}
        if name is not None:
            item["profile"] = name
            item["source"] = profile_source(name, node.path)
        if strategy is not MergeStrategy.replace:
            item["effective"] = effective[i]
        if subtree:
            item["above_subtree"] = node.path in ancestors
        items.append(item)
    return {
        **_head(key, rev, strategy, declared, subtree, wanted),
        "total": len(items),
        "limit": limit,
        "offset": offset,
        "nodes": items[offset : offset + limit],
        # Every profile that holds the key (attached or not), with its value.
        "profiles": [{"name": n, "value": v} for n, v in sorted(held.items()) if wanted.matches(v)],
    }


def host_values(
    session: Session,
    key: str,
    revision: int | None = None,
    *,
    subtree: str | None = None,
    wanted: Wanted = NO_VALUE,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """Every host in scope whose resolved ``key`` matches ``wanted`` (every host that has the
    key, without a value), sorted by path and paged. ``KeyError`` for an unknown subtree.

    Each item: ``host``, ``path``, ``value`` (resolved), ``from`` (the deepest node on the
    host's chain that sets the key: its emitted value is the host's) and, for a fold
    strategy, ``sources`` (every contributing node, root first). Counts cover the whole scope,
    not the page: ``total`` (matching), ``with_key``, ``without_key``; ``values`` is the
    distribution of resolved values among the hosts that have the key (the ``MAX_VALUES``
    most common, ``values_truncated`` when there were more)."""
    rev = revision if revision is not None else current_revision(session)
    if subtree:
        _scope_root(session, subtree, rev)
    snap = load_snapshot(session, rev, subtree, keys=[key])
    strategy = snap.strategy(key)
    declared = session.get(VariableDefinition, key) is not None
    folds = strategy is not MergeStrategy.replace

    items: list[dict[str, Any]] = []
    with_key = without_key = 0
    counts: Counter[str] = Counter()
    for node in snap.nodes.values():
        if node.kind is not NodeKind.host:
            continue
        contributors = [anc for anc in snap.chain(node) if snap.sets(anc.id, key)]
        if not contributors:
            without_key += 1
            continue
        with_key += 1
        value = _emitted(snap, contributors[-1])[key]  # shared cache: read-only from here on
        counts[canonical(value)] += 1
        if not wanted.matches(value):
            continue
        item: dict[str, Any] = {"host": node.name, "path": node.path, "value": value, "from": contributors[-1].path}
        top = [name for name, values in snap.layers(contributors[-1].id) if key in values][-1]
        if top is not None:  # the deepest contribution is a profile's (the node sets no own value)
            item["profile"] = top
            item["source"] = profile_source(top, contributors[-1].path)
        if folds:
            item["sources"] = [
                c.path if name is None else profile_source(name, c.path) for c in contributors for name, values in snap.layers(c.id) if key in values
            ]
        items.append(item)
    items.sort(key=lambda i: i["path"])
    common = counts.most_common(MAX_VALUES)
    return {
        **_head(key, rev, strategy, declared, subtree, wanted),
        "total": len(items),
        "with_key": with_key,
        "without_key": without_key,
        "values": [{"value": json.loads(c), "hosts": n} for c, n in common],
        "values_truncated": len(counts) > MAX_VALUES,
        "limit": limit,
        "offset": offset,
        "hosts": items[offset : offset + limit],
    }

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

from ais.models import MergeStrategy, Node, NodeKind, NodeVariable, VariableDefinition
from ais.resolver import _emitted, _live_at, _under, current_revision, fold, load_snapshot

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

    Each item: ``id``, ``path``, ``kind``, ``value`` (the node's own value) and, for a fold
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
    if subtree:
        root = _scope_root(session, subtree, rev)
        ancestors = set(root.ancestors_paths()[:-1])
        stmt = stmt.where(or_(Node.path.in_(root.ancestors_paths()), _under(session, subtree)))
    rows = sorted(session.execute(stmt).all(), key=lambda r: r[0].path)

    # The fold each setter emits. Every ancestor that sets the key is in ``rows`` (the scope
    # keeps the subtree's ancestors), and rows are sorted by path, so an ancestor's fold is
    # ready before its descendants'. Same rule as ``resolver._emitted``, for one key.
    effective: dict[str, Any] = {}
    if strategy is not MergeStrategy.replace:
        for node, value in rows:
            parent_fold = None
            for p in reversed(node.ancestors_paths()[:-1]):
                if p in effective:
                    parent_fold = effective[p]
                    break
            effective[node.path] = fold(strategy, parent_fold, value)

    items: list[dict[str, Any]] = []
    for node, value in rows:
        if not wanted.matches(value):
            continue
        item: dict[str, Any] = {"id": node.id, "path": node.path, "kind": node.kind.value, "value": value}
        if strategy is not MergeStrategy.replace:
            item["effective"] = effective[node.path]
        if subtree:
            item["above_subtree"] = node.path in ancestors
        items.append(item)
    return {
        **_head(key, rev, strategy, declared, subtree, wanted),
        "total": len(items),
        "limit": limit,
        "offset": offset,
        "nodes": items[offset : offset + limit],
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
        contributors = [anc for anc in snap.chain(node) if key in snap.vars_by_node.get(anc.id, ())]
        if not contributors:
            without_key += 1
            continue
        with_key += 1
        value = _emitted(snap, contributors[-1])[key]  # shared cache: read-only from here on
        counts[canonical(value)] += 1
        if not wanted.matches(value):
            continue
        item: dict[str, Any] = {"host": node.name, "path": node.path, "value": value, "from": contributors[-1].path}
        if folds:
            item["sources"] = [c.path for c in contributors]
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

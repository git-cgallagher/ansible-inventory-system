"""Undeclared keys that look like typos, and strict mode (issue #24).

A write that stores a value under a key with no variable definition still succeeds, but
when the key is *close* to a known one the response carries a structured warning::

    {"warnings": [{"key": "log_levle", "path": "all.commercial.staging",
                   "did_you_mean": "log_level", "declared": true, "message": "..."}],
     "warnings_total": 1}

Known keys (the candidates) are every declared name, plus undeclared keys that are *widely
used*: live on at least ``AIS_TYPO_MIN_NODES`` nodes (default 5; 0 turns this half off) and
on more nodes than the key being written, at most ``MAX_WIDELY_USED`` of them (the most used).
The "more nodes" rule makes the rare spelling the suspect, so two established keys never
flag each other.

Close means, after normalizing both keys (lower case, ``-`` as ``_``), an optimal string
alignment distance (Damerau-Levenshtein with adjacent transpositions, no substring edits) of
at most 2 to a declared key and at most 1 to a widely used one, and less for short keys: 0
(normalization only) when the shorter key has 3 characters or fewer, 1 up to 6. The tighter
limit for undeclared candidates keeps ``ansible_port`` from being "a typo of"
``ansible_host`` (distance 2), which every inventory sets on every host. Declaring a key
always silences its warning.

Strict mode (``AIS_STRICT_KEYS=true``, a server-wide setting) refuses a write that would
introduce a *new* undeclared key: one no node has before the write. Keys already in the tree
keep working (reads, renders, deletes, and new values), so strict mode can be turned on over
an existing inventory and tightened with ``ais lint``. Restores and reverts are never refused:
they put back what the tree held before (every write kind must stay revertible).

Profile variables (issue #22) count like node variables: a profile is one more place a key
is set (``profile:<name>`` in the warnings), and a profile write is reviewed like a node write.

The check runs after a write's rows are in the session, over the rows that write created
(``NodeVariable.created_rev``, ``ProfileVariable.created_rev``), like ``schema.check_live(created_rev=)``. Call sites: ``PUT
…/vars/{key}``, ``POST /nodes`` (its ``vars``), ``POST /import`` and ``ais import``,
``POST /nodes/restore`` and ``ais restore``, the revert routes and ``ais revert``.
"""

from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ais.models import Node, NodeVariable, ProfileVariable, VariableDefinition

STRICT_ENV = "AIS_STRICT_KEYS"
MIN_NODES_ENV = "AIS_TYPO_MIN_NODES"
DEFAULT_MIN_NODES = 5
MAX_WIDELY_USED = 1000
MAX_WARNINGS = 50
LINT_PATHS = 10

DECLARED_DISTANCE = 2
UNDECLARED_DISTANCE = 1


def strict_keys() -> bool:
    """``AIS_STRICT_KEYS``: true/1/yes/on refuses new undeclared keys."""
    return os.environ.get(STRICT_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def min_nodes() -> int:
    """``AIS_TYPO_MIN_NODES``: how many nodes make an undeclared key widely used (0 = off)."""
    try:
        return max(int(os.environ.get(MIN_NODES_ENV, DEFAULT_MIN_NODES)), 0)
    except ValueError:
        return DEFAULT_MIN_NODES


def normalize(key: str) -> str:
    return key.lower().replace("-", "_")


def length_limit(a: str, b: str) -> int:
    """The most edits two keys this long may differ by and still count as close."""
    n = min(len(a), len(b))
    if n <= 3:
        return 0
    if n <= 6:
        return 1
    return 2


def distance(a: str, b: str, limit: int) -> int:
    """Optimal string alignment distance between ``a`` and ``b`` (insert, delete, substitute,
    swap two adjacent characters; each 1), or ``limit + 1`` as soon as it must exceed
    ``limit``. Deterministic, no dependency, cheap on short strings."""
    if a == b:
        return 0
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev2: list[int] = []
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            best = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                best = min(best, prev2[j - 2] + 1)
            cur[j] = best
        if min(cur) > limit:
            return limit + 1
        prev2, prev = prev, cur
    return min(prev[len(b)], limit + 1)


@dataclass(frozen=True)
class Suggestion:
    name: str
    declared: bool
    nodes: int  # live nodes that set it (0 for a declared key nobody sets: not counted)
    distance: int

    def describe(self) -> str:
        return f"{self.name} (declared)" if self.declared else f"{self.name} (not declared either, set on {self.nodes} nodes)"


class Candidates:
    """The keys a written key is compared with: declared names, and the widely used
    undeclared keys. ``uses`` holds the live node count of *every* undeclared key."""

    def __init__(self, declared: set[str], uses: dict[str, int], threshold: int):
        self.declared = declared
        self.uses = uses
        widely = sorted(((n, k) for k, n in uses.items() if threshold and n >= threshold), key=lambda t: (-t[0], t[1]))
        self.widely = {k: n for n, k in widely[:MAX_WIDELY_USED]}
        self._by_len: dict[int, list[tuple[str, str, bool]]] = defaultdict(list)
        for name in declared:
            self._by_len[len(name)].append((name, normalize(name), True))
        for name in self.widely:
            self._by_len[len(name)].append((name, normalize(name), False))

    @classmethod
    def load(cls, session: Session) -> "Candidates":
        declared = set(session.scalars(select(VariableDefinition.name)))
        uses: dict[str, int] = {}
        for model in (NodeVariable, ProfileVariable):  # a profile counts as one place, like a node
            rows = session.execute(
                select(model.key, func.count()).where(model.deleted_rev.is_(None), model.key.not_in(select(VariableDefinition.name))).group_by(model.key)
            )
            for k, n in rows:
                uses[k] = uses.get(k, 0) + n
        return cls(declared, uses, min_nodes())

    def suggest(self, key: str) -> Suggestion | None:
        """The closest known key ``key`` may be a typo of, or None. Ties: smaller distance,
        then a declared key, then the most used, then by name."""
        if key in self.declared:
            return None
        own = self.uses.get(key, 0)
        norm = normalize(key)
        best: tuple[Any, ...] | None = None
        for size in range(len(key) - DECLARED_DISTANCE, len(key) + DECLARED_DISTANCE + 1):
            for name, name_norm, declared in self._by_len.get(size, ()):
                if name == key:
                    continue
                uses = self.widely.get(name, 0)
                if not declared and uses <= own:
                    continue
                limit = min(DECLARED_DISTANCE if declared else UNDECLARED_DISTANCE, length_limit(key, name))
                d = distance(norm, name_norm, limit)
                if d > limit:
                    continue
                rank = (d, not declared, -uses, name)
                if best is None or rank < best[0]:
                    best = (rank, Suggestion(name=name, declared=declared, nodes=uses, distance=d))
        return None if best is None else best[1]


class UndeclaredKeys(ValueError):
    """Strict mode refused new undeclared keys. ``detail`` is the 422 body's ``detail``."""

    def __init__(self, detail: dict[str, Any]):
        super().__init__(detail["message"])
        self.detail = detail


def _how_to_declare(keys: list[str]) -> str:
    name = keys[0] if len(keys) == 1 else "<name>"
    return (
        f"To add a new key, declare it first: PUT /api/v1/variables/{name}, the "
        "git_cgallagher.inventory.variable_definition module, a 'variables:' entry in the import, "
        "or the UI's Variables page"
    )


def _message(key: str, path: str, s: Suggestion) -> str:
    return f"{key} at {path} is not declared; did you mean {s.describe()}?"


@dataclass
class KeyReview:
    warnings: list[dict[str, Any]]
    total: int

    def body(self) -> dict[str, Any]:
        return {"warnings": self.warnings, "warnings_total": self.total}

    def lines(self) -> list[str]:
        """For the CLI: one line per warning, and how many were left out."""
        out = [f"warning: {w['message']}" for w in self.warnings]
        if self.total > len(self.warnings):
            out.append(f"warning: ... and {self.total - len(self.warnings)} more")
        return out


EMPTY = KeyReview([], 0)


def review(session: Session, rev_id: int, *, strict: bool, limit: int | None = None) -> KeyReview:
    """Warnings for the undeclared keys written under ``rev_id`` that look like typos, one
    per key and path, at most ``limit`` (every key's first path before any key's second),
    with the total. With ``strict``, raise ``UndeclaredKeys`` when one of those keys was set
    on no node before this revision. Reads only; the caller rolls back on a refusal."""
    limit = MAX_WARNINGS if limit is None else limit
    session.flush()
    written_set: set[str] = set()
    for model in (NodeVariable, ProfileVariable):
        written_set.update(
            session.scalars(
                select(model.key)
                .distinct()
                .where(model.created_rev == rev_id, model.deleted_rev.is_(None), model.key.not_in(select(VariableDefinition.name)))
            )
        )
    written = sorted(written_set)
    if not written:
        return EMPTY
    cands = Candidates.load(session)
    suggestions = {k: cands.suggest(k) for k in written}

    def paths(key: str, n: int | None) -> tuple[list[str], int]:
        """Where ``rev_id`` wrote ``key``: node paths, then ``profile:<name>``."""
        where = (NodeVariable.created_rev == rev_id, NodeVariable.deleted_rev.is_(None), NodeVariable.key == key)
        stmt = select(Node.path).select_from(NodeVariable).join(Node, Node.id == NodeVariable.node_id).where(*where).order_by(Node.path)
        found = list(session.scalars(stmt.limit(n) if n is not None else stmt))
        count = session.scalar(select(func.count()).select_from(NodeVariable).where(*where)) or 0
        pwhere = (ProfileVariable.created_rev == rev_id, ProfileVariable.deleted_rev.is_(None), ProfileVariable.key == key)
        pstmt = select(ProfileVariable.profile).where(*pwhere).order_by(ProfileVariable.profile)
        if n is None or len(found) < n:
            found += [f"profile:{p}" for p in session.scalars(pstmt.limit(n - len(found)) if n is not None else pstmt)]
        count += session.scalar(select(func.count()).select_from(ProfileVariable).where(*pwhere)) or 0
        return found, count

    if strict:
        before: set[str] = set()
        for model in (NodeVariable, ProfileVariable):
            before.update(
                session.scalars(
                    select(model.key)
                    .distinct()
                    .where(model.key.in_(written), model.created_rev < rev_id, or_(model.deleted_rev.is_(None), model.deleted_rev >= rev_id))
                )
            )
        new = [k for k in written if k not in before]
        if new:
            keys = []
            for k in new:
                where, count = paths(k, 3)
                entry: dict[str, Any] = {"key": k, "paths": where, "nodes": count}
                if suggestions[k] is not None:
                    entry["did_you_mean"] = suggestions[k].name  # type: ignore[union-attr]
                keys.append(entry)
            hints = ", ".join(k["key"] + (f" (did you mean {k['did_you_mean']}?)" if "did_you_mean" in k else "") for k in keys)
            raise UndeclaredKeys(
                {
                    "error": "undeclared key",
                    "message": (
                        f"strict mode ({STRICT_ENV}) refuses keys that are not declared and not set anywhere yet: {hints}. "
                        + _how_to_declare(new)
                        + ". Nothing was written"
                    ),
                    "keys": keys,
                }
            )

    flagged = [k for k in written if suggestions[k] is not None]
    per_key: dict[str, list[str]] = {}
    total = 0
    for k in flagged:
        per_key[k], count = paths(k, limit)
        total += count
    warnings: list[dict[str, Any]] = []
    for i in range(limit):  # round robin: every key shows once before any key twice
        for k in flagged:
            if i < len(per_key[k]) and len(warnings) < limit:
                s = suggestions[k]
                assert s is not None
                warnings.append(
                    {"key": k, "path": per_key[k][i], "did_you_mean": s.name, "declared": s.declared, "message": _message(k, per_key[k][i], s)}
                )
    return KeyReview(warnings, total)


def lint(session: Session, *, paths: int = LINT_PATHS) -> list[dict[str, Any]]:
    """Every live undeclared key that looks like a typo of a known one (the same rule as the
    write warnings), with how many nodes set it and the first ``paths`` of them. Read only;
    ``GET /api/v1/vars/lint`` and ``ais lint``."""
    cands = Candidates.load(session)
    out: list[dict[str, Any]] = []
    for key in sorted(cands.uses):
        s = cands.suggest(key)
        if s is None:
            continue
        where = list(
            session.scalars(
                select(Node.path)
                .select_from(NodeVariable)
                .join(Node, Node.id == NodeVariable.node_id)
                .where(NodeVariable.key == key, NodeVariable.deleted_rev.is_(None))
                .order_by(Node.path)
                .limit(paths)
            )
        )
        if len(where) < paths:
            where += [
                f"profile:{p}"
                for p in session.scalars(
                    select(ProfileVariable.profile)
                    .where(ProfileVariable.key == key, ProfileVariable.deleted_rev.is_(None))
                    .order_by(ProfileVariable.profile)
                    .limit(paths - len(where))
                )
            ]
        out.append({"key": key, "did_you_mean": s.name, "declared": s.declared, "nodes": cands.uses[key], "paths": where})
    return out

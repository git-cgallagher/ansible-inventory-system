"""Bounded node lookup for pickers (``GET /api/v1/nodes?q=…``, issue #27).

The web UI's host and path pickers used to download every node and filter in the browser
(about 21 MB at 100k hosts). This answers one keystroke with at most ``limit`` nodes,
ranked in tiers, each tier its own ``LIMIT``ed query that runs only while the answer is
short:

1. the name equals ``q``;
2. the name starts with ``q``;
3. the path starts with ``q`` (``all.b0.prod`` → that node and what is below it);
4. the name contains ``q``;
5. the path contains ``q`` (hosts under a matching group).

Matching ignores case; within a tier rows come in path order. A typical keystroke is one
query. ``%`` and ``_`` in ``q`` match themselves (``_`` is legal in names).

Indexes: on PostgreSQL migration 0008 adds trigram GIN indexes on ``name`` and ``path``
(``pg_trgm``), which serve ``ILIKE`` prefix and substring patterns; with no ``revision`` the
live filter is ``deleted_rev IS NULL``, the predicate of ``uq_node_path_live``, so the planner
can also walk that index in path order and stop at the limit. Measured in docs/scale.md.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ais.models import Node, NodeKind
from ais.resolver import _live_at, _under

DEFAULT_LIMIT = 20
MAX_LIMIT = 100


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search(
    session: Session,
    q: str = "",
    *,
    kinds: Iterable[NodeKind] | None = None,
    subtree: str | None = None,
    revision: int | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[Node]:
    """At most ``limit`` live nodes matching ``q`` (see the module docstring), as of
    ``revision`` (default: current). ``subtree`` keeps that node and what is below it; one
    that is not live at the revision matches nothing. An empty ``q`` lists the first nodes
    in path order."""
    if revision is None:
        live = Node.deleted_rev.is_(None)  # the partial indexes' predicate
    else:
        live = _live_at(Node, revision)
    base = [live]
    kinds = list(kinds or [])
    if kinds:
        base.append(Node.kind.in_(kinds))
    if subtree:
        if session.scalar(select(Node.id).where(Node.path == subtree, live)) is None:
            return []
        base.append(or_(Node.path == subtree, _under(session, subtree)))

    q = q.strip()
    if not q:
        return list(session.scalars(select(Node).where(*base).order_by(Node.path).limit(limit)))

    pattern = _escape(q)
    tiers = [
        Node.name.ilike(pattern, escape="\\"),  # no wildcard: equality, ignoring case
        Node.name.ilike(f"{pattern}%", escape="\\"),
        Node.path.ilike(f"{pattern}%", escape="\\"),
        Node.name.ilike(f"%{pattern}%", escape="\\"),
        Node.path.ilike(f"%{pattern}%", escape="\\"),
    ]
    found: list[Node] = []
    seen: set[int] = set()
    for cond in tiers:
        want = limit - len(found)
        if want <= 0:
            break
        stmt = select(Node).where(*base, cond)
        if seen:
            stmt = stmt.where(Node.id.notin_(seen))
        for n in session.scalars(stmt.order_by(Node.path).limit(want)):
            seen.add(n.id)
            found.append(n)
    return found

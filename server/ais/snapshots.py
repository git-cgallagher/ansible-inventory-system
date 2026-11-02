"""Materialized per-host resolved snapshots and diffs.

``materialize`` writes one ``host_snapshot`` row per (host, revision) the first
time that host is rendered or exported at that revision; later calls are no-ops
(insert … on conflict do nothing, so concurrent exports cannot collide).
``diff`` compares two revisions of a host, computing a missing snapshot on demand
so a diff always works even for revisions nobody exported.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ais.models import HostSnapshot, NodeKind, Revision
from ais.resolver import Snapshot, load_chain, resolve

# PostgreSQL allows 65,535 bind parameters per statement; a 100k-host export exceeded it
# in one IN list (docs/scale.md). Chunk lookups and inserts well below that.
_CHUNK = 1000

# Present in every resolved set and different at every revision by construction.
_IGNORED_KEYS = {"inventory_revision"}


def resolved_hash(resolved: dict[str, Any]) -> str:
    canonical = json.dumps({k: v for k, v in resolved.items() if k not in _IGNORED_KEYS}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _insert(session: Session):
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert


def materialize(session: Session, snap: Snapshot, hosts: list[str] | None = None) -> int:
    """Write missing snapshots for ``hosts`` (default: every host in ``snap``).
    Returns how many rows were computed."""
    host_nodes = {n.name: n for n in snap.nodes.values() if n.kind is NodeKind.host}
    wanted = [host_nodes[h] for h in (hosts if hosts is not None else host_nodes) if h in host_nodes]
    if not wanted:
        return 0
    existing: set[int] = set()
    stmt = select(HostSnapshot.host_node_id).where(HostSnapshot.revision == snap.revision)
    if hosts is None:  # every host: one scan of the revision, no id list at all
        existing.update(session.scalars(stmt))
    else:
        for i in range(0, len(wanted), _CHUNK):
            existing.update(session.scalars(stmt.where(HostSnapshot.host_node_id.in_([n.id for n in wanted[i : i + _CHUNK]]))))
    rows = []
    for node in wanted:
        if node.id in existing:
            continue
        resolved = resolve(snap, host=node.name)["resolved"]
        rows.append(
            {
                "host_node_id": node.id,
                "host_name": node.name,
                "revision": snap.revision,
                "resolved": resolved,
                "resolved_hash": resolved_hash(resolved),
            }
        )
    insert = _insert(session)
    for i in range(0, len(rows), _CHUNK):
        session.execute(insert(HostSnapshot).values(rows[i : i + _CHUNK]).on_conflict_do_nothing(index_elements=["host_node_id", "revision"]))
    return len(rows)


def list_snapshots(session: Session, host: str) -> list[dict[str, Any]]:
    rows = session.scalars(select(HostSnapshot).where(HostSnapshot.host_name == host).order_by(HostSnapshot.revision)).all()
    return [{"revision": r.revision, "resolved_hash": r.resolved_hash, "created_at": r.created_at} for r in rows]


def get_snapshot(session: Session, host: str, revision: int, *, create: bool = True) -> HostSnapshot | None:
    row = session.scalar(select(HostSnapshot).where(HostSnapshot.host_name == host, HostSnapshot.revision == revision))
    if row is not None or not create:
        return row
    if session.get(Revision, revision) is None:
        return None
    try:
        snap = load_chain(session, revision, host=host)
    except KeyError:
        return None
    materialize(session, snap, [host])
    return session.scalar(select(HostSnapshot).where(HostSnapshot.host_name == host, HostSnapshot.revision == revision))


def diff_resolved(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    a = {k: v for k, v in a.items() if k not in _IGNORED_KEYS}
    b = {k: v for k, v in b.items() if k not in _IGNORED_KEYS}
    added = {k: b[k] for k in b if k not in a}
    removed = {k: a[k] for k in a if k not in b}
    changed = {k: {"from": a[k], "to": b[k]} for k in a if k in b and a[k] != b[k]}
    return {"added": added, "removed": removed, "changed": changed, "identical": not (added or removed or changed)}


def diff(session: Session, host: str, from_rev: int, to_rev: int) -> dict[str, Any] | None:
    a = get_snapshot(session, host, from_rev)
    b = get_snapshot(session, host, to_rev)
    if a is None or b is None:
        return None
    out = {"host": host, "from": from_rev, "to": to_rev, "from_hash": a.resolved_hash, "to_hash": b.resolved_hash}
    if a.resolved_hash == b.resolved_hash:
        out.update({"added": {}, "removed": {}, "changed": {}, "identical": True})
    else:
        out.update(diff_resolved(a.resolved, b.resolved))
    return out

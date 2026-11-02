"""Per-host snapshot history: stored only where a host's resolved vars changed, written
by a background pass, never on the export or render path.

**The pass** (``run_pass``) takes the newest revision, resolves every live host there,
compares each with that host's latest stored row and writes a row only where the hash
differs. It then records the revision in ``snapshot_pass``. Bursts of writes coalesce:
the pass always jumps to the newest revision, so revisions in between are never passed.

**Lookup** (``get_snapshot``) for a host at revision R, in this order:

1. a row stored exactly at R: it is the state at R;
2. R has a ``snapshot_pass`` row: the host's latest row at or before R is its state at R,
   because that pass compared the state at R with exactly that row;
3. otherwise the state is computed on demand from the temporal rows and **not stored**.
   Storing it would break rule 2 for passes that ran later at higher revisions.

Rows only ever land at a passed revision, and every pass is at a higher revision than
the last, so rule 2 holds for every passed revision forever. Rows from before this design
(one per host per exported revision) are exact states, so rule 1 and the pass treat them
like any other row.

**The worker** (``SnapshotWorker``) is one daemon thread per API process. A committed
write wakes it; it also polls, so ordering between the wake-up and the commit never
matters. On PostgreSQL a transaction-level advisory lock keeps two processes from running
the same pass; on SQLite a process lock does. ``AIS_SNAPSHOTS=off`` disables it (tests,
or a dedicated ``ais snapshots run`` job).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import event, func, select, text
from sqlalchemy.orm import Session

from ais.models import HostSnapshot, NodeKind, Revision, SnapshotPass
from ais.resolver import current_revision, host_path, load_chain, load_snapshot, resolve, resolved_vars

log = logging.getLogger("ais.snapshots")

# PostgreSQL allows 65,535 bind parameters per statement; chunk inserts well below that.
_CHUNK = 1000

# Present in every resolved set and different at every revision by construction.
_IGNORED_KEYS = {"inventory_revision"}

# Arbitrary constant for pg_try_advisory_xact_lock: "the snapshot pass".
_ADVISORY_KEY = 0x41495353  # "AISS"


def resolved_hash(resolved: dict[str, Any]) -> str:
    canonical = json.dumps({k: v for k, v in resolved.items() if k not in _IGNORED_KEYS}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _insert(session: Session):
    if session.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert


# ------------------------------------------------------------------------- pass


@dataclass
class PassResult:
    revision: int
    hosts_checked: int
    rows_written: int
    seconds: float


def last_passed(session: Session) -> int:
    return session.scalar(select(func.max(SnapshotPass.revision))) or 0


def _latest_hashes(session: Session, upto: int) -> dict[str, str]:
    """Each host's most recent stored hash at or before ``upto``, keyed by host name."""
    newest = (
        select(HostSnapshot.host_name, func.max(HostSnapshot.revision).label("rev"))
        .where(HostSnapshot.revision <= upto)
        .group_by(HostSnapshot.host_name)
        .subquery()
    )
    stmt = select(HostSnapshot.host_name, HostSnapshot.resolved_hash).join(
        newest, (HostSnapshot.host_name == newest.c.host_name) & (HostSnapshot.revision == newest.c.rev)
    )
    return {name: digest for name, digest in session.execute(stmt)}


_process_lock = threading.Lock()


def _try_lock(session: Session) -> bool:
    if session.get_bind().dialect.name == "postgresql":
        return bool(session.scalar(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": _ADVISORY_KEY}))
    return True  # SQLite: the caller holds _process_lock, and SQLite serializes writers


def run_pass(session: Session) -> PassResult | None:
    """One pass at the newest revision, if it has not been passed yet. Writes nothing and
    returns None when there is nothing to do or another process holds the pass lock.
    The caller commits (rows and the ``snapshot_pass`` marker land together)."""
    latest = current_revision(session)
    if latest == 0 or latest <= last_passed(session):
        return None
    if not _try_lock(session):
        return None
    if latest <= last_passed(session):  # another process finished it while we waited
        return None
    started = time.monotonic()
    snap = load_snapshot(session, latest)
    previous = _latest_hashes(session, latest)
    rows: list[dict[str, Any]] = []
    checked = 0
    for node in snap.nodes.values():
        if node.kind is not NodeKind.host:
            continue
        checked += 1
        resolved = resolved_vars(snap, node)
        digest = resolved_hash(resolved)
        if previous.get(node.name) == digest:
            continue
        rows.append(
            {"host_node_id": node.id, "host_name": node.name, "revision": latest, "resolved": resolved, "resolved_hash": digest}
        )
    insert = _insert(session)
    for i in range(0, len(rows), _CHUNK):
        session.execute(insert(HostSnapshot).values(rows[i : i + _CHUNK]).on_conflict_do_nothing(index_elements=["host_node_id", "revision"]))
    seconds = round(time.monotonic() - started, 3)
    session.add(SnapshotPass(revision=latest, hosts_checked=checked, rows_written=len(rows), seconds=seconds))
    session.flush()
    return PassResult(latest, checked, len(rows), seconds)


def catch_up(max_passes: int = 100) -> list[PassResult]:
    """Run passes in their own transactions until the newest revision is passed (a write
    may land during a pass). Used by the worker and ``ais snapshots run``."""
    from ais.db import session_scope

    done: list[PassResult] = []
    with _process_lock:
        for _ in range(max_passes):
            with session_scope() as session:
                result = run_pass(session)
            if result is None:
                break
            done.append(result)
            log.info("snapshot pass at revision %s: %s hosts, %s changed, %.2fs", result.revision, result.hosts_checked, result.rows_written, result.seconds)
    return done


def status(session: Session) -> dict[str, Any]:
    latest = current_revision(session)
    passed = last_passed(session)
    last = session.get(SnapshotPass, passed) if passed else None
    return {
        "revision": latest,
        "passed": passed,
        "behind": latest > passed,
        "last_pass": None if last is None else {
            "revision": last.revision,
            "finished_at": last.finished_at,
            "hosts_checked": last.hosts_checked,
            "rows_written": last.rows_written,
            "seconds": last.seconds,
        },
        "rows": session.scalar(select(func.count()).select_from(HostSnapshot)) or 0,
    }


# ----------------------------------------------------------------------- lookup


@dataclass
class SnapshotView:
    """A host's resolved vars at ``revision``. ``stored_revision`` is the revision of the
    stored row it came from (≤ ``revision``), or None when computed on demand."""

    host: str
    revision: int
    stored_revision: int | None
    resolved: dict[str, Any]
    resolved_hash: str
    created_at: dt.datetime | None


def list_snapshots(session: Session, host: str) -> list[dict[str, Any]]:
    """The revisions where ``host``'s resolved vars were stored, i.e. changed."""
    rows = session.scalars(select(HostSnapshot).where(HostSnapshot.host_name == host).order_by(HostSnapshot.revision)).all()
    return [{"revision": r.revision, "resolved_hash": r.resolved_hash, "created_at": r.created_at} for r in rows]


def get_snapshot(session: Session, host: str, revision: int) -> SnapshotView | None:
    """``host``'s resolved vars at ``revision`` (see the module docstring for the rule),
    or None when the revision or the host at that revision does not exist."""
    if session.get(Revision, revision) is None or host_path(session, host, revision) is None:
        return None
    row = session.scalar(select(HostSnapshot).where(HostSnapshot.host_name == host, HostSnapshot.revision == revision))
    if row is None and session.get(SnapshotPass, revision) is not None:
        row = session.scalar(
            select(HostSnapshot)
            .where(HostSnapshot.host_name == host, HostSnapshot.revision <= revision)
            .order_by(HostSnapshot.revision.desc())
            .limit(1)
        )
    if row is not None:
        resolved = {**row.resolved, "inventory_revision": revision}
        return SnapshotView(host, revision, row.revision, resolved, row.resolved_hash, row.created_at)
    snap = load_chain(session, revision, host=host)
    resolved = resolve(snap, host=host)["resolved"]
    return SnapshotView(host, revision, None, resolved, resolved_hash(resolved), None)


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


# ----------------------------------------------------------------------- worker


def enabled() -> bool:
    return os.environ.get("AIS_SNAPSHOTS", "background").lower() not in ("off", "0", "false", "no")


def _poll_seconds() -> float:
    try:
        return max(float(os.environ.get("AIS_SNAPSHOT_POLL_SECONDS", "10")), 0.5)
    except ValueError:
        return 10.0


class SnapshotWorker:
    """One daemon thread: a pass after every committed write (plus a poll), coalesced."""

    # After a wake-up, wait this long so a burst of writes becomes one pass.
    debounce = 0.5

    def __init__(self) -> None:
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="ais-snapshots", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)
        self._thread = None

    def notify(self) -> None:
        self._wake.set()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                catch_up()
            except Exception:  # noqa: BLE001 - never let the thread die; the next wake retries
                log.exception("snapshot pass failed")
            self._wake.wait(_poll_seconds())
            if self._stop.is_set():
                break
            self._wake.clear()
            self._stop.wait(self.debounce)


worker = SnapshotWorker()


@event.listens_for(Session, "after_flush")
def _note_revision(session: Session, _ctx) -> None:
    if any(isinstance(obj, Revision) for obj in session.new):
        session.info["ais_new_revision"] = True


@event.listens_for(Session, "after_commit")
def _wake_after_commit(session: Session) -> None:
    if session.info.pop("ais_new_revision", False) and worker.running:
        worker.notify()


@event.listens_for(Session, "after_rollback")
def _forget_on_rollback(session: Session) -> None:
    session.info.pop("ais_new_revision", None)

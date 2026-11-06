"""Restore a deleted subtree as it was at a past revision (``POST /api/v1/nodes/restore``,
``ais restore``).

Built on the tree export round trip (``ais.tree``): ``export_tree(session, R, path)`` then
``import_tree_report`` in-process, so a restore reproduces exactly what that revision
exported (own variables, kinds, ``inventory_name``, tag memberships) and nothing above the
subtree is rolled back (the export's ancestors are structure only). On top of the import it:

* refuses up front, with a message that says why: the root, a live node at the path (a
  restore never overwrites; ``export-tree --as`` clones), a parent that is not live now
  (restore the nearest deleted ancestor instead, or the spine would come back var-less), a
  host name that is live elsewhere now (host names are unique);
* puts back the subtree's profile attachments (issue #22) but never the profiles themselves:
  a profile is shared, so its values are today's, not the revision's (like the ancestors,
  which are structure only), and a profile that no longer exists refuses the restore (409:
  recreate it first). Recreating it here would leave the ``node.restore`` row's inverse (delete
  the subtree) unable to undo it;
* copies node ``meta``, which the import format leaves out;
* returns ``restored`` = ``[{old_id, id, path}]`` for every node, which the caller writes
  into the ``node.restore`` audit row. ``audit.node_lineage`` reads that map like a move's,
  so ``GET /audit?node=<path>`` still shows the history from before the delete.

Why new rows and not revived ones: a node row is live over one interval ``[created_rev,
deleted_rev)``. Clearing ``deleted_rev`` would make every revision between the delete and the
restore show the subtree as live, which breaks "a pinned revision sees exactly the tree of
that moment" (and ``host_path`` for those revisions). So ids change, as on a move, and the
lineage links them.

The caller holds the write lock (``check_precondition`` / ``lock_for_write``), runs
``check_export`` and records the audit row; a dry run rolls the session back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ais.importer import ImportError_, import_tree_report, live_node_by_path
from ais.models import PATH_SEP, Node, NodeKind, Revision
from ais.profiles import live_profile
from ais.resolver import _live_at, _under, load_snapshot
from ais.tree import ROOT, export_tree


class RestoreError(ValueError):
    """A restore that cannot run. ``status`` is the HTTP status the API answers with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass
class RestoreResult:
    revision: Revision
    path: str
    from_revision: int
    restored: list[dict[str, Any]] = field(default_factory=list)  # [{old_id, id, path}], shallowest first
    hosts: int = 0
    variables_set: int = 0
    tags: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "from_revision": self.from_revision,
            "nodes": len(self.restored),
            "hosts": self.hosts,
            "variables_set": self.variables_set,
            "tags": self.tags,
        }


def last_live_revision(session: Session, path: str) -> int:
    """The newest revision at which a node lived at ``path`` before its latest delete: the
    newest revision id below that delete's. Never ``deleted_rev - 1``: revision ids are
    increasing but not contiguous on PostgreSQL (a rolled-back write uses up a value)."""
    deleted = session.scalar(
        select(Node.deleted_rev).where(Node.path == path, Node.deleted_rev.is_not(None)).order_by(Node.deleted_rev.desc()).limit(1)
    )
    if deleted is None:
        raise RestoreError(404, f"nothing was ever deleted at {path}: there is nothing to restore")
    before = session.scalar(select(Revision.id).where(Revision.id < deleted).order_by(Revision.id.desc()).limit(1))
    if before is None:  # pragma: no cover - a node always has a creating revision before its delete
        raise RestoreError(404, f"no revision before the delete of {path} (revision {deleted})")
    return before


def _nearest_missing_ancestor(session: Session, path: str) -> str:
    """The shallowest ancestor of ``path`` that is not live now."""
    parts = path.split(PATH_SEP)
    for i in range(2, len(parts)):
        candidate = PATH_SEP.join(parts[:i])
        if live_node_by_path(session, candidate) is None:
            return candidate
    return PATH_SEP.join(parts[:-1])


def restore_subtree(
    session: Session,
    path: str,
    revision: int | None = None,
    *,
    actor: str,
    source: str,
    message: str | None = None,
    rev: Revision | None = None,
) -> RestoreResult:
    """Bring the subtree at ``path`` back as it was at ``revision`` (default: the last
    revision before its latest delete) under one new revision, or under ``rev`` (a revert
    undoing a delete shares its one revision). Raises ``RestoreError`` (with the HTTP
    status) or ``ImportError_`` (422: a value its definition now rejects)."""
    path = path.strip()
    if not path or path == ROOT:
        raise RestoreError(422, "the root cannot be restored: give the path of a deleted subtree, such as all.commercial.prod.dc1")
    if live_node_by_path(session, path) is not None:
        raise RestoreError(
            409,
            f"{path} exists now: a restore only brings back a deleted subtree and never overwrites a live one. "
            f"Delete or move the live node first, or copy the old version elsewhere with GET /api/v1/tree?subtree={path}&revision=R&as=<new path>",
        )
    if revision is None:
        revision = last_live_revision(session, path)
    elif session.get(Revision, revision) is None:
        raise RestoreError(404, f"revision {revision} does not exist")
    old_root = session.scalar(select(Node).where(Node.path == path, _live_at(Node, revision)))
    if old_root is None:
        raise RestoreError(404, f"{path} did not exist at revision {revision}")
    parent_path = path.rpartition(PATH_SEP)[0]
    if live_node_by_path(session, parent_path) is None:
        missing = _nearest_missing_ancestor(session, path)
        raise RestoreError(
            409,
            f"the parent {parent_path} does not exist now: restore {missing} (the highest deleted ancestor) instead, "
            "which brings this subtree back with it",
        )

    # The old rows, by path (one node per path at a revision).
    snap = load_snapshot(session, revision, path)
    old_nodes = sorted((n for n in snap.nodes.values() if n.path == path or n.path.startswith(path + PATH_SEP)), key=lambda n: (n.depth, n.path))
    old_hosts = [n.name for n in old_nodes if n.kind is NodeKind.host]
    if old_hosts:
        taken = sorted(
            f"{name} (at {where})"
            for name, where in session.execute(
                select(Node.name, Node.path).where(Node.kind == NodeKind.host, Node.deleted_rev.is_(None), Node.name.in_(sorted(set(old_hosts))))
            )
        )
        if taken:
            raise RestoreError(
                409,
                f"cannot restore {path}: host names are unique and {len(taken)} of its hosts exist elsewhere now: "
                + ", ".join(taken[:10])
                + (" …" if len(taken) > 10 else "")
                + ". Delete or rename those hosts first",
            )

    tree = export_tree(session, revision, path)
    gone = sorted(name for name in tree.get("profiles") or {} if live_profile(session, name) is None)
    if gone:
        raise RestoreError(
            409,
            f"cannot restore {path}: it had profile(s) {', '.join(gone)} attached, which do not exist now. "
            f"Recreate them first (PUT /api/v1/profiles/<name>; GET /api/v1/profiles/<name>?revision={revision} shows them as they were)",
        )
    shared = rev is not None
    result = import_tree_report(
        session, tree, actor=actor, message=message or f"restore {path} from revision {revision}", rev=rev, profile_values=False
    )
    rev = result.revision
    if not shared:
        rev.source = source  # the importer says "import"; this revision is a restore from this client

    new_by_path = {
        n.path: n
        for n in session.scalars(select(Node).where(Node.deleted_rev.is_(None), (Node.path == path) | _under(session, path)))
    }
    restored: list[dict[str, Any]] = []
    for old in old_nodes:
        new = new_by_path.get(old.path)
        if new is None or new.created_rev != rev.id:  # pragma: no cover - the checks above make this impossible
            raise ImportError_(f"restore of {path} did not recreate {old.path}")
        if old.meta:
            new.meta = dict(old.meta)  # the import format has no place for meta
        restored.append({"old_id": old.id, "id": new.id, "path": new.path})
    session.flush()
    return RestoreResult(
        revision=rev,
        path=path,
        from_revision=revision,
        restored=restored,
        hosts=len(old_hosts),
        variables_set=result.variables_set,
        tags=result.tags_changed,
    )

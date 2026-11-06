"""Profiles (issue #22): named bundles of variables attached to nodes.

A profile (``gpu-a100: {gpu_driver: "560.35", node_labels: {gpu: a100}}``) is attached to any
number of non-host nodes. At each node it applies as a level of its own between the node's
parent and the node: below the node's own variables, above everything inherited, with the
same merge strategies (``resolver._emitted`` folds ``Snapshot.layers``). Several profiles on
one node apply in the node's list order, first = lowest precedence. Profiles do not include
other profiles (v1). The plugin never sees a profile: the exporter emits the values on the
node exactly as if they were set there.

Everything is temporal (``profile``, ``profile_variable``, ``profile_attachment``): a write
closes rows (``deleted_rev``) and opens new ones under its revision, so a pinned revision sees
the profile and its attachments exactly as they were. The functions here are the mutations
the routes (``ais.api_profiles``), the importer and ``ais.revert`` share; they take the
revision and raise ``ProfileError`` (with the HTTP status) to refuse.

The unit of change for attachments is **a node's ordered profile list**
(``set_node_profiles``): attach, detach and reorder all converge it, and their audit rows
carry the list before and after, which is exactly what a revert puts back.
"""

from __future__ import annotations

import copy
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ais import schema
from ais.models import NAME_RE, Node, NodeKind, Profile, ProfileAttachment, ProfileVariable, Revision
from ais.resolver import _live_at, profile_source
from ais.varsearch import canonical

PREFIX = "profile:"  # how keycheck/schema messages name a profile instead of a node path


class ProfileError(ValueError):
    """A profile write that cannot run. ``status`` is the HTTP status; ``detail`` the body's
    detail (a string, or a dict with ``error`` and ``message``)."""

    def __init__(self, status: int, detail: Any):
        super().__init__(detail.get("message", "") if isinstance(detail, dict) else str(detail))
        self.status = status
        self.detail = detail


def name_error(name: Any) -> str | None:
    """Why ``name`` cannot be a profile name, or None (the node name rule)."""
    if not isinstance(name, str) or not NAME_RE.match(name):
        return f"invalid profile name {name!r}: letters, digits, '_' and '-' only (max 255)"
    return None


def label(name: str) -> str:
    """Where a profile's value lives, for messages: ``profile:gpu-a100``."""
    return f"{PREFIX}{name}"


# ----------------------------------------------------------------------- reads


def live_profile(session: Session, name: str) -> Profile | None:
    return session.scalar(select(Profile).where(Profile.name == name, Profile.deleted_rev.is_(None)))


def _vars(session: Session, name: str, rev: int | None) -> dict[str, Any]:
    live = ProfileVariable.deleted_rev.is_(None) if rev is None else _live_at(ProfileVariable, rev)
    rows = session.execute(select(ProfileVariable.key, ProfileVariable.value).where(ProfileVariable.profile == name, live).order_by(ProfileVariable.key))
    return {k: v for k, v in rows}


def doc(session: Session, name: str, rev: int | None = None) -> dict[str, Any] | None:
    """The profile as the API and the audit rows show it, live now or as of ``rev``:
    ``{name, description, vars}``; None when it does not exist (then)."""
    live = Profile.deleted_rev.is_(None) if rev is None else _live_at(Profile, rev)
    row = session.scalar(select(Profile).where(Profile.name == name, live))
    if row is None:
        return None
    return {"name": name, "description": row.description, "vars": _vars(session, name, rev)}


def attachments(session: Session, name: str, rev: int | None = None) -> list[dict[str, Any]]:
    """Where ``name`` is attached (live now or at ``rev``): ``[{path, kind, position}]`` by path."""
    att_live = ProfileAttachment.deleted_rev.is_(None) if rev is None else _live_at(ProfileAttachment, rev)
    node_live = Node.deleted_rev.is_(None) if rev is None else _live_at(Node, rev)
    rows = session.execute(
        select(Node.path, Node.kind, ProfileAttachment.position)
        .select_from(ProfileAttachment)
        .join(Node, Node.id == ProfileAttachment.node_id)
        .where(ProfileAttachment.profile == name, att_live, node_live)
        .order_by(Node.path)
    )
    return [{"path": p, "kind": k.value, "position": pos} for p, k, pos in rows]


def node_list(session: Session, node_id: int, rev: int | None = None) -> list[str]:
    """The profiles attached to a node, in application order (live now or at ``rev``)."""
    live = ProfileAttachment.deleted_rev.is_(None) if rev is None else _live_at(ProfileAttachment, rev)
    rows = session.execute(
        select(ProfileAttachment.profile, ProfileAttachment.position).where(ProfileAttachment.node_id == node_id, live)
    )
    return [name for name, _pos in sorted(rows, key=lambda r: (r[1], r[0]))]


def changed_keys(before: dict[str, Any] | None, after: dict[str, Any] | None) -> list[str]:
    """The variables whose value differs between two profile docs (for ``audit_key``)."""
    b = (before or {}).get("vars") or {}
    a = (after or {}).get("vars") or {}
    return sorted(k for k in set(b) | set(a) if k not in b or k not in a or canonical(b[k]) != canonical(a[k]))


# -------------------------------------------------------------------- writes


def set_profile(session: Session, rev: Revision, name: str, wanted: dict[str, Any] | None) -> dict[str, Any] | None:
    """Converge profile ``name`` to ``wanted`` (``{description, vars}``; ``vars`` is the
    complete set) under ``rev``, or delete it when ``wanted`` is None (it must not be
    attached anywhere: 409). Every new value must satisfy its definition's schema (422).
    Returns the profile as it now is (``doc``). Shared by the routes, the importer and revert."""
    error = name_error(name)
    if error:
        raise ProfileError(422, error)
    row = live_profile(session, name)
    current = {k: nv for k, nv in ((nv.key, nv) for nv in session.scalars(select(ProfileVariable).where(ProfileVariable.profile == name, ProfileVariable.deleted_rev.is_(None))))}
    if wanted is None:
        if row is None:
            return None
        attached = attachments(session, name)
        if attached:
            raise ProfileError(409, attached_detail(name, attached))
        row.deleted_rev = rev.id
        for nv in current.values():
            nv.deleted_rev = rev.id
        session.flush()
        return None
    values = wanted.get("vars") or {}
    if not isinstance(values, dict):
        raise ProfileError(422, f"profile {name!r}: 'vars' must be a mapping")
    for key, value in values.items():
        if not isinstance(key, str) or not key or len(key) > 255:
            raise ProfileError(422, f"profile {name!r}: invalid key {key!r}")
        if key not in current or canonical(current[key].value) != canonical(value):
            try:
                schema.check_value(session, key, value, label(name))
            except schema.SchemaViolation as exc:
                raise ProfileError(422, str(exc)) from exc
    description = wanted.get("description")
    if row is None:
        session.add(Profile(name=name, description=description, created_rev=rev.id))
    elif row.description != description:
        row.deleted_rev = rev.id
        session.flush()  # close before reopening: one live row per name
        session.add(Profile(name=name, description=description, created_rev=rev.id))
    for key, nv in current.items():
        if key not in values or canonical(nv.value) != canonical(values[key]):
            nv.deleted_rev = rev.id
    for key, value in values.items():
        if key not in current or canonical(current[key].value) != canonical(value):
            session.add(ProfileVariable(profile=name, key=key, value=copy.deepcopy(value), created_rev=rev.id))
    session.flush()
    return doc(session, name)


def attached_detail(name: str, attached: list[dict[str, Any]]) -> dict[str, Any]:
    paths = [a["path"] for a in attached]
    shown = ", ".join(paths[:10]) + (" …" if len(paths) > 10 else "")
    return {
        "error": "profile attached",
        "message": (
            f"profile {name!r} is attached to {len(paths)} node(s): {shown}. Nothing was deleted. "
            "Detach it first, or send detach=true to detach it everywhere in the same revision"
        ),
        "nodes": paths,
    }


def set_node_profiles(session: Session, rev: Revision, node: Node, names: list[str]) -> tuple[list[str], list[str]]:
    """Converge the node's ordered profile list to ``names`` under ``rev``: rows whose
    position changed or that are gone are closed, new positions opened. Returns the list
    before and after. Refused: a host (422), a name twice (422), a profile that does not
    exist (404)."""
    if node.kind is NodeKind.host:
        raise ProfileError(422, f"{node.path} is a host: profiles attach to groups (any non-host node); set host values on the host")
    if node.deleted_rev is not None:
        raise ProfileError(404, f"node {node.path} does not exist")
    if len(set(names)) != len(names):
        raise ProfileError(422, f"a profile is listed twice for {node.path}: {names}")
    for name in names:
        error = name_error(name)
        if error:
            raise ProfileError(422, error)
        if live_profile(session, name) is None:
            raise ProfileError(404, f"profile {name!r} does not exist")
    rows = list(session.scalars(select(ProfileAttachment).where(ProfileAttachment.node_id == node.id, ProfileAttachment.deleted_rev.is_(None))))
    before = [r.profile for r in sorted(rows, key=lambda r: (r.position, r.profile))]
    if before == names:
        return before, before
    keep: set[tuple[str, int]] = set()
    for r in rows:
        if r.position < len(names) and names[r.position] == r.profile:
            keep.add((r.profile, r.position))
        else:
            r.deleted_rev = rev.id
    session.flush()  # close before reopening: a node holds a profile once while live
    for position, name in enumerate(names):
        if (name, position) not in keep:
            session.add(ProfileAttachment(node_id=node.id, profile=name, position=position, created_rev=rev.id))
    session.flush()
    return before, list(names)


def attach_action(before: list[str], after: list[str]) -> str:
    """The audit action of a node's list change: ``profile.attach`` (only additions, order
    kept), ``profile.detach`` (only removals) or ``node.profiles`` (a reorder or a mix)."""
    if [n for n in after if n in before] == before and len(after) > len(before):
        return "profile.attach"
    if [n for n in before if n in after] == after and len(after) < len(before):
        return "profile.detach"
    return "node.profiles"


def close_attachments(session: Session, rev: Revision, node_ids: list[int]) -> None:
    """Close the live attachments of these nodes (a subtree delete or move)."""
    for i in range(0, len(node_ids), 5000):
        for r in session.scalars(
            select(ProfileAttachment).where(ProfileAttachment.node_id.in_(node_ids[i : i + 5000]), ProfileAttachment.deleted_rev.is_(None))
        ):
            r.deleted_rev = rev.id


def live_attachment_rows(session: Session, node_ids: list[int]) -> list[ProfileAttachment]:
    out: list[ProfileAttachment] = []
    for i in range(0, len(node_ids), 5000):
        out.extend(
            session.scalars(select(ProfileAttachment).where(ProfileAttachment.node_id.in_(node_ids[i : i + 5000]), ProfileAttachment.deleted_rev.is_(None)))
        )
    return out


def delete_profile(session: Session, rev: Revision, name: str, *, detach: bool) -> dict[str, Any]:
    """Delete profile ``name``; with ``detach`` first remove it from every node's list (in the
    same revision), else refuse while attached (409). Returns the ``profile.delete`` row's
    ``before``: the profile plus ``attached`` = ``[{path, profiles}]``, each node's whole list
    before, which a revert converges back."""
    before = doc(session, name)
    if before is None:
        raise ProfileError(404, f"profile {name!r} does not exist")
    attached = attachments(session, name)
    if attached and not detach:
        raise ProfileError(409, attached_detail(name, attached))
    lists: list[dict[str, Any]] = []
    for a in attached:
        node = session.scalar(select(Node).where(Node.path == a["path"], Node.deleted_rev.is_(None)))
        assert node is not None
        was, _now = set_node_profiles(session, rev, node, [n for n in node_list(session, node.id) if n != name])
        lists.append({"path": a["path"], "profiles": was})
    set_profile(session, rev, name, None)
    return {**before, "attached": lists}


__all__ = [
    "ProfileError",
    "attach_action",
    "attached_detail",
    "attachments",
    "changed_keys",
    "close_attachments",
    "delete_profile",
    "doc",
    "label",
    "live_attachment_rows",
    "live_profile",
    "name_error",
    "node_list",
    "profile_source",
    "set_node_profiles",
    "set_profile",
]

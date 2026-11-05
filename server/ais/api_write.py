"""Write routes with optimistic locking (``If-Match``), editor role.

Every write: ``begin_write`` (lock, precondition, new ``Revision``) → change →
``check_export`` (re-run the exporter on the live snapshot and map its errors to
409, so group-name and tag-group collisions are rejected at write time by the same
code that would reject them at export) → ``audit.record`` (one row per effective
write, in the same transaction) → respond with the new revision and ETag.
Nothing is updated in place: nodes and variables are closed (``deleted_rev``) and
reopened under the new revision. A move/rename closes the whole subtree and
reopens it under new ids (``POST /nodes/{id}/move``).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ais import audit, schema
from ais.auth import Principal, require
from ais.db import get_session
from ais.importer import ImportError_, check_kind, check_name, check_position, infer_kind, live_node_by_path
from ais.locking import IfMatch, begin_write, check_precondition, etag
from ais.models import PATH_SEP, MergeStrategy, Node, NodeKind, NodeVariable, Revision, Role, TagMember, VariableDefinition
from ais.resolver import build_export, current_revision, load_snapshot
from ais.restore import RestoreError, restore_subtree

router = APIRouter(prefix="/api/v1", tags=["write"])
editor = Depends(require(Role.editor))


# --------------------------------------------------------------------- helpers


def node_out(n: Node, live_vars: dict[str, Any] | None = None) -> dict[str, Any]:
    out = {
        "id": n.id,
        "parent_id": n.parent_id,
        "kind": n.kind.value,
        "name": n.name,
        "inventory_name": n.inventory_name,
        "path": n.path,
        "depth": n.depth,
        "meta": n.meta or {},
        "created_rev": n.created_rev,
        "deleted_rev": n.deleted_rev,
    }
    if live_vars is not None:
        out["vars"] = live_vars
    return out


def live_vars(session: Session, node_id: int) -> dict[str, Any]:
    rows = session.scalars(select(NodeVariable).where(NodeVariable.node_id == node_id, NodeVariable.deleted_rev.is_(None)))
    return {r.key: r.value for r in rows}


def get_live_node(session: Session, node_id: int) -> Node:
    node = session.get(Node, node_id)
    if node is None or node.deleted_rev is not None:
        raise HTTPException(404, f"node {node_id} does not exist")
    return node


def live_subtree(session: Session, node: Node) -> list[Node]:
    """``node`` and every live descendant, shallowest first."""
    prefix = node.path + PATH_SEP
    rows = [n for n in session.scalars(select(Node).where(Node.deleted_rev.is_(None))) if n.path == node.path or n.path.startswith(prefix)]
    return sorted(rows, key=lambda n: (n.depth, n.path))


def check_export(session: Session) -> None:
    """Reject the pending write if the live tree no longer exports cleanly."""
    try:
        build_export(load_snapshot(session))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


def _finish(response: Response, rev: Revision, body: dict[str, Any]) -> dict[str, Any]:
    response.headers["ETag"] = etag(rev.id)
    return {"revision": rev.id, "changed": True, **body}


def _audit(session: Session, principal: Principal, rev: Revision, action: str, target_type: str, target_id: Any, before: Any, after: Any) -> None:
    audit.record(
        session,
        actor=principal.actor,
        action=action,
        target_type=target_type,
        target_id=target_id,
        before=before,
        after=after,
        source=rev.source,
        revision=rev.id,
    )


def _set_var(session: Session, node: Node, key: str, value: Any, rev: Revision) -> bool:
    current = session.scalar(
        select(NodeVariable).where(NodeVariable.node_id == node.id, NodeVariable.key == key, NodeVariable.deleted_rev.is_(None))
    )
    if current is not None and current.value == value:
        return False
    if current is not None:
        current.deleted_rev = rev.id
    session.add(NodeVariable(node_id=node.id, key=key, value=value, created_rev=rev.id))
    return True


def _check_value(session: Session, key: str, value: Any, path: str) -> None:
    try:
        schema.check_value(session, key, value, path)
    except schema.SchemaViolation as exc:
        raise HTTPException(422, str(exc)) from exc


def _resolve_parent(session: Session, parent_id: int | None, parent_path: str | None) -> Node:
    if parent_id is not None:
        return get_live_node(session, parent_id)
    parent = live_node_by_path(session, parent_path or "")
    if parent is None:
        raise HTTPException(404, f"parent {parent_path!r} does not exist")
    return parent


# ---------------------------------------------------------------------- nodes


class NodeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_id: int | None = None
    parent_path: str | None = Field(default=None, description="Alternative to parent_id, e.g. all.commercial.prod")
    name: str = Field(max_length=255)
    kind: NodeKind | None = Field(default=None, description="Inferred from depth when omitted")
    inventory_name: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    vars: dict[str, Any] = Field(default_factory=dict)
    message: str | None = None


class NodePatch(BaseModel):
    """Only non-temporal metadata; renames and moves go through ``move``."""

    model_config = ConfigDict(extra="forbid")

    inventory_name: str | None = None
    clear_inventory_name: bool = False
    meta: dict[str, Any] | None = None
    message: str | None = None


class NodeMove(BaseModel):
    """New parent (``parent_id`` or ``parent_path``), new ``name``, or both."""

    model_config = ConfigDict(extra="forbid")

    parent_id: int | None = None
    parent_path: str | None = None
    name: str | None = Field(default=None, max_length=255)
    message: str | None = None


@router.post("/nodes", status_code=201)
def create_node(
    req: NodeCreate,
    response: Response,
    if_match: str | None = IfMatch(),
    principal: Principal = editor,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    if (req.parent_id is None) == (req.parent_path is None):
        raise HTTPException(422, "exactly one of parent_id or parent_path is required")
    rev = begin_write(session, if_match, actor=principal.actor, message=req.message or f"create node {req.name}")
    parent = _resolve_parent(session, req.parent_id, req.parent_path)
    kind = req.kind or infer_kind(parent.depth + 1, None)
    path = f"{parent.path}{PATH_SEP}{req.name}"
    try:
        check_name(req.name)
        check_kind(parent, kind)
        check_position(path, parent.depth + 1)
    except ImportError_ as exc:
        raise HTTPException(422, str(exc)) from exc
    if kind is NodeKind.host and req.inventory_name:
        raise HTTPException(422, "hosts have no inventory_name")
    if live_node_by_path(session, path) is not None:
        raise HTTPException(409, f"node {path} already exists")
    for key, value in req.vars.items():
        _check_value(session, key, value, path)
    node = Node(
        parent_id=parent.id,
        kind=kind,
        name=req.name,
        path=path,
        depth=parent.depth + 1,
        inventory_name=req.inventory_name,
        meta=req.meta,
        created_rev=rev.id,
    )
    session.add(node)
    session.flush()
    for key, value in req.vars.items():
        _set_var(session, node, key, value, rev)
    check_export(session)
    out = node_out(node, req.vars)
    _audit(session, principal, rev, "node.create", "node", node.id, None, out)
    return _finish(response, rev, {"node": out})


@router.patch("/nodes/{node_id}")
def patch_node(
    node_id: int,
    req: NodePatch,
    response: Response,
    if_match: str | None = IfMatch(),
    principal: Principal = editor,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    rev = begin_write(session, if_match, actor=principal.actor, message=req.message or f"patch node {node_id}")
    node = get_live_node(session, node_id)
    if node.kind is NodeKind.host and req.inventory_name:
        raise HTTPException(422, "hosts have no inventory_name")
    before = node_out(node)
    if req.clear_inventory_name:
        node.inventory_name = None
    elif req.inventory_name is not None:
        node.inventory_name = req.inventory_name
    if req.meta is not None:
        node.meta = req.meta
    session.flush()
    check_export(session)
    out = node_out(node, live_vars(session, node.id))
    _audit(session, principal, rev, "node.update", "node", node.id, before, node_out(node))
    return _finish(response, rev, {"node": out})


@router.post("/nodes/{node_id}/move")
def move_node(
    node_id: int,
    req: NodeMove,
    response: Response,
    if_match: str | None = IfMatch(),
    principal: Principal = editor,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Move and/or rename a node with its whole subtree.

    The old rows are closed and new ones opened under the new revision (new ids;
    variables and tag memberships are carried over), so a pinned revision still
    shows the old tree. Refused: the root, a move under the node's own subtree,
    a child kind the new parent cannot have, and a live node at the new path.
    """
    if req.parent_id is not None and req.parent_path is not None:
        raise HTTPException(422, "give parent_id or parent_path, not both")
    if req.parent_id is None and req.parent_path is None and req.name is None:
        raise HTTPException(422, "nothing to do: give a new parent, a new name, or both")
    rev = begin_write(session, if_match, actor=principal.actor, message=req.message or f"move node {node_id}")
    node = get_live_node(session, node_id)
    if node.kind is NodeKind.all:
        raise HTTPException(422, "the root cannot be moved or renamed")
    if req.parent_id is None and req.parent_path is None:
        new_parent = get_live_node(session, node.parent_id)  # type: ignore[arg-type]
    else:
        new_parent = _resolve_parent(session, req.parent_id, req.parent_path)
    new_name = req.name or node.name
    if new_parent.id == node.parent_id and new_name == node.name:
        raise HTTPException(422, f"{node.path} is already there under that name")
    if new_parent.path == node.path or new_parent.path.startswith(node.path + PATH_SEP):
        raise HTTPException(422, f"cannot move {node.path} under its own subtree ({new_parent.path})")
    try:
        check_name(new_name)
        check_kind(new_parent, node.kind)
    except ImportError_ as exc:
        raise HTTPException(422, str(exc)) from exc
    new_path = f"{new_parent.path}{PATH_SEP}{new_name}"
    if live_node_by_path(session, new_path) is not None:
        raise HTTPException(409, f"node {new_path} already exists")

    old_rows = live_subtree(session, node)
    # Every relocated node must still fit: check the whole subtree before touching a row.
    try:
        for n in old_rows:
            check_position(new_path + n.path[len(node.path) :], new_parent.depth + 1 + (n.depth - node.depth))
    except ImportError_ as exc:
        raise HTTPException(422, str(exc)) from exc
    old_ids = [n.id for n in old_rows]
    old_vars = session.scalars(select(NodeVariable).where(NodeVariable.node_id.in_(old_ids), NodeVariable.deleted_rev.is_(None))).all()
    old_members = session.scalars(select(TagMember).where(TagMember.node_id.in_(old_ids))).all()
    before = {**node_out(node, live_vars(session, node.id)), "subtree": [n.path for n in old_rows]}

    for n in old_rows:
        n.deleted_rev = rev.id
    for nv in old_vars:
        nv.deleted_rev = rev.id
    session.flush()  # close before reopening, so the live unique indexes never see both

    id_map: dict[int, int] = {}
    new_rows: list[Node] = []
    for n in old_rows:
        is_root = n.id == node.id
        new = Node(
            parent_id=new_parent.id if is_root else id_map[n.parent_id],  # type: ignore[index]
            kind=n.kind,
            name=new_name if is_root else n.name,
            inventory_name=n.inventory_name,
            path=new_path + n.path[len(node.path) :],
            depth=new_parent.depth + 1 + (n.depth - node.depth),
            meta=dict(n.meta or {}),
            created_rev=rev.id,
        )
        session.add(new)
        session.flush()
        id_map[n.id] = new.id
        new_rows.append(new)
    for nv in old_vars:
        session.add(NodeVariable(node_id=id_map[nv.node_id], key=nv.key, value=nv.value, created_rev=rev.id))
    for tm in old_members:
        session.add(TagMember(tag_id=tm.tag_id, node_id=id_map[tm.node_id]))
    session.flush()
    check_export(session)

    moved = [{"old_id": o.id, "id": n.id, "from": o.path, "to": n.path} for o, n in zip(old_rows, new_rows)]
    root = new_rows[0]
    out = node_out(root, live_vars(session, root.id))
    _audit(session, principal, rev, "node.move", "node", node.id, before, {**out, "moved": moved})
    return _finish(response, rev, {"node": out, "moved": moved})


def _delete_guard(node: Node, subtree: list[Node], recursive: bool, expect_count: int | None) -> None:
    """Refuse to delete a node with live descendants unless the caller confirmed it:
    ``recursive=true``, or ``expect_count`` equal to the subtree size (the node included).
    A wrong ``expect_count`` refuses even with ``recursive``: the tree is not what the
    caller looked at. A leaf (a host, an empty group) needs neither."""
    nodes = len(subtree)
    hosts = sum(1 for n in subtree if n.kind is NodeKind.host)
    counts = {"path": node.path, "nodes": nodes, "hosts": hosts}
    if expect_count is not None and expect_count != nodes:
        raise HTTPException(
            409,
            {
                "error": "count mismatch",
                "message": (
                    f"deleting {node.path} would delete {nodes} node(s) ({hosts} host(s)), "
                    f"not the {expect_count} expect_count says. Nothing was deleted; look again, then send expect_count={nodes}"
                ),
                **counts,
            },
        )
    if nodes > 1 and not recursive and expect_count is None:
        raise HTTPException(
            409,
            {
                "error": "subtree not empty",
                "message": (
                    f"{node.path} has {nodes - 1} node(s) below it; deleting it deletes {nodes} node(s) in all, "
                    f"{hosts} of them host(s). Nothing was deleted. To confirm, send recursive=true or expect_count={nodes}"
                ),
                **counts,
            },
        )


@router.delete("/nodes/{node_id}")
def delete_node(
    node_id: int,
    response: Response,
    recursive: bool = Query(False, description="Confirm deleting a node that has descendants (the whole subtree goes)"),
    expect_count: int | None = Query(None, ge=1, description="Confirm by size: must equal the number of nodes deleted, the node included"),
    dry_run: bool = Query(False, description="Report what would be deleted (and whether it would be refused); writes nothing, no If-Match needed"),
    if_match: str | None = IfMatch(),
    principal: Principal = editor,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Delete a node and its subtree (closed under one revision; earlier revisions keep them,
    and ``POST /nodes/restore`` brings them back). A node with live descendants is refused
    (409, with the counts) unless ``recursive`` or a matching ``expect_count`` confirms it."""
    if dry_run:
        node = get_live_node(session, node_id)
    else:
        rev = begin_write(session, if_match, actor=principal.actor, message=f"delete node {node_id}")
        node = get_live_node(session, node_id)
    if node.kind is NodeKind.all:
        raise HTTPException(422, "the root cannot be deleted")
    subtree = live_subtree(session, node)
    _delete_guard(node, subtree, recursive, expect_count)
    counts = {"nodes": len(subtree), "hosts": sum(1 for n in subtree if n.kind is NodeKind.host)}
    paths = [n.path for n in subtree]
    if dry_run:
        rev_now = current_revision(session)
        response.headers["ETag"] = etag(rev_now)
        return {"revision": rev_now, "changed": True, "dry_run": True, "deleted": paths, **counts}
    ids = [n.id for n in subtree]
    before = {**node_out(node, live_vars(session, node.id)), "subtree": paths}
    for n in subtree:
        n.deleted_rev = rev.id
    for nv in session.scalars(select(NodeVariable).where(NodeVariable.node_id.in_(ids), NodeVariable.deleted_rev.is_(None))):
        nv.deleted_rev = rev.id
    session.flush()
    _audit(session, principal, rev, "node.delete", "node", node.id, before, None)
    return _finish(response, rev, {"dry_run": False, "deleted": paths, **counts})


class NodeRestore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(description="Path of the deleted subtree, e.g. all.commercial.prod.dc1")
    revision: int | None = Field(default=None, description="Restore it as it was at this revision (default: the last one before its latest delete)")
    dry_run: bool = Field(default=False, description="Report what the restore would bring back, write nothing (no If-Match needed)")
    message: str | None = None


@router.post("/nodes/restore")
def restore_node(
    req: NodeRestore,
    response: Response,
    if_match: str | None = IfMatch(),
    principal: Principal = editor,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Bring a deleted subtree back as it was at ``revision``: nodes, own variables, node
    ``meta`` and tag memberships, under one new revision (201). The restored nodes get new
    ids; the ``node.restore`` audit row maps every old id to its new one, so ``GET
    /audit?node=<path>`` keeps the history from before the delete. Refused: the root (422), a
    live node at ``path`` or a parent that is not live (409), a host name live elsewhere now
    (409), a value its definition now rejects (422). ``dry_run`` reports the same and rolls back."""
    if not req.dry_run:
        check_precondition(session, if_match)  # the importer opens the revision
    try:
        result = restore_subtree(session, req.path, req.revision, actor=principal.actor, source=audit.current_source(), message=req.message)
    except RestoreError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    except ImportError_ as exc:
        raise HTTPException(422, str(exc)) from exc
    check_export(session)
    summary = result.summary()
    if req.dry_run:
        restored = [{"path": r["path"], "old_id": r["old_id"]} for r in result.restored]
        session.rollback()
        rev_now = current_revision(session)
        response.headers["ETag"] = etag(rev_now)
        return {"revision": rev_now, "changed": True, "dry_run": True, **summary, "restored": restored}
    rev = result.revision
    root = session.get(Node, result.restored[0]["id"])
    out = node_out(root, live_vars(session, root.id))  # type: ignore[arg-type]
    _audit(
        session,
        principal,
        rev,
        "node.restore",
        "node",
        root.id,  # type: ignore[union-attr]
        {"path": result.path, "id": result.restored[0]["old_id"], "revision": result.from_revision},
        {**out, **summary, "restored": result.restored},
    )
    response.status_code = 201
    return _finish(response, rev, {"dry_run": False, "node": out, **summary, "restored": result.restored})


# ------------------------------------------------------------------ variables


class VarPut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Any
    message: str | None = None


def _var_target(node: Node, key: str) -> str:
    return f"{node.id}/{key}"


@router.put("/nodes/{node_id}/vars/{key}")
def put_var(
    node_id: int,
    key: str,
    req: VarPut,
    response: Response,
    if_match: str | None = IfMatch(),
    principal: Principal = editor,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    if not key or len(key) > 255:
        raise HTTPException(422, "invalid key")
    # No-op check happens before the precondition so idempotent re-runs (modules)
    # never need a new revision; a real change still needs a fresh If-Match.
    node = get_live_node(session, node_id)
    current = live_vars(session, node.id)
    if key in current and current[key] == req.value:
        rev_now = current_revision(session)
        response.headers["ETag"] = etag(rev_now)
        return {"revision": rev_now, "changed": False, "node_id": node.id, "key": key, "value": req.value}
    _check_value(session, key, req.value, node.path)
    rev = begin_write(session, if_match, actor=principal.actor, message=req.message or f"set {key} on {node.path}")
    _set_var(session, node, key, req.value, rev)
    session.flush()
    check_export(session)
    before = {"path": node.path, "key": key, "value": current[key]} if key in current else None
    _audit(session, principal, rev, "variable.set", "node_variable", _var_target(node, key), before, {"path": node.path, "key": key, "value": req.value})
    return _finish(response, rev, {"node_id": node.id, "key": key, "value": req.value})


@router.delete("/nodes/{node_id}/vars/{key}")
def delete_var(
    node_id: int,
    key: str,
    response: Response,
    if_match: str | None = IfMatch(),
    principal: Principal = editor,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    rev = begin_write(session, if_match, actor=principal.actor, message=f"unset {key} on node {node_id}")
    node = get_live_node(session, node_id)
    current = session.scalar(
        select(NodeVariable).where(NodeVariable.node_id == node.id, NodeVariable.key == key, NodeVariable.deleted_rev.is_(None))
    )
    if current is None:
        raise HTTPException(404, f"{key} is not set on {node.path}")
    current.deleted_rev = rev.id
    session.flush()
    _audit(session, principal, rev, "variable.unset", "node_variable", _var_target(node, key), {"path": node.path, "key": key, "value": current.value}, None)
    return _finish(response, rev, {"node_id": node.id, "key": key})


# ------------------------------------------------------- variable definitions


class DefinitionPut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    merge_strategy: MergeStrategy = MergeStrategy.replace
    description: str | None = None
    json_schema: dict[str, Any] | None = None
    message: str | None = None


def definition_out(d: VariableDefinition) -> dict[str, Any]:
    return {"name": d.name, "merge_strategy": d.merge_strategy.value, "description": d.description, "json_schema": d.json_schema}


@router.put("/variables/{name}")
def put_definition(
    name: str,
    req: DefinitionPut,
    response: Response,
    if_match: str | None = IfMatch(),
    principal: Principal = editor,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    if req.json_schema is not None:
        try:
            schema.check_schema(req.json_schema)
        except schema.SchemaViolation as exc:
            raise HTTPException(422, str(exc)) from exc
    definition = session.get(VariableDefinition, name)
    before = definition_out(definition) if definition is not None else None
    wanted = {"name": name, "merge_strategy": req.merge_strategy.value, "description": req.description, "json_schema": req.json_schema}
    if before == wanted:  # idempotent re-put: no revision, no precondition, no audit row
        rev_now = current_revision(session)
        response.headers["ETag"] = etag(rev_now)
        return {"revision": rev_now, "changed": False, "definition": before}
    rev = begin_write(session, if_match, actor=principal.actor, message=req.message or f"declare variable {name}")
    definition = definition or VariableDefinition(name=name)
    definition.merge_strategy = req.merge_strategy
    definition.description = req.description
    definition.json_schema = req.json_schema
    session.add(definition)
    session.flush()
    try:
        schema.check_live(session, keys={name})  # a new schema must accept every live value
    except schema.SchemaViolation as exc:
        raise HTTPException(422, str(exc)) from exc
    out = definition_out(definition)
    _audit(session, principal, rev, "definition.set", "variable_definition", name, before, out)
    return _finish(response, rev, {"definition": out})


@router.delete("/variables/{name}")
def delete_definition(
    name: str,
    response: Response,
    if_match: str | None = IfMatch(),
    principal: Principal = editor,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    rev = begin_write(session, if_match, actor=principal.actor, message=f"undeclare variable {name}")
    definition = session.get(VariableDefinition, name)
    if definition is None:
        raise HTTPException(404, f"variable {name!r} is not declared")
    before = definition_out(definition)
    session.delete(definition)
    session.flush()
    _audit(session, principal, rev, "definition.delete", "variable_definition", name, before, None)
    return _finish(response, rev, {"name": name})

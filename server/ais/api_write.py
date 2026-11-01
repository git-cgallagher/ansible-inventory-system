"""Write routes with optimistic locking (``If-Match``), editor role.

Every write: ``begin_write`` (lock, precondition, new ``Revision``) → change →
``check_export`` (re-run the exporter on the live snapshot and map its errors to
409, so group-name and tag-group collisions are rejected at write time by the same
code that would reject them at export) → respond with the new revision and ETag.
Nothing is updated in place: nodes and variables are closed (``deleted_rev``) and
reopened under the new revision.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ais.auth import Principal, require
from ais.db import get_session
from ais.importer import ImportError_, check_kind, check_name, infer_kind, live_node_by_path
from ais.locking import IfMatch, begin_write, etag
from ais.models import PATH_SEP, MergeStrategy, Node, NodeKind, NodeVariable, Revision, Role, VariableDefinition
from ais.resolver import build_export, current_revision, load_snapshot

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


def check_export(session: Session) -> None:
    """Reject the pending write if the live tree no longer exports cleanly."""
    try:
        build_export(load_snapshot(session))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


def _finish(response: Response, rev: Revision, body: dict[str, Any]) -> dict[str, Any]:
    response.headers["ETag"] = etag(rev.id)
    return {"revision": rev.id, "changed": True, **body}


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
    """Only non-temporal metadata; renames and moves are a later ``move`` endpoint."""

    model_config = ConfigDict(extra="forbid")

    inventory_name: str | None = None
    clear_inventory_name: bool = False
    meta: dict[str, Any] | None = None
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
    parent = get_live_node(session, req.parent_id) if req.parent_id is not None else live_node_by_path(session, req.parent_path or "")
    if parent is None:
        raise HTTPException(404, f"parent {req.parent_path!r} does not exist")
    kind = req.kind or infer_kind(parent.depth + 1, None)
    try:
        check_name(req.name)
        check_kind(parent, kind)
    except ImportError_ as exc:
        raise HTTPException(422, str(exc)) from exc
    if kind is NodeKind.host and req.inventory_name:
        raise HTTPException(422, "hosts have no inventory_name")
    path = f"{parent.path}{PATH_SEP}{req.name}"
    if live_node_by_path(session, path) is not None:
        raise HTTPException(409, f"node {path} already exists")
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
    return _finish(response, rev, {"node": node_out(node, req.vars)})


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
    if req.clear_inventory_name:
        node.inventory_name = None
    elif req.inventory_name is not None:
        node.inventory_name = req.inventory_name
    if req.meta is not None:
        node.meta = req.meta
    session.flush()
    check_export(session)
    return _finish(response, rev, {"node": node_out(node, live_vars(session, node.id))})


@router.delete("/nodes/{node_id}")
def delete_node(
    node_id: int,
    response: Response,
    if_match: str | None = IfMatch(),
    principal: Principal = editor,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    rev = begin_write(session, if_match, actor=principal.actor, message=f"delete node {node_id}")
    node = get_live_node(session, node_id)
    if node.kind is NodeKind.all:
        raise HTTPException(422, "the root cannot be deleted")
    prefix = node.path + PATH_SEP
    subtree = [
        n
        for n in session.scalars(select(Node).where(Node.deleted_rev.is_(None)))
        if n.path == node.path or n.path.startswith(prefix)
    ]
    ids = [n.id for n in subtree]
    for n in subtree:
        n.deleted_rev = rev.id
    for nv in session.scalars(select(NodeVariable).where(NodeVariable.node_id.in_(ids), NodeVariable.deleted_rev.is_(None))):
        nv.deleted_rev = rev.id
    session.flush()
    return _finish(response, rev, {"deleted": [n.path for n in subtree]})


# ------------------------------------------------------------------ variables


class VarPut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Any
    message: str | None = None


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
    rev = begin_write(session, if_match, actor=principal.actor, message=req.message or f"set {key} on {node.path}")
    _set_var(session, node, key, req.value, rev)
    session.flush()
    check_export(session)
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
    rev = begin_write(session, if_match, actor=principal.actor, message=req.message or f"declare variable {name}")
    definition = session.get(VariableDefinition, name) or VariableDefinition(name=name)
    definition.merge_strategy = req.merge_strategy
    definition.description = req.description
    definition.json_schema = req.json_schema
    session.add(definition)
    session.flush()
    return _finish(response, rev, {"definition": definition_out(definition)})


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
    session.delete(definition)
    session.flush()
    return _finish(response, rev, {"name": name})

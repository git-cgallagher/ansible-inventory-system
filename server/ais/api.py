"""FastAPI application.

Every ``/api/v1`` route needs a bearer token (``ais.auth``); reads need ``viewer``,
writes ``editor`` (``ais.api_write``), user management ``admin`` (``ais.api_auth``).
``/healthz`` is open.
"""

from __future__ import annotations

import hashlib
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ais import __version__, api_auth, api_write, oidc
from ais.auth import Principal, require
from ais.db import get_session, init_db
from ais.importer import ImportError_, import_tree
from ais.locking import check_precondition, etag
from ais.models import Node, NodeKind, Revision, Role, VariableDefinition
from ais.render import MOCK_HOSTNAME, render_templates
from ais.resolver import GROUP_SEP, build_export, current_revision, load_snapshot, resolve
from ais.snapshots import diff, get_snapshot, list_snapshots, materialize


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Ansible Inventory System", version=__version__, lifespan=_lifespan)
if oidc.configured():  # the OIDC login flow keeps its state in a signed cookie
    from starlette.middleware.sessions import SessionMiddleware

    app.add_middleware(SessionMiddleware, secret_key=os.environ["AIS_SECRET_KEY"], https_only=os.environ.get("AIS_COOKIE_SECURE", "1") == "1")
app.include_router(api_auth.router)
app.include_router(api_write.router)
app.include_router(oidc.router)

viewer = Depends(require(Role.viewer))
editor = Depends(require(Role.editor))


class RenderRequest(BaseModel):
    host: str | None = None
    path: str | None = Field(default=None, description="Mock host position, e.g. all.commercial.prod.dc1.web")
    overrides: dict[str, Any] | None = None
    facts: dict[str, Any] | None = Field(default=None, description="Mock facts (keys with or without the ansible_ prefix)")
    revision: int | None = None
    group_separator: str = GROUP_SEP
    template: bool = Field(default=True, description="Also evaluate Jinja (rendered + errors)")
    mock_hostname: str = MOCK_HOSTNAME


class ImportRequest(BaseModel):
    tree: dict[str, Any]
    message: str | None = None


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


# --------------------------------------------------------------------- export


def _check_revision(session: Session, revision: int | None) -> None:
    if revision is not None and session.get(Revision, revision) is None:
        raise HTTPException(404, f"revision {revision} does not exist")


def export_etag(revision: int, subtree: str | None, sep: str, emit_tags: bool) -> str:
    """Revision plus a digest of the parameters the export varies by."""
    params = hashlib.sha256(f"{subtree or ''}|{sep}|{int(emit_tags)}".encode()).hexdigest()[:8]
    return f'"{revision}.{params}"'


@app.get("/api/v1/export")
def export(
    request: Request,
    response: Response,
    revision: int | None = None,
    subtree: str | None = None,
    group_separator: str = Query(GROUP_SEP, max_length=4),
    emit_tags: bool = True,
    principal: Principal = viewer,
    session: Session = Depends(get_session),
) -> Any:
    _check_revision(session, revision)
    rev = revision if revision is not None else current_revision(session)
    tag = export_etag(rev, subtree, group_separator, emit_tags)
    if request.headers.get("if-none-match") == tag:
        return Response(status_code=304, headers={"ETag": tag})
    try:
        snap = load_snapshot(session, rev, subtree)
        data = build_export(snap, sep=group_separator, emit_tags=emit_tags)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    materialize(session, snap)
    response.headers["ETag"] = tag
    return data


# --------------------------------------------------------------------- render


@app.post("/api/v1/render")
def render(req: RenderRequest, principal: Principal = viewer, session: Session = Depends(get_session)) -> dict[str, Any]:
    if not (req.host or req.path):
        raise HTTPException(422, "host or path is required")
    _check_revision(session, req.revision)
    snap = load_snapshot(session, req.revision)
    try:
        out = resolve(snap, host=req.host, path=req.path, overrides=req.overrides, sep=req.group_separator)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    if req.host:
        materialize(session, snap, [req.host])
    if req.template:
        hostname = req.host or req.mock_hostname
        out["rendered"], out["errors"] = render_templates(out["resolved"], facts=req.facts, hostname=hostname, chain=out["chain"])
    return out


# ---------------------------------------------------------------------- reads


@app.get("/api/v1/nodes")
def nodes(
    subtree: str | None = None,
    revision: int | None = None,
    principal: Principal = viewer,
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    _check_revision(session, revision)
    snap = load_snapshot(session, revision)
    rows = sorted(snap.nodes.values(), key=lambda n: n.path)
    if subtree:
        rows = [n for n in rows if n.path == subtree or n.path.startswith(subtree + ".")]
    return [api_write.node_out(n) for n in rows]


@app.get("/api/v1/nodes/{node_id}")
def node(node_id: int, principal: Principal = viewer, session: Session = Depends(get_session)) -> dict[str, Any]:
    n = session.get(Node, node_id)
    if n is None:
        raise HTTPException(404, f"node {node_id} does not exist")
    return api_write.node_out(n, api_write.live_vars(session, n.id))


@app.get("/api/v1/nodes/{node_id}/children")
def children(node_id: int, principal: Principal = viewer, session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    api_write.get_live_node(session, node_id)
    rows = session.scalars(select(Node).where(Node.parent_id == node_id, Node.deleted_rev.is_(None)).order_by(Node.name))
    return [api_write.node_out(n) for n in rows]


@app.get("/api/v1/nodes/{node_id}/vars/{key}")
def get_var(node_id: int, key: str, principal: Principal = viewer, session: Session = Depends(get_session)) -> dict[str, Any]:
    n = api_write.get_live_node(session, node_id)
    values = api_write.live_vars(session, n.id)
    if key not in values:
        raise HTTPException(404, f"{key} is not set on {n.path}")
    return {"node_id": n.id, "key": key, "value": values[key]}


@app.get("/api/v1/variables")
def variables(principal: Principal = viewer, session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    return [api_write.definition_out(d) for d in session.scalars(select(VariableDefinition).order_by(VariableDefinition.name))]


@app.get("/api/v1/variables/{name}")
def variable(name: str, principal: Principal = viewer, session: Session = Depends(get_session)) -> dict[str, Any]:
    d = session.get(VariableDefinition, name)
    if d is None:
        raise HTTPException(404, f"variable {name!r} is not declared")
    return api_write.definition_out(d)


@app.get("/api/v1/revisions")
def revisions(limit: int = Query(50, ge=1, le=1000), before: int | None = None, principal: Principal = viewer, session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    stmt = select(Revision).order_by(Revision.id.desc()).limit(limit)
    if before is not None:
        stmt = stmt.where(Revision.id < before)
    return [{"id": r.id, "actor": r.actor, "source": r.source, "ts": r.ts, "message": r.message} for r in session.scalars(stmt)]


# ------------------------------------------------------------------ snapshots


@app.get("/api/v1/hosts/{name}/snapshots")
def host_snapshots(name: str, principal: Principal = viewer, session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    rows = list_snapshots(session, name)
    if not rows and not any(n.kind is NodeKind.host and n.name == name for n in load_snapshot(session).nodes.values()):
        raise HTTPException(404, f"unknown host {name!r}")
    return rows


@app.get("/api/v1/hosts/{name}/snapshots/{revision}")
def host_snapshot(name: str, revision: int, principal: Principal = viewer, session: Session = Depends(get_session)) -> dict[str, Any]:
    row = get_snapshot(session, name, revision)
    if row is None:
        raise HTTPException(404, f"host {name!r} does not exist at revision {revision}")
    return {"host": name, "revision": row.revision, "resolved_hash": row.resolved_hash, "resolved": row.resolved, "created_at": row.created_at}


@app.get("/api/v1/diff")
def diff_(
    host: str,
    from_: int = Query(alias="from"),
    to: int = Query(),
    principal: Principal = viewer,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    out = diff(session, host, from_, to)
    if out is None:
        raise HTTPException(404, f"host {host!r} does not exist at one of the revisions")
    return out


# --------------------------------------------------------------------- import


@app.post("/api/v1/import", status_code=201)
def import_(
    req: ImportRequest,
    response: Response,
    if_match: str | None = api_write.IfMatch(),
    principal: Principal = editor,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    check_precondition(session, if_match)  # the importer opens its own revision
    try:
        rev = import_tree(session, req.tree, actor=principal.actor, message=req.message)
    except ImportError_ as exc:
        raise HTTPException(422, str(exc)) from exc
    api_write.check_export(session)
    response.headers["ETag"] = etag(rev.id)
    return {"revision": rev.id}

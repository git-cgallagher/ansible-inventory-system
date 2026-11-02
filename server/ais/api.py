"""FastAPI application.

Every ``/api/v1`` route needs a bearer token (``ais.auth``); reads need ``viewer``,
writes ``editor`` (``ais.api_write``), user management ``admin`` (``ais.api_auth``).
``/healthz`` is open. The web UI (``web/``, built into ``ais/static``) is served at ``/``
and ``/assets/…``: static files only, no data, so they need no token; every call the UI
makes goes to ``/api/v1`` with the bearer like any other client.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import pathlib
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from ais import __version__, api_auth, api_tags, api_write, audit, oidc
from ais.auth import Principal, require
from ais.db import get_session, init_db
from ais.importer import ImportError_, import_tree_report, live_node_by_path
from ais.locking import check_precondition, etag
from ais.models import AuditLog, Node, NodeKind, Revision, Role, VariableDefinition
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
app.add_middleware(audit.RequestIdMiddleware)
app.include_router(api_auth.router)
app.include_router(api_write.router)
app.include_router(api_tags.router)
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
    dry_run: bool = Field(default=False, description="Run the import and roll it back: report what it would change, write nothing")


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    # ``oidc`` tells the login page whether to offer single sign-on.
    return {"status": "ok", "version": __version__, "oidc": oidc.configured()}


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


@app.get("/api/v1/nodes/{ref}")
def node(ref: str, principal: Principal = viewer, session: Session = Depends(get_session)) -> dict[str, Any]:
    """One node with its live variables, by id (any row, deleted included) or by
    path (``all.commercial.prod``, live rows only)."""
    n = session.get(Node, int(ref)) if ref.isdigit() else live_node_by_path(session, ref)
    if n is None:
        raise HTTPException(404, f"node {ref!r} does not exist")
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


# ---------------------------------------------------------------------- audit


@app.get("/api/v1/audit")
def audit_log(
    actor: str | None = None,
    action: str | None = Query(None, description="Exact action, or a prefix ending in '.' such as 'node.'"),
    target_type: str | None = None,
    target_id: str | None = None,
    revision: int | None = None,
    request_id: str | None = None,
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
    limit: int = Query(50, ge=1, le=1000),
    before: int | None = Query(None, description="Only rows with an id below this (paging, newest first)"),
    node: str | None = Query(None, description="A node by id or live path: its rows and its variables' rows under every id it had (moves change ids)"),
    key: str | None = Query(None, description="With node: only that variable's rows"),
    principal: Principal = editor,
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    """Audit rows, newest first. Editor or admin; viewers get 403."""
    stmt = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)
    if key is not None and node is None:
        raise HTTPException(422, "key needs node")
    if node is not None:
        n = session.get(Node, int(node)) if node.isdigit() else live_node_by_path(session, node)
        if n is None:
            raise HTTPException(404, f"node {node!r} does not exist")
        ids = audit.node_lineage(session, n.id)
        if key is not None:
            stmt = stmt.where(AuditLog.target_type == "node_variable", AuditLog.target_id.in_([f"{i}/{key}" for i in ids]))
        else:
            # A variable's target_id is "<node id>/<key>"; the prefix is digits and "/" only.
            var_rows = or_(*[AuditLog.target_id.like(f"{i}/%") for i in ids])
            stmt = stmt.where(
                or_(
                    and_(AuditLog.target_type == "node", AuditLog.target_id.in_([str(i) for i in ids])),
                    and_(AuditLog.target_type == "node_variable", var_rows),
                )
            )
    if actor is not None:
        stmt = stmt.where(AuditLog.actor == actor)
    if action is not None:
        stmt = stmt.where(AuditLog.action.like(action + "%") if action.endswith(".") else AuditLog.action == action)
    if target_type is not None:
        stmt = stmt.where(AuditLog.target_type == target_type)
    if target_id is not None:
        stmt = stmt.where(AuditLog.target_id == target_id)
    if revision is not None:
        stmt = stmt.where(AuditLog.revision == revision)
    if request_id is not None:
        stmt = stmt.where(AuditLog.request_id == request_id)
    if since is not None:
        stmt = stmt.where(AuditLog.ts >= audit.as_utc(since))
    if until is not None:
        stmt = stmt.where(AuditLog.ts < audit.as_utc(until))
    if before is not None:
        stmt = stmt.where(AuditLog.id < before)
    return [audit.audit_out(r) for r in session.scalars(stmt)]


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


@app.post("/api/v1/import")
def import_(
    req: ImportRequest,
    response: Response,
    if_match: str | None = api_write.IfMatch(),
    principal: Principal = editor,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Additive bulk import under one revision. 201 when it changed something; 200 with
    ``changed: false`` (and no revision, no audit row) when the tree already matched;
    ``dry_run`` reports the same counts and rolls back (no ``If-Match`` needed)."""
    if not req.dry_run:
        check_precondition(session, if_match)  # the importer opens its own revision
    try:
        result = import_tree_report(session, req.tree, actor=principal.actor, message=req.message)
    except ImportError_ as exc:
        raise HTTPException(422, str(exc)) from exc
    api_write.check_export(session)
    summary = {
        "nodes_created": result.nodes_created,
        "variables_set": result.variables_set,
        "definitions": result.definitions_changed,
    }
    if req.dry_run or not result.changed:
        session.rollback()
        rev_now = current_revision(session)
        response.headers["ETag"] = etag(rev_now)
        return {"revision": rev_now, "changed": result.changed, "dry_run": req.dry_run, **summary}
    rev = result.revision
    audit.record(session, actor=principal.actor, action="import", target_type="tree", target_id="all", after={"revision": rev.id, **summary}, source="import", revision=rev.id)
    response.status_code = 201
    response.headers["ETag"] = etag(rev.id)
    return {"revision": rev.id, "changed": True, "dry_run": False, **summary}


# --------------------------------------------------------------------- web UI


def web_dir() -> pathlib.Path:
    """The built UI: ``AIS_WEB_DIR`` or ``ais/static`` (``npm run build`` in ``web/``
    writes there; the image bakes it in). Read per request, so a missing build only
    makes ``/`` answer 404 and the API runs regardless."""
    return pathlib.Path(os.environ.get("AIS_WEB_DIR") or pathlib.Path(__file__).parent / "static")


_NO_UI = "the web UI is not built: run `npm ci && npm run build` in web/ (writes server/ais/static) or set AIS_WEB_DIR"


@app.get("/", include_in_schema=False)
def ui_index() -> FileResponse:
    index = web_dir() / "index.html"
    if not index.is_file():
        raise HTTPException(404, _NO_UI)
    return FileResponse(index, headers={"Cache-Control": "no-cache"})


def ui_assets() -> dict[str, pathlib.Path]:
    """The built assets by file name. Vite writes them flat into ``assets/``."""
    root = web_dir() / "assets"
    if not root.is_dir():
        return {}
    return {p.name: p for p in root.iterdir() if p.is_file()}


@app.get("/assets/{name:path}", include_in_schema=False)
def ui_asset(name: str) -> FileResponse:
    # Look the request up by exact name in the directory listing instead of joining it
    # onto a path: no file path is ever built from the URL, so ``..``, encoded
    # separators and symlink tricks have nothing to act on.
    target = ui_assets().get(name)
    if target is None:
        raise HTTPException(404, "not found")
    # Vite content-hashes asset names, so they never change under one URL.
    return FileResponse(target, headers={"Cache-Control": "public, max-age=31536000, immutable"})

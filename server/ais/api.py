"""FastAPI application (spike scope: export, render, tree, import, health).

Auth, tokens, per-node writes and audit endpoints arrive in phase 1/2.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ais import __version__
from ais.db import get_session, init_db
from ais.importer import ImportError_, import_tree
from ais.models import Node
from ais.resolver import GROUP_SEP, build_export, load_snapshot, resolve

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Ansible Inventory System", version=__version__, lifespan=_lifespan)


class RenderRequest(BaseModel):
    host: str | None = None
    path: str | None = Field(default=None, description="Mock host position, e.g. all.commercial.prod.dc1.web")
    overrides: dict[str, Any] | None = None
    revision: int | None = None
    group_separator: str = GROUP_SEP


class ImportRequest(BaseModel):
    tree: dict[str, Any]
    actor: str = "api"
    message: str | None = None


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.get("/api/v1/export")
def export(
    response: Response,
    revision: int | None = None,
    subtree: str | None = None,
    group_separator: str = Query(GROUP_SEP, max_length=4),
    emit_tags: bool = True,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    try:
        snap = load_snapshot(session, revision, subtree)
        data = build_export(snap, sep=group_separator, emit_tags=emit_tags)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    response.headers["ETag"] = f'"{snap.revision}"'
    return data


@app.post("/api/v1/render")
def render(req: RenderRequest, session: Session = Depends(get_session)) -> dict[str, Any]:
    if not (req.host or req.path):
        raise HTTPException(422, "host or path is required")
    snap = load_snapshot(session, req.revision)
    try:
        return resolve(snap, host=req.host, path=req.path, overrides=req.overrides, sep=req.group_separator)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/v1/nodes")
def nodes(subtree: str | None = None, session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    stmt = select(Node).order_by(Node.path)
    rows = session.scalars(stmt).all()
    if subtree:
        rows = [n for n in rows if n.path == subtree or n.path.startswith(subtree + ".")]
    return [
        {"id": n.id, "parent_id": n.parent_id, "kind": n.kind.value, "name": n.name, "path": n.path, "depth": n.depth}
        for n in rows
    ]


@app.post("/api/v1/import", status_code=201)
def import_(req: ImportRequest, session: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        rev = import_tree(session, req.tree, actor=req.actor, message=req.message)
    except ImportError_ as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"revision": rev.id}

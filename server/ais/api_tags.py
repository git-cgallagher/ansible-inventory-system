"""Tag routes (``/api/v1/tags``): explicit, var-less targeting groups.

A tag is a name, a description and a set of member nodes; the exporter emits it as a
group directly under ``all`` (named ``resolver.sanitize(name)``: ``gpu-canary`` is group
``gpu_canary``, the rule node names follow) holding the member hosts (a non-host member contributes
every host under it). **Tags never carry variables**: no model has a column for them
and no route here accepts one (``extra="forbid"``).

Tags are not temporal (like variable definitions): a change applies to every
revision's export. Writes still take the write lock, need ``If-Match`` and open a
revision, because they change what the plugin receives (and so its ETag). Members are
named by path, never by id (a move changes ids); a moved node keeps its memberships.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ais.api_write import DRY_RUN, _audit, answer, begin, check_export, unchanged
from ais.auth import Principal, require
from ais.db import get_session
from ais.importer import live_node_by_path, tag_conflict_error, tag_doc, tag_name_error
from ais.locking import IfMatch
from ais.models import Node, Role, Tag, TagMember

router = APIRouter(prefix="/api/v1", tags=["tags"])
viewer = Depends(require(Role.viewer))
editor = Depends(require(Role.editor))



def tag_out(session: Session, tag: Tag) -> dict[str, Any]:
    return tag_doc(session, tag)  # type: ignore[return-value]


def _get_tag(session: Session, name: str) -> Tag:
    tag = session.scalar(select(Tag).where(Tag.name == name))
    if tag is None:
        raise HTTPException(404, f"tag {name!r} does not exist")
    return tag


def check_tag_name(name: str) -> None:
    error = tag_name_error(name)  # shared with the importer's ``tags:`` block
    if error:
        raise HTTPException(422, error)


@router.get("/tags")
def list_tags(principal: Principal = viewer, session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    return [tag_out(session, t) for t in session.scalars(select(Tag).order_by(Tag.name))]


@router.get("/tags/{name}")
def get_tag(name: str, principal: Principal = viewer, session: Session = Depends(get_session)) -> dict[str, Any]:
    return tag_out(session, _get_tag(session, name))


class TagPut(BaseModel):
    """Declarative: ``members`` (paths) is the complete member set when given."""

    model_config = ConfigDict(extra="forbid")

    description: str | None = None
    members: list[str] | None = Field(default=None, description="Member node paths; omit to keep the current members")
    message: str | None = None


@router.put("/tags/{name}")
def put_tag(
    name: str,
    req: TagPut,
    response: Response,
    dry_run: bool = Query(False, description=DRY_RUN),
    if_match: str | None = IfMatch(),
    principal: Principal = editor,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Create or converge a tag. An unchanged tag answers ``changed: false`` without a
    revision (no precondition, no audit row), like the other idempotent PUTs."""
    check_tag_name(name)
    tag = session.scalar(select(Tag).where(Tag.name == name))
    before = tag_out(session, tag) if tag is not None else None
    wanted_members = sorted(set(req.members)) if req.members is not None else (before["members"] if before else [])
    wanted = {"name": name, "description": req.description, "members": wanted_members}
    if before == wanted:
        return unchanged(response, session, dry_run, {"tag": before})
    rev, preview = begin(session, if_match, dry_run, actor=principal.actor, message=req.message or f"tag {name}")
    out = converge_tag(session, name, req.description, wanted_members)
    check_export(session)
    _audit(session, principal, rev, "tag.set", "tag", name, before, out)
    return answer(session, response, rev, preview, {"tag": out})


def converge_tag(session: Session, name: str, description: str | None, members: list[str], restore_ids: list[int] | None = None) -> dict[str, Any]:
    """Create or converge tag ``name``: ``description`` replaced, live members exactly
    ``members`` (paths; 404 for one that is not live). Memberships of deleted nodes are left
    alone; ``restore_ids`` adds membership rows for such node ids (a revert of a tag delete
    puts back the ones the delete removed). Returns ``tag_out``. Shared with ``ais.revert``.

    422 when the tag's group name is taken (``tag_conflict_error``: another tag that maps to
    the same group, or a hierarchy group), with or without members."""
    conflict = tag_conflict_error(session, name)
    if conflict:
        raise HTTPException(422, conflict)
    nodes: dict[str, Node] = {}
    for path in members:
        node = live_node_by_path(session, path)
        if node is None:
            raise HTTPException(404, f"node {path} does not exist")
        nodes[path] = node
    tag = session.scalar(select(Tag).where(Tag.name == name))
    if tag is None:
        tag = Tag(name=name)
        session.add(tag)
    tag.description = description
    session.flush()
    current = {
        tm.node_id: tm
        for tm in session.scalars(
            select(TagMember).join(Node, Node.id == TagMember.node_id).where(TagMember.tag_id == tag.id, Node.deleted_rev.is_(None))
        )
    }
    keep = {n.id for n in nodes.values()}
    for node_id, tm in current.items():
        if node_id not in keep:
            session.delete(tm)
    for node_id in keep - set(current):
        session.add(TagMember(tag_id=tag.id, node_id=node_id))
    if restore_ids:
        existing = set(session.scalars(select(TagMember.node_id).where(TagMember.tag_id == tag.id)))
        for node_id in set(restore_ids) - existing - keep:
            if session.get(Node, node_id) is not None:
                session.add(TagMember(tag_id=tag.id, node_id=node_id))
    session.flush()
    return tag_out(session, tag)


def remove_tag(session: Session, tag: Tag) -> dict[str, Any]:
    """Delete ``tag`` and every membership row; returns the ``tag.delete`` audit row's
    ``before``: ``tag_out`` plus ``deleted_members`` (``[{id, path}]``), the memberships of
    nodes that are deleted now, which ``tag_out`` leaves out and a revert puts back (a
    restore of those nodes then brings them back into the tag)."""
    before = tag_out(session, tag)
    gone = session.execute(
        select(Node.id, Node.path).join(TagMember, TagMember.node_id == Node.id).where(TagMember.tag_id == tag.id, Node.deleted_rev.is_not(None))
    ).all()
    if gone:
        before["deleted_members"] = [{"id": i, "path": p} for i, p in sorted(gone)]
    session.execute(delete(TagMember).where(TagMember.tag_id == tag.id))
    session.delete(tag)
    session.flush()
    return before


@router.delete("/tags/{name}")
def delete_tag(
    name: str,
    response: Response,
    dry_run: bool = Query(False, description=DRY_RUN),
    if_match: str | None = IfMatch(),
    principal: Principal = editor,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    rev, preview = begin(session, if_match, dry_run, actor=principal.actor, message=f"delete tag {name}")
    tag = _get_tag(session, name)
    before = remove_tag(session, tag)
    _audit(session, principal, rev, "tag.delete", "tag", name, before, None)
    return answer(session, response, rev, preview, {"name": name})

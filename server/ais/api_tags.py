"""Tag routes (``/api/v1/tags``): explicit, var-less targeting groups.

A tag is a name, a description and a set of member nodes; the exporter emits it as a
group directly under ``all`` holding the member hosts (a non-host member contributes
every host under it). **Tags never carry variables**: no model has a column for them
and no route here accepts one (``extra="forbid"``).

Tags are not temporal (like variable definitions): a change applies to every
revision's export. Writes still take the write lock, need ``If-Match`` and open a
revision, because they change what the plugin receives (and so its ETag). Members are
named by path, never by id (a move changes ids); a moved node keeps its memberships.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ais.api_write import _audit, _finish, check_export
from ais.auth import Principal, require
from ais.db import get_session
from ais.importer import live_node_by_path
from ais.locking import IfMatch, begin_write, etag
from ais.models import TAGGED_KINDS, Node, Role, Tag, TagMember
from ais.resolver import GROUP_SEP, current_revision

router = APIRouter(prefix="/api/v1", tags=["tags"])
viewer = Depends(require(Role.viewer))
editor = Depends(require(Role.editor))

# Valid Ansible group names as they are emitted (no sanitizing needed).
TAG_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,255}$")
# The exporter's automatic tag groups are ``<kind>_<name>`` (``tier_prod``); an explicit
# tag of that shape would silently merge into one, so those prefixes are reserved.
RESERVED_PREFIXES = tuple(f"{k.value}{GROUP_SEP}" for k in sorted(TAGGED_KINDS, key=lambda k: k.value))


def live_members(session: Session, tag: Tag) -> list[str]:
    rows = session.scalars(
        select(Node.path).join(TagMember, TagMember.node_id == Node.id).where(TagMember.tag_id == tag.id, Node.deleted_rev.is_(None))
    )
    return sorted(rows)


def tag_out(session: Session, tag: Tag) -> dict[str, Any]:
    return {"name": tag.name, "description": tag.description, "members": live_members(session, tag)}


def _get_tag(session: Session, name: str) -> Tag:
    tag = session.scalar(select(Tag).where(Tag.name == name))
    if tag is None:
        raise HTTPException(404, f"tag {name!r} does not exist")
    return tag


def check_tag_name(name: str) -> None:
    if not TAG_NAME_RE.match(name):
        raise HTTPException(422, f"invalid tag name {name!r}: letters, digits and '_' only (max 255)")
    if name.startswith(RESERVED_PREFIXES) or name == "all":
        raise HTTPException(422, f"tag name {name!r} is reserved (prefixes {', '.join(RESERVED_PREFIXES)} are automatic tag groups)")


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
        rev_now = current_revision(session)
        response.headers["ETag"] = etag(rev_now)
        return {"revision": rev_now, "changed": False, "tag": before}
    rev = begin_write(session, if_match, actor=principal.actor, message=req.message or f"tag {name}")
    nodes: dict[str, Node] = {}
    for path in wanted_members:
        node = live_node_by_path(session, path)
        if node is None:
            raise HTTPException(404, f"node {path} does not exist")
        nodes[path] = node
    if tag is None:
        tag = Tag(name=name)
        session.add(tag)
    tag.description = req.description
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
    session.flush()
    check_export(session)  # a tag named like a hierarchy group is a 409 here
    out = tag_out(session, tag)
    _audit(session, principal, rev, "tag.set", "tag", name, before, out)
    return _finish(response, rev, {"tag": out})


@router.delete("/tags/{name}")
def delete_tag(
    name: str,
    response: Response,
    if_match: str | None = IfMatch(),
    principal: Principal = editor,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    rev = begin_write(session, if_match, actor=principal.actor, message=f"delete tag {name}")
    tag = _get_tag(session, name)
    before = tag_out(session, tag)
    session.execute(delete(TagMember).where(TagMember.tag_id == tag.id))
    session.delete(tag)
    session.flush()
    _audit(session, principal, rev, "tag.delete", "tag", name, before, None)
    return _finish(response, rev, {"name": name})

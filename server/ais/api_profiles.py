"""Profile routes (``/api/v1/profiles``, ``/api/v1/nodes/{id}/profiles``), issue #22.

A profile is a named bundle of variables attached to any number of non-host nodes; its
values apply at each node as a level between the node's parent and the node (``ais.profiles``).
Every write here is an ordinary locked write: ``If-Match`` (428/409), one revision, ``dry_run``
with ``impact``, an ``X-AIS-Change`` change id, one audit row, revertible (``ais.revert``):

* ``PUT /profiles/{name}``: create or converge (``description`` and ``vars`` replaced when
  given, kept when omitted; ``vars`` is the complete set). ``profile.create`` /
  ``profile.update``.
* ``PUT``/``DELETE /profiles/{name}/vars/{key}``: one value (``profile.update``). Bumping a
  driver everywhere the profile is attached is this one write.
* ``DELETE /profiles/{name}``: refused while attached (409 with the nodes) unless
  ``detach=true``, which detaches it everywhere in the same revision (``profile.delete``,
  whose ``before`` records every node's list).
* ``PUT /nodes/{id}/profiles``: a node's ordered list (first = lowest precedence);
  ``PUT``/``DELETE /nodes/{id}/profiles/{name}`` attach (appended, or at ``position``) and
  detach. ``profile.attach`` / ``profile.detach`` / ``node.profiles`` rows with the list before
  and after.

Profile rows carry the variables they changed in ``audit_key`` (``GET /audit?key=``). No
``check_export``: nothing here can change a group name or a host's name.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ais import profiles
from ais.api_write import DRY_RUN, _audit, answer, begin, get_live_node, review_keys, unchanged
from ais.auth import Principal, require
from ais.db import get_session
from ais.locking import IfMatch
from ais.models import Node, Profile, ProfileAttachment, ProfileVariable, Revision, Role
from ais.profiles import ProfileError
from ais.resolver import current_revision

router = APIRouter(prefix="/api/v1", tags=["profiles"])
viewer = Depends(require(Role.viewer))
editor = Depends(require(Role.editor))


def _http(exc: ProfileError) -> HTTPException:
    return HTTPException(exc.status, exc.detail)


def _name(name: str) -> None:
    error = profiles.name_error(name)
    if error:
        raise HTTPException(422, error)


def _get(session: Session, name: str, rev: int | None = None) -> dict[str, Any]:
    out = profiles.doc(session, name, rev)
    if out is None:
        raise HTTPException(404, f"profile {name!r} does not exist" + (f" at revision {rev}" if rev is not None else ""))
    return out


# ----------------------------------------------------------------------- reads


@router.get("/profiles")
def list_profiles(principal: Principal = viewer, session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    """Every live profile: name, description, its keys and how many nodes it is attached to."""
    keys: dict[str, list[str]] = {}
    for name, key in session.execute(select(ProfileVariable.profile, ProfileVariable.key).where(ProfileVariable.deleted_rev.is_(None)).order_by(ProfileVariable.key)):
        keys.setdefault(name, []).append(key)
    counts = dict(
        session.execute(
            select(ProfileAttachment.profile, func.count())
            .join(Node, Node.id == ProfileAttachment.node_id)
            .where(ProfileAttachment.deleted_rev.is_(None), Node.deleted_rev.is_(None))
            .group_by(ProfileAttachment.profile)
        ).all()
    )
    return [
        {"name": p.name, "description": p.description, "keys": keys.get(p.name, []), "attached": counts.get(p.name, 0)}
        for p in session.scalars(select(Profile).where(Profile.deleted_rev.is_(None)).order_by(Profile.name))
    ]


@router.get("/profiles/{name}")
def get_profile(
    name: str,
    revision: int | None = Query(None, description="As of this revision (default: current)"),
    principal: Principal = viewer,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """One profile with its values and where it is attached (``[{path, kind, position}]``,
    ``position`` = its place in that node's list, 0 = lowest precedence)."""
    if revision is not None and session.get(Revision, revision) is None:
        raise HTTPException(404, f"revision {revision} does not exist")
    rev = revision if revision is not None else current_revision(session)
    out = _get(session, name, rev)
    return {**out, "revision": rev, "attached": profiles.attachments(session, name, rev)}


# ---------------------------------------------------------------- profile writes


class ProfilePut(BaseModel):
    """Declarative: ``description`` and ``vars`` (the complete set) replace what is there
    when given and are kept when omitted."""

    model_config = ConfigDict(extra="forbid")

    description: str | None = None
    vars: dict[str, Any] | None = Field(default=None, description="The profile's complete variables; omit to keep them")
    message: str | None = None


def _write_profile(
    session: Session, response: Response, principal: Principal, if_match: str | None, dry_run: bool, name: str, wanted: dict[str, Any], message: str
) -> dict[str, Any]:
    before = profiles.doc(session, name)
    if before is not None and before["description"] == wanted["description"] and profiles.changed_keys(before, wanted) == []:
        return unchanged(response, session, dry_run, {"profile": before})  # no revision, no audit row
    rev, preview = begin(session, if_match, dry_run, actor=principal.actor, message=message)
    try:
        out = profiles.set_profile(session, rev, name, wanted)
    except ProfileError as exc:
        raise _http(exc) from exc
    keys = review_keys(session, rev)
    _audit(session, principal, rev, "profile.create" if before is None else "profile.update", "profile", name, before, out, keys=profiles.changed_keys(before, out))
    return answer(session, response, rev, preview, {"profile": out, **keys})


@router.put("/profiles/{name}")
def put_profile(
    name: str,
    req: ProfilePut,
    response: Response,
    dry_run: bool = Query(False, description=DRY_RUN),
    if_match: str | None = IfMatch(),
    principal: Principal = editor,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Create or converge a profile. An unchanged profile answers ``changed: false`` without
    a revision. Every value must satisfy its definition's ``json_schema`` (422)."""
    _name(name)
    before = profiles.doc(session, name)
    wanted = {
        "name": name,
        "description": req.description if "description" in req.model_fields_set else (before or {}).get("description"),
        "vars": req.vars if req.vars is not None else (before or {}).get("vars", {}),
    }
    return _write_profile(session, response, principal, if_match, dry_run, name, wanted, req.message or f"profile {name}")


class ProfileVarPut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Any
    message: str | None = None


@router.put("/profiles/{name}/vars/{key}")
def put_profile_var(
    name: str,
    key: str,
    req: ProfileVarPut,
    response: Response,
    dry_run: bool = Query(False, description=DRY_RUN),
    if_match: str | None = IfMatch(),
    principal: Principal = editor,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Set one of the profile's values: every node it is attached to (and every host below
    them) gets it, in one revision."""
    if not key or len(key) > 255:
        raise HTTPException(422, "invalid key")
    before = _get(session, name)
    wanted = {**before, "vars": {**before["vars"], key: req.value}}
    return _write_profile(session, response, principal, if_match, dry_run, name, wanted, req.message or f"set {key} in profile {name}")


@router.delete("/profiles/{name}/vars/{key}")
def delete_profile_var(
    name: str,
    key: str,
    response: Response,
    dry_run: bool = Query(False, description=DRY_RUN),
    if_match: str | None = IfMatch(),
    principal: Principal = editor,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    before = _get(session, name)
    if key not in before["vars"]:
        raise HTTPException(404, f"{key} is not set in profile {name}")
    wanted = {**before, "vars": {k: v for k, v in before["vars"].items() if k != key}}
    return _write_profile(session, response, principal, if_match, dry_run, name, wanted, f"unset {key} in profile {name}")


@router.delete("/profiles/{name}")
def delete_profile(
    name: str,
    response: Response,
    detach: bool = Query(False, description="Also detach it from every node it is attached to (refused with 409 while attached otherwise)"),
    dry_run: bool = Query(False, description=DRY_RUN),
    if_match: str | None = IfMatch(),
    principal: Principal = editor,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    rev, preview = begin(session, if_match, dry_run, actor=principal.actor, message=f"delete profile {name}")
    try:
        before = profiles.delete_profile(session, rev, name, detach=detach)
    except ProfileError as exc:
        raise _http(exc) from exc
    detached = [a["path"] for a in before["attached"]]
    _audit(session, principal, rev, "profile.delete", "profile", name, before, None, keys=sorted(before["vars"]))
    return answer(session, response, rev, preview, {"name": name, "detached": detached})


# ------------------------------------------------------------- attachments


class NodeProfilesPut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profiles: list[str] = Field(description="The node's profiles in application order (first = lowest precedence; the node's own values beat all)")
    message: str | None = None


class AttachPut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position: int | None = Field(default=None, ge=0, description="Place in the node's list (0 = lowest precedence); default: last, or where it is")
    message: str | None = None


def _write_list(
    session: Session, response: Response, principal: Principal, if_match: str | None, dry_run: bool, node_id: int, wanted: list[str] | None, compute, message: str
) -> dict[str, Any]:
    node = get_live_node(session, node_id)
    current = profiles.node_list(session, node.id)
    target = wanted if wanted is not None else compute(current)
    if target == current:
        return unchanged(response, session, dry_run, {"node_id": node.id, "path": node.path, "profiles": current})
    rev, preview = begin(session, if_match, dry_run, actor=principal.actor, message=message)
    node = get_live_node(session, node_id)
    try:
        before, after = profiles.set_node_profiles(session, rev, node, target)
    except ProfileError as exc:
        raise _http(exc) from exc
    action = profiles.attach_action(before, after)
    _audit(session, principal, rev, action, "node", node.id, {"path": node.path, "profiles": before}, {"path": node.path, "profiles": after})
    return answer(session, response, rev, preview, {"node_id": node.id, "path": node.path, "profiles": after})


@router.put("/nodes/{node_id}/profiles")
def put_node_profiles(
    node_id: int,
    req: NodeProfilesPut,
    response: Response,
    dry_run: bool = Query(False, description=DRY_RUN),
    if_match: str | None = IfMatch(),
    principal: Principal = editor,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Set the node's profile list exactly (attach, detach and reorder in one write)."""
    return _write_list(session, response, principal, if_match, dry_run, node_id, list(req.profiles), None, req.message or f"profiles of node {node_id}")


@router.put("/nodes/{node_id}/profiles/{name}")
def attach_profile(
    node_id: int,
    name: str,
    req: AttachPut,
    response: Response,
    dry_run: bool = Query(False, description=DRY_RUN),
    if_match: str | None = IfMatch(),
    principal: Principal = editor,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Attach ``name`` to the node: appended (the highest precedence among its profiles), or
    at ``position``; an attached profile without ``position`` is a no-op."""
    _name(name)

    def compute(current: list[str]) -> list[str]:
        if name in current and req.position is None:
            return current
        rest = [n for n in current if n != name]
        at = len(rest) if req.position is None else min(req.position, len(rest))
        return rest[:at] + [name] + rest[at:]

    return _write_list(session, response, principal, if_match, dry_run, node_id, None, compute, req.message or f"attach profile {name} to node {node_id}")


@router.delete("/nodes/{node_id}/profiles/{name}")
def detach_profile(
    node_id: int,
    name: str,
    response: Response,
    dry_run: bool = Query(False, description=DRY_RUN),
    if_match: str | None = IfMatch(),
    principal: Principal = editor,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    node = get_live_node(session, node_id)
    if name not in profiles.node_list(session, node.id):
        raise HTTPException(404, f"profile {name!r} is not attached to {node.path}")
    return _write_list(session, response, principal, if_match, dry_run, node_id, None, lambda cur: [n for n in cur if n != name], f"detach profile {name} from node {node_id}")

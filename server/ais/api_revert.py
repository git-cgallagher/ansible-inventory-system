"""Revert routes (editor): ``POST /api/v1/revisions/{id}/revert`` and ``POST
/api/v1/changes/{change}/revert``. The engine is ``ais.revert``.

A revert is a write like any other: ``If-Match`` (428/409), the write lock, one new
revision, ``check_export``, audit rows in the same transaction; a ``dry_run`` needs no
``If-Match``, applies the revert in the transaction to report what it would do (the
operations and the hosts whose resolved vars change) and rolls it back. A conflict (a later
revision touched the same things) is a 409 in both, unless ``force``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ais import audit
from ais.api_write import check_export, review_keys
from ais.auth import Principal, require
from ais.db import get_session
from ais.locking import IfMatch, check_precondition, etag
from ais.models import Role
from ais.resolver import current_revision
from ais.revert import RevertError, revert, revisions_of_change

router = APIRouter(prefix="/api/v1", tags=["revert"])
editor = Depends(require(Role.editor))


class RevertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    force: bool = Field(default=False, description="Revert even though later revisions changed the same things (refused with 409 otherwise)")
    dry_run: bool = Field(default=False, description="Report the inverse operations and the hosts whose vars would change; write nothing (no If-Match needed)")
    message: str | None = None


def _run(
    session: Session,
    response: Response,
    principal: Principal,
    if_match: str | None,
    req: RevertRequest,
    revisions: list[int],
    change: str | None = None,
) -> dict[str, Any]:
    if not req.dry_run:
        check_precondition(session, if_match)
    try:
        result = revert(
            session,
            revisions,
            actor=principal.actor,
            source=audit.current_source(),
            message=req.message,
            force=req.force,
            hosts=req.dry_run,
            change=change,
        )
    except RevertError as exc:
        raise HTTPException(exc.status, exc.detail) from exc
    check_export(session)
    body = {**result.body(), **review_keys(session, result.revision, strict=False)}  # an undo is never refused
    if req.dry_run:
        session.rollback()
        rev_now = current_revision(session)
        response.headers["ETag"] = etag(rev_now)
        return {"revision": rev_now, "changed": True, "dry_run": True, **body}
    response.headers["ETag"] = etag(result.revision.id)
    response.status_code = 201
    return {"revision": result.revision.id, "changed": True, "dry_run": False, **body}


@router.post("/revisions/{revision_id}/revert")
def revert_revision(
    revision_id: int,
    req: RevertRequest,
    response: Response,
    if_match: str | None = IfMatch(),
    principal: Principal = editor,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Undo one revision as one new revision (201): the inverse of each of its audit rows,
    newest first, each recorded as an ordinary audit row, plus one ``revert`` row. Refused:
    a revision with nothing recorded to invert or an older import whose row lacks what
    definitions and tags were (422); a later revision that changed the same node, key,
    definition or tag (409 with ``conflicts``, unless ``force``); an inverse that cannot
    apply now (409/422 saying what blocked it). ``dry_run`` reports and writes nothing."""
    return _run(session, response, principal, if_match, req, [revision_id])


@router.post("/changes/{change}/revert")
def revert_change(
    response: Response,
    req: RevertRequest,
    change: str = Path(max_length=64, description="A change id (X-AIS-Change), e.g. SRE-1234"),
    if_match: str | None = IfMatch(),
    principal: Principal = editor,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Undo every revision of a change id as one new revision, newest first. The revisions
    of the change never conflict with each other; any other later revision that touched the
    same things does (409 unless ``force``)."""
    revisions = revisions_of_change(session, change)
    if not revisions:
        raise HTTPException(404, f"no revision carries change id {change!r}")
    return _run(session, response, principal, if_match, req, revisions, change=change)

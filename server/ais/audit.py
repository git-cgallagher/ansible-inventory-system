"""Audit log: ``record`` writes one ``audit_log`` row into the caller's session, so
the row commits or rolls back together with the change it describes. A refused
write (409/422/428 raised after the row was added) therefore leaves no trace; a
failed login, which must be recorded, returns its 401 as a response instead of
raising (see ``api_auth.login``).

``request_id`` comes from a context variable set by ``RequestIdMiddleware``: the
client's ``X-Request-ID`` header when present (clients such as the modules can
correlate), otherwise a fresh UUID. The id is echoed in the response header.
The same middleware reads ``X-AIS-Source: module|ui`` into ``source`` (default
``api``) so revisions and audit rows say which kind of client wrote. CLI writes
have no request id and ``source`` ``cli``.
"""

from __future__ import annotations

import contextvars
import datetime as dt
import uuid
from typing import Any

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.orm import Session

from ais.models import AuditLog

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("ais_request_id", default=None)
source_var: contextvars.ContextVar[str] = contextvars.ContextVar("ais_source", default="api")

REQUEST_ID_HEADER = b"x-request-id"
SOURCE_HEADER = b"x-ais-source"
SOURCES = ("api", "module", "ui")  # what a client may claim; import/login/cli are set server-side
_MAX_REQUEST_ID = 64


class RequestIdMiddleware:
    """Pure ASGI middleware: sets ``request_id_var`` for the request and adds
    ``X-Request-ID`` to the response."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = scope.get("headers", [])
        incoming = next((v for k, v in headers if k == REQUEST_ID_HEADER), None)
        rid = incoming.decode("latin-1")[:_MAX_REQUEST_ID] if incoming else uuid.uuid4().hex
        claimed = next((v for k, v in headers if k == SOURCE_HEADER), b"").decode("latin-1").lower()
        token = request_id_var.set(rid)
        source_token = source_var.set(claimed if claimed in SOURCES else "api")

        async def send_with_id(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((REQUEST_ID_HEADER, rid.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            request_id_var.reset(token)
            source_var.reset(source_token)


def current_request_id() -> str | None:
    return request_id_var.get()


def current_source() -> str:
    """``api`` unless the request carried ``X-AIS-Source: module|ui``."""
    return source_var.get()


def record(
    session: Session,
    *,
    actor: str,
    action: str,
    target_type: str,
    target_id: Any,
    before: Any = None,
    after: Any = None,
    source: str = "api",
    revision: int | None = None,
) -> AuditLog:
    """Add an audit row to ``session`` (flushed, not committed)."""
    row = AuditLog(
        actor=actor,
        action=action,
        target_type=target_type,
        target_id=str(target_id),
        before=jsonable_encoder(before) if before is not None else None,
        after=jsonable_encoder(after) if after is not None else None,
        source=source,
        request_id=current_request_id(),
        revision=revision,
    )
    session.add(row)
    session.flush()
    return row


def audit_out(row: AuditLog) -> dict[str, Any]:
    return {
        "id": row.id,
        "ts": row.ts,
        "actor": row.actor,
        "action": row.action,
        "target_type": row.target_type,
        "target_id": row.target_id,
        "before": row.before,
        "after": row.after,
        "source": row.source,
        "request_id": row.request_id,
        "revision": row.revision,
    }


def node_lineage(session: Session, node_id: int) -> list[int]:
    """Every id the node has had: ``node_id`` and, walking ``node.move`` rows backwards
    (their ``after.moved`` maps ``old_id`` → ``id`` for the whole moved subtree), the ids
    before each move of the node or of an ancestor. Moves are rare, so this reads them all."""
    previous: dict[int, int] = {}
    for after in session.scalars(select(AuditLog.after).where(AuditLog.action == "node.move")):
        for m in (after or {}).get("moved", []):
            if isinstance(m, dict) and "id" in m and "old_id" in m:
                previous[int(m["id"])] = int(m["old_id"])
    out = [node_id]
    while out[-1] in previous and previous[out[-1]] not in out:
        out.append(previous[out[-1]])
    return out


def as_utc(value: dt.datetime | None) -> dt.datetime | None:
    """Normalize a query datetime: naive means UTC. SQLite stores timestamps as
    naive UTC strings, PostgreSQL as timestamptz; an aware UTC bound value compares
    correctly on both."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)

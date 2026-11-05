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

``X-AIS-Change: <id>`` tags the request's writes with a change id (``SRE-1234``): the
revision and its audit rows record it, ``GET /revisions?change=`` and ``GET /audit?change=``
filter by it, and ``POST /changes/{id}/revert`` undoes every revision that carries it. The
middleware validates it (``CHANGE_RE``) and answers 400 for a bad one without echoing it, so
a pasted token never reaches a log line or an error message.
"""

from __future__ import annotations

import contextvars
import datetime as dt
import json
import re
import uuid
from typing import Any

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.orm import Session

from ais.models import AuditLog

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("ais_request_id", default=None)
source_var: contextvars.ContextVar[str] = contextvars.ContextVar("ais_source", default="api")
change_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("ais_change", default=None)

REQUEST_ID_HEADER = b"x-request-id"
SOURCE_HEADER = b"x-ais-source"
CHANGE_HEADER = b"x-ais-change"
SOURCES = ("api", "module", "ui")  # what a client may claim; import/login/cli are set server-side
_MAX_REQUEST_ID = 64

# A change id: a ticket or change number, not free text. 1-64 characters, starting with a
# letter or digit, then letters, digits and ``. _ : / # + @ -`` (``SRE-1234``,
# ``CHG0012345``, ``deploy/2026-11-05#3``). No spaces or quotes, so it is safe in a URL path,
# a header and a log line.
CHANGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#+@-]{0,63}$")
CHANGE_RULE = "1-64 characters: letters, digits and . _ : / # + @ - (starting with a letter or digit)"


def change_error(value: str) -> str | None:
    """Why ``value`` cannot be a change id, or None. The message never contains the value:
    it may be a secret pasted into the wrong field."""
    if not CHANGE_RE.match(value):
        return f"invalid change id: {CHANGE_RULE}"
    if value.lower().startswith("ais_"):
        return "invalid change id: it looks like an API token (ais_...); a change id names a ticket or change, such as SRE-1234"
    return None


def check_change(value: str | None) -> str | None:
    """``value`` stripped, or None when empty; raises ``ValueError`` (with ``change_error``)."""
    value = (value or "").strip()
    if not value:
        return None
    error = change_error(value)
    if error:
        raise ValueError(error)
    return value


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
        try:
            change = check_change(next((v for k, v in headers if k == CHANGE_HEADER), b"").decode("latin-1"))
        except ValueError as exc:
            body = json.dumps({"detail": {"error": "invalid change id", "message": str(exc)}}).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 400,
                    "headers": [(b"content-type", b"application/json"), (REQUEST_ID_HEADER, rid.encode("latin-1"))],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        token = request_id_var.set(rid)
        source_token = source_var.set(claimed if claimed in SOURCES else "api")
        change_token = change_var.set(change)

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
            change_var.reset(change_token)


def current_request_id() -> str | None:
    return request_id_var.get()


def current_source() -> str:
    """``api`` unless the request carried ``X-AIS-Source: module|ui``."""
    return source_var.get()


def current_change() -> str | None:
    """The request's ``X-AIS-Change`` (validated), or the CLI's ``--change``; None without."""
    return change_var.get()


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
    """Add an audit row to ``session`` (flushed, not committed). An inventory write's row
    (``revision`` set) carries the request's change id; auth events carry none."""
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
        change=current_change() if revision is not None else None,
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
        "change": row.change,
    }


# Audit actions whose ``after`` maps a whole subtree's old ids to new ones: a move or
# rename (``after.moved``) and a restore of a deleted subtree (``after.restored``).
_LINEAGE = {"node.move": "moved", "node.restore": "restored"}


def node_lineage(session: Session, node_id: int) -> list[int]:
    """Every id the node has had: ``node_id`` and, walking ``node.move`` and ``node.restore``
    rows backwards (their ``after.moved`` / ``after.restored`` map ``old_id`` → ``id`` for the
    whole subtree), the ids before each move of the node or of an ancestor and the ids of the
    deleted rows a restore brought back. Both are rare, so this reads them all."""
    previous: dict[int, int] = {}
    for action, after in session.execute(select(AuditLog.action, AuditLog.after).where(AuditLog.action.in_(list(_LINEAGE)))):
        for m in (after or {}).get(_LINEAGE[action], []):
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

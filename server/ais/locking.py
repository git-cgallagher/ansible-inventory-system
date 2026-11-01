"""Optimistic locking for writes.

Every write carries ``If-Match: "<revision>"`` (the inventory revision the client
based its change on). The server takes the global write lock, reads the current
revision and refuses with 409 when it moved. Missing header → 428.

The lock: on PostgreSQL a transaction-scoped advisory lock, so concurrent writers
serialize *before* reading the revision. On SQLite a no-op UPDATE on
``alembic_version`` acquires the database write lock for the same effect (a second
writer waits on the busy timeout, then sees the new revision and gets its 409).
"""

from __future__ import annotations

import re

from fastapi import Header, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from ais.models import Revision
from ais.resolver import current_revision

ADVISORY_LOCK_KEY = 0x41495331  # "AIS1"
_ETAG = re.compile(r'^\s*(?:W/)?"?(\d+)"?\s*$')


def parse_if_match(value: str | None) -> int | None:
    if value is None:
        return None
    m = _ETAG.match(value)
    if not m:
        raise HTTPException(400, f"If-Match must be a revision number, got {value!r}")
    return int(m.group(1))


def etag(revision: int) -> str:
    return f'"{revision}"'


def lock_for_write(session: Session) -> None:
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": ADVISORY_LOCK_KEY})
    elif dialect == "sqlite":
        session.execute(text("UPDATE alembic_version SET version_num = version_num"))


def check_precondition(session: Session, if_match: str | None) -> int:
    """Lock, then verify ``If-Match`` against the current revision (returned)."""
    expected = parse_if_match(if_match)
    if expected is None:
        raise HTTPException(428, 'If-Match: "<revision>" is required on writes')
    lock_for_write(session)
    current = current_revision(session)
    if expected != current:
        raise HTTPException(
            409,
            {"error": "stale revision", "expected": expected, "current": current},
            headers={"ETag": etag(current)},
        )
    return current


def begin_write(session: Session, if_match: str | None, *, actor: str, source: str = "api", message: str | None = None) -> Revision:
    """Lock, check the precondition, open a new revision. Call first in a write."""
    check_precondition(session, if_match)
    rev = Revision(actor=actor, source=source, message=message)
    session.add(rev)
    session.flush()
    return rev


def IfMatch():  # noqa: N802 - dependency marker reads like a header
    return Header(default=None, alias="If-Match", description='Revision the change is based on, e.g. "12"')

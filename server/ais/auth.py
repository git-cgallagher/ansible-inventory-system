"""Authentication and authorization.

* Local users with scrypt password hashes (stdlib ``hashlib.scrypt``, RFC 7914; no
  C extension needed). ``password_hash`` is NULL for token-only service users.
* Bearer tokens (``ais_…``), random 256-bit secrets stored as their SHA-256. A token
  is either ``personal`` (long-lived, created via API/CLI) or ``session`` (issued by
  ``POST /api/v1/auth/login``, short-lived).
* Roles ``viewer < editor < admin``. A token may cap the user's role.
* ``require(role)`` is the FastAPI dependency every ``/api/v1`` route uses.

OIDC lives in ``ais.oidc`` behind ``AIS_OIDC_*`` and is a stub until configured.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from ais.db import get_session
from ais.models import ROLE_RANK, ApiToken, Role, User

TOKEN_PREFIX = "ais_"
_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}
_LAST_USED_GRANULARITY = dt.timedelta(minutes=1)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _aware(value: dt.datetime | None) -> dt.datetime | None:
    """SQLite returns naive datetimes; treat them as UTC."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value


# ------------------------------------------------------------------ passwords


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)
    return "scrypt$%d$%d$%d$%s$%s" % (
        _SCRYPT["n"],
        _SCRYPT["r"],
        _SCRYPT["p"],
        base64.b64encode(salt).decode(),
        base64.b64encode(dk).decode(),
    )


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algo, n, r, p, salt_b64, dk_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
        dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, expected)


# --------------------------------------------------------------------- tokens


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_token(
    session: Session,
    user: User,
    *,
    name: str = "token",
    kind: str = "personal",
    role: Role | None = None,
    expires_at: dt.datetime | None = None,
) -> tuple[str, ApiToken]:
    """Mint a token; the plaintext is returned once and never stored."""
    if role is not None and ROLE_RANK[role] > ROLE_RANK[user.role]:
        raise ValueError("token role cannot exceed the user's role")
    plaintext = TOKEN_PREFIX + secrets.token_urlsafe(32)
    row = ApiToken(
        user_id=user.id,
        name=name,
        token_hash=hash_token(plaintext),
        prefix=plaintext[: len(TOKEN_PREFIX) + 6],
        kind=kind,
        role=role,
        expires_at=expires_at,
    )
    session.add(row)
    session.flush()
    return plaintext, row


def session_ttl() -> dt.timedelta:
    return dt.timedelta(hours=float(os.environ.get("AIS_SESSION_TTL_HOURS", "12")))


# ------------------------------------------------------------------ principal


@dataclass
class Principal:
    user: User
    token: ApiToken | None
    role: Role

    @property
    def actor(self) -> str:
        return self.user.email

    def has(self, role: Role) -> bool:
        return ROLE_RANK[self.role] >= ROLE_RANK[role]


def authenticate_token(session: Session, token: str) -> Principal | None:
    row = session.scalar(select(ApiToken).where(ApiToken.token_hash == hash_token(token)))
    if row is None or row.revoked_at is not None:
        return None
    now = _now()
    expires = _aware(row.expires_at)
    if expires is not None and expires <= now:
        return None
    user = session.get(User, row.user_id)
    if user is None or not user.is_active:
        return None
    last = _aware(row.last_used)
    if last is None or now - last > _LAST_USED_GRANULARITY:
        row.last_used = now
    role = user.role
    if row.role is not None and ROLE_RANK[row.role] < ROLE_RANK[role]:
        role = row.role
    return Principal(user=user, token=row, role=role)


def authenticate_password(session: Session, email: str, password: str) -> User | None:
    user = session.scalar(select(User).where(User.email == email.strip().lower()))
    if user is None or not user.is_active or user.auth_source != "local":
        # still burn a hash so timing does not reveal whether the user exists
        verify_password(password, hash_password("x"))
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


_bearer = HTTPBearer(auto_error=False)


def current_principal(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: Session = Depends(get_session),
) -> Principal:
    if creds is None or creds.scheme.lower() != "bearer" or not creds.credentials:
        raise HTTPException(401, "bearer token required", headers={"WWW-Authenticate": "Bearer"})
    principal = authenticate_token(session, creds.credentials)
    if principal is None:
        raise HTTPException(401, "invalid, expired or revoked token", headers={"WWW-Authenticate": "Bearer"})
    request.state.principal = principal
    return principal


def require(role: Role):
    """Dependency factory: ``Depends(require(Role.editor))``."""

    def _dep(principal: Principal = Depends(current_principal)) -> Principal:
        if not principal.has(role):
            raise HTTPException(403, f"{role.value} role required")
        return principal

    return _dep


# --------------------------------------------------------------------- users


def create_user(
    session: Session,
    email: str,
    *,
    role: Role = Role.viewer,
    password: str | None = None,
    display_name: str | None = None,
    auth_source: str = "local",
) -> User:
    email = email.strip().lower()
    if session.scalar(select(User).where(User.email == email)) is not None:
        raise ValueError(f"user {email!r} already exists")
    user = User(
        email=email,
        display_name=display_name,
        role=role,
        auth_source=auth_source,
        password_hash=hash_password(password) if password else None,
    )
    session.add(user)
    session.flush()
    return user

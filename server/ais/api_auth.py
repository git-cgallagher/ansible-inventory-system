"""Auth, token and user routes (``/api/v1/auth``, ``/api/v1/tokens``, ``/api/v1/users``)."""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ais import audit, auth
from ais.auth import Principal, current_principal, require
from ais.db import get_session
from ais.models import ApiToken, Role, User
from ais.ratelimit import login_limiter

router = APIRouter(prefix="/api/v1", tags=["auth"])


def _token_out(t: ApiToken) -> dict[str, Any]:
    return {
        "id": t.id,
        "user_id": t.user_id,
        "name": t.name,
        "prefix": t.prefix,
        "kind": t.kind,
        "role": t.role.value if t.role else None,
        "expires_at": t.expires_at,
        "last_used": t.last_used,
        "revoked_at": t.revoked_at,
        "created_at": t.created_at,
    }


def _user_out(u: User) -> dict[str, Any]:
    return {
        "id": u.id,
        "email": u.email,
        "display_name": u.display_name,
        "auth_source": u.auth_source,
        "role": u.role.value,
        "is_active": u.is_active,
        "created_at": u.created_at,
    }


# ---------------------------------------------------------------------- login


class LoginRequest(BaseModel):
    email: str
    password: str


@router.post("/auth/login")
def login(req: LoginRequest, request: Request, session: Session = Depends(get_session)) -> Any:
    email = req.email.strip().lower()
    ip = request.client.host if request.client else "unknown"
    wait = login_limiter.retry_after(email, ip)
    if wait is not None:
        # Refused before the password is checked; returned (not raised) so the row commits.
        audit.record(session, actor=email, action="login.throttled", target_type="user", target_id=email, after={"client": ip, "retry_after": wait}, source="login")
        return JSONResponse(status_code=429, content={"detail": f"too many failed logins, retry in {wait} s"}, headers={"Retry-After": str(wait)})
    user = auth.authenticate_password(session, email, req.password)
    if user is None:
        login_limiter.failure(email, ip)
        # Returned, not raised: raising would roll back the session and lose the audit row.
        audit.record(session, actor=email, action="login.failure", target_type="user", target_id=email, after={"client": ip}, source="login")
        return JSONResponse(status_code=401, content={"detail": "invalid credentials"})
    login_limiter.success(email)
    expires = auth._now() + auth.session_ttl()
    plaintext, row = auth.create_token(session, user, name="login session", kind="session", expires_at=expires)
    audit.record(session, actor=user.email, action="login.success", target_type="user", target_id=user.email, after={"token_id": row.id, "expires_at": row.expires_at}, source="login")
    return {"token": plaintext, "expires_at": row.expires_at, "role": user.role.value, "email": user.email}


@router.post("/auth/logout", status_code=204)
def logout(principal: Principal = Depends(current_principal), session: Session = Depends(get_session)) -> None:
    if principal.token is not None and principal.token.revoked_at is None:
        principal.token.revoked_at = auth._now()
        audit.record(session, actor=principal.actor, action="logout", target_type="token", target_id=principal.token.id, before=_token_out(principal.token), source="login")


@router.get("/auth/me")
def me(principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    return {"user": _user_out(principal.user), "role": principal.role.value, "token": _token_out(principal.token) if principal.token else None}


# --------------------------------------------------------------------- tokens


class TokenCreate(BaseModel):
    name: str = Field(default="token", max_length=255)
    expires_days: int | None = Field(default=None, ge=1, le=3650)
    role: Role | None = Field(default=None, description="Cap this token below the user's role")
    email: str | None = Field(default=None, description="Admin only: mint for another user")


@router.post("/tokens", status_code=201)
def create_token(req: TokenCreate, principal: Principal = Depends(current_principal), session: Session = Depends(get_session)) -> dict[str, Any]:
    user = principal.user
    if req.email and req.email.strip().lower() != user.email:
        if not principal.has(Role.admin):
            raise HTTPException(403, "admin role required to mint tokens for other users")
        user = session.scalar(select(User).where(User.email == req.email.strip().lower()))
        if user is None:
            raise HTTPException(404, "unknown user")
    expires = auth._now() + dt.timedelta(days=req.expires_days) if req.expires_days else None
    try:
        plaintext, row = auth.create_token(session, user, name=req.name, role=req.role, expires_at=expires)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    audit.record(session, actor=principal.actor, action="token.create", target_type="token", target_id=row.id, after=_token_out(row))
    return {"token": plaintext, **_token_out(row)}


@router.get("/tokens")
def list_tokens(all: bool = False, principal: Principal = Depends(current_principal), session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    stmt = select(ApiToken).order_by(ApiToken.id)
    if all:
        if not principal.has(Role.admin):
            raise HTTPException(403, "admin role required")
    else:
        stmt = stmt.where(ApiToken.user_id == principal.user.id)
    return [_token_out(t) for t in session.scalars(stmt)]


@router.delete("/tokens/{token_id}", status_code=204)
def revoke_token(token_id: int, principal: Principal = Depends(current_principal), session: Session = Depends(get_session)) -> None:
    row = session.get(ApiToken, token_id)
    if row is None or (row.user_id != principal.user.id and not principal.has(Role.admin)):
        raise HTTPException(404, "unknown token")
    if row.revoked_at is None:
        before = _token_out(row)
        row.revoked_at = auth._now()
        audit.record(session, actor=principal.actor, action="token.revoke", target_type="token", target_id=row.id, before=before, after=_token_out(row))


# ---------------------------------------------------------------------- users


class UserCreate(BaseModel):
    email: EmailStr
    role: Role = Role.viewer
    password: str | None = Field(default=None, min_length=8)
    display_name: str | None = None


class UserPatch(BaseModel):
    role: Role | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=8)
    display_name: str | None = None


@router.get("/users", dependencies=[Depends(require(Role.admin))])
def list_users(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
    return [_user_out(u) for u in session.scalars(select(User).order_by(User.id))]


@router.post("/users", status_code=201)
def create_user(req: UserCreate, principal: Principal = Depends(require(Role.admin)), session: Session = Depends(get_session)) -> dict[str, Any]:
    try:
        user = auth.create_user(session, str(req.email), role=req.role, password=req.password, display_name=req.display_name)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    audit.record(session, actor=principal.actor, action="user.create", target_type="user", target_id=user.email, after=_user_out(user))
    return _user_out(user)


@router.patch("/users/{user_id}")
def patch_user(user_id: int, req: UserPatch, principal: Principal = Depends(require(Role.admin)), session: Session = Depends(get_session)) -> dict[str, Any]:
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(404, "unknown user")
    before = _user_out(user)
    if req.role is not None:
        user.role = req.role
    if req.is_active is not None:
        user.is_active = req.is_active
    if req.password is not None:
        user.password_hash = auth.hash_password(req.password)
    if req.display_name is not None:
        user.display_name = req.display_name
    session.flush()
    after = {**_user_out(user), "password_changed": req.password is not None}
    audit.record(session, actor=principal.actor, action="user.update", target_type="user", target_id=user.email, before=before, after=after)
    return _user_out(user)

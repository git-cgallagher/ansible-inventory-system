"""OIDC login (Authlib) behind a config flag. **Stub until configured and not yet
exercised against a real provider.**

Set all of ``AIS_OIDC_ISSUER`` (the issuer URL; its
``/.well-known/openid-configuration`` is fetched), ``AIS_OIDC_CLIENT_ID``,
``AIS_OIDC_CLIENT_SECRET`` and ``AIS_SECRET_KEY`` (signs the login-flow session
cookie) to enable ``GET /api/v1/auth/oidc/login`` → provider → ``…/callback``, which
maps the ID token's ``email`` to a user (auto-provisioned with
``AIS_OIDC_DEFAULT_ROLE``, default ``viewer``, when ``AIS_OIDC_AUTO_PROVISION`` is
``1``) and answers with a session bearer token. Unconfigured, both routes answer 501.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ais import auth
from ais.db import get_session
from ais.models import Role, User

router = APIRouter(prefix="/api/v1/auth/oidc", tags=["auth"])

_REQUIRED = ("AIS_OIDC_ISSUER", "AIS_OIDC_CLIENT_ID", "AIS_OIDC_CLIENT_SECRET", "AIS_SECRET_KEY")


def configured() -> bool:
    return all(os.environ.get(k) for k in _REQUIRED)


_oauth = None


def _client():
    global _oauth
    if _oauth is None:
        try:
            from authlib.integrations.starlette_client import OAuth
        except ImportError as exc:  # pragma: no cover - optional extra
            raise HTTPException(501, "OIDC needs the 'oidc' extra: pip install 'ansible-inventory-system[oidc]'") from exc
        _oauth = OAuth()
        _oauth.register(
            "oidc",
            client_id=os.environ["AIS_OIDC_CLIENT_ID"],
            client_secret=os.environ["AIS_OIDC_CLIENT_SECRET"],
            server_metadata_url=os.environ["AIS_OIDC_ISSUER"].rstrip("/") + "/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )
    return _oauth.oidc


def _not_configured() -> None:
    if not configured():
        raise HTTPException(501, "OIDC is not configured (set AIS_OIDC_ISSUER, AIS_OIDC_CLIENT_ID, AIS_OIDC_CLIENT_SECRET, AIS_SECRET_KEY)")


@router.get("/login")
async def oidc_login(request: Request):
    _not_configured()
    redirect_uri = os.environ.get("AIS_OIDC_REDIRECT_URL") or str(request.url_for("oidc_callback"))
    return await _client().authorize_redirect(request, redirect_uri)


@router.get("/callback", name="oidc_callback")
async def oidc_callback(request: Request, session: Session = Depends(get_session)):
    _not_configured()
    token = await _client().authorize_access_token(request)
    claims = token.get("userinfo") or {}
    email = (claims.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(401, "OIDC token carries no email claim")
    user = session.scalar(select(User).where(User.email == email))
    if user is None:
        if os.environ.get("AIS_OIDC_AUTO_PROVISION", "0") != "1":
            raise HTTPException(403, "unknown user; auto-provisioning is off")
        role = Role(os.environ.get("AIS_OIDC_DEFAULT_ROLE", "viewer"))
        user = auth.create_user(session, email, role=role, display_name=claims.get("name"), auth_source="oidc")
    if not user.is_active:
        raise HTTPException(403, "user is disabled")
    plaintext, row = auth.create_token(session, user, name="oidc session", kind="session", expires_at=auth._now() + auth.session_ttl())
    return {"token": plaintext, "expires_at": row.expires_at, "role": user.role.value, "email": user.email}

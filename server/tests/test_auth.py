"""401 without/with a bad token, 403 below the required role, login, token lifecycle."""

from __future__ import annotations

import datetime as dt

from ais import auth
from ais.models import Role
from tests.conftest import AuthClient, current_rev


def test_anonymous_is_401(anon, seeded):
    for url in ("/api/v1/export", "/api/v1/nodes", "/api/v1/revisions", "/api/v1/auth/me"):
        r = anon.get(url)
        assert r.status_code == 401, url
        assert r.headers["WWW-Authenticate"] == "Bearer"
    assert anon.post("/api/v1/render", json={"host": "web01"}).status_code == 401
    assert anon.get("/healthz").status_code == 200  # the only open route


def test_garbage_token_is_401(engine, seeded):
    assert AuthClient(engine and __import__("ais.api", fromlist=["app"]).app, "ais_nope").get("/api/v1/export").status_code == 401


def test_viewer_reads_but_cannot_write(viewer, seeded):
    assert viewer.get("/api/v1/export").status_code == 200
    assert viewer.post("/api/v1/render", json={"host": "web01"}).status_code == 200
    rev = current_rev(viewer)
    r = viewer.post("/api/v1/nodes", json={"parent_path": "all", "name": "x"}, if_match=rev)
    assert r.status_code == 403
    assert viewer.put("/api/v1/variables/foo", json={"merge_strategy": "merge"}, if_match=rev).status_code == 403
    assert viewer.get("/api/v1/users").status_code == 403


def test_editor_cannot_manage_users(editor, seeded):
    assert editor.get("/api/v1/users").status_code == 403
    assert editor.post("/api/v1/users", json={"email": "x@example.com"}).status_code == 403


def test_login_and_session_token(admin, anon, session):
    r = anon.post("/api/v1/auth/login", json={"email": "admin@example.com", "password": "admin-pass-example"})
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "admin" and body["token"].startswith("ais_")
    me = AuthClient(anon.app, body["token"]).get("/api/v1/auth/me").json()
    assert me["user"]["email"] == "admin@example.com" and me["token"]["kind"] == "session"
    assert anon.post("/api/v1/auth/login", json={"email": "admin@example.com", "password": "wrong"}).status_code == 401
    assert anon.post("/api/v1/auth/login", json={"email": "nobody@example.com", "password": "x"}).status_code == 401


def test_token_lifecycle_and_role_cap(admin, anon, session):
    r = admin.post("/api/v1/tokens", json={"name": "ci", "role": "viewer", "expires_days": 1})
    assert r.status_code == 201
    tok = r.json()
    capped = AuthClient(anon.app, tok["token"])
    assert capped.get("/api/v1/auth/me").json()["role"] == "viewer"
    assert capped.get("/api/v1/users").status_code == 403
    assert admin.post("/api/v1/tokens", json={"name": "too-much", "role": "admin", "email": "admin@example.com"}).status_code == 201
    # revoke
    assert admin.delete(f"/api/v1/tokens/{tok['id']}").status_code == 204
    assert capped.get("/api/v1/auth/me").status_code == 401
    # logout revokes the session token
    login = anon.post("/api/v1/auth/login", json={"email": "admin@example.com", "password": "admin-pass-example"}).json()
    c = AuthClient(anon.app, login["token"])
    assert c.post("/api/v1/auth/logout").status_code == 204
    assert c.get("/api/v1/auth/me").status_code == 401


def test_expired_and_disabled(admin, anon, session):
    u = auth.create_user(session, "temp@example.com", role=Role.editor)
    past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    expired, _ = auth.create_token(session, u, expires_at=past)
    live, _ = auth.create_token(session, u)
    session.commit()
    assert AuthClient(anon.app, expired).get("/api/v1/auth/me").status_code == 401
    assert AuthClient(anon.app, live).get("/api/v1/auth/me").status_code == 200
    users = {x["email"]: x for x in admin.get("/api/v1/users").json()}
    assert admin.patch(f"/api/v1/users/{users['temp@example.com']['id']}", json={"is_active": False}).status_code == 200
    assert AuthClient(anon.app, live).get("/api/v1/auth/me").status_code == 401


def test_admin_creates_users_and_token_for_them(admin, anon, session):
    r = admin.post("/api/v1/users", json={"email": "svc@example.com", "role": "editor"})
    assert r.status_code == 201
    assert admin.post("/api/v1/users", json={"email": "svc@example.com", "role": "editor"}).status_code == 409
    r = admin.post("/api/v1/tokens", json={"name": "svc", "email": "svc@example.com"})
    assert r.status_code == 201
    assert AuthClient(anon.app, r.json()["token"]).get("/api/v1/auth/me").json()["role"] == "editor"


def test_password_hashing_roundtrip():
    h = auth.hash_password("correct horse")
    assert h.startswith("scrypt$") and auth.verify_password("correct horse", h)
    assert not auth.verify_password("wrong", h)
    assert not auth.verify_password("x", None)
    assert not auth.verify_password("x", "garbage")

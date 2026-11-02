"""401 without/with a bad token, 403 below the required role, login, token lifecycle."""

from __future__ import annotations

import datetime as dt

from ais import auth
from ais.api import app
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
    assert AuthClient(app, "ais_nope").get("/api/v1/export").status_code == 401


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


def test_bad_tokens_are_audited_without_the_secret(admin, anon, session, seeded):
    from sqlalchemy import select

    from ais.models import AuditLog

    assert anon.get("/api/v1/export").status_code == 401  # no token at all: not recorded
    assert AuthClient(app, "ais_nopenopenope").get("/api/v1/export").status_code == 401
    assert AuthClient(app, "hunter2-password").get("/api/v1/nodes").status_code == 401
    minted = admin.post("/api/v1/tokens", json={"name": "short"}).json()
    assert admin.delete(f"/api/v1/tokens/{minted['id']}").status_code == 204
    assert AuthClient(app, minted["token"]).get("/api/v1/export").status_code == 401
    rows = session.scalars(select(AuditLog).where(AuditLog.action == "token.rejected").order_by(AuditLog.id)).all()
    assert [(r.actor, r.target_id, r.after["reason"]) for r in rows] == [
        ("anonymous", "ais_nopeno", "unknown"),
        ("anonymous", "(not an ais_ token)", "unknown"),
        ("admin@example.com", str(minted["id"]), "revoked"),
    ]
    assert rows[0].after["path"] == "/api/v1/export" and rows[0].after["method"] == "GET"
    dumped = repr([(r.target_id, r.before, r.after) for r in rows])
    assert "hunter2" not in dumped and minted["token"] not in dumped


def test_login_rate_limit(admin, anon, session, monkeypatch):
    from sqlalchemy import select

    from ais.models import AuditLog

    monkeypatch.setenv("AIS_LOGIN_MAX_FAILURES", "3")
    good = {"email": "admin@example.com", "password": "admin-pass-example"}
    bad = {"email": "admin@example.com", "password": "wrong"}
    for _ in range(3):
        assert anon.post("/api/v1/auth/login", json=bad).status_code == 401
    r = anon.post("/api/v1/auth/login", json=good)  # even the right password waits
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
    actions = [a.action for a in session.scalars(select(AuditLog).where(AuditLog.action.like("login.%")).order_by(AuditLog.id))]
    assert actions == ["login.failure"] * 3 + ["login.throttled"]
    # another e-mail from the same client is still allowed (per-address limit is 20)
    assert anon.post("/api/v1/auth/login", json={"email": "other@example.com", "password": "x"}).status_code == 401
    # the window passing lets the user in, and success clears the e-mail's count
    from ais.ratelimit import login_limiter

    login_limiter.reset()
    assert anon.post("/api/v1/auth/login", json=good).status_code == 200
    monkeypatch.setenv("AIS_LOGIN_MAX_FAILURES_PER_IP", "2")
    monkeypatch.setenv("AIS_LOGIN_MAX_FAILURES", "0")  # 0 = off
    for email in ("a@example.com", "b@example.com"):
        assert anon.post("/api/v1/auth/login", json={"email": email, "password": "x"}).status_code == 401
    assert anon.post("/api/v1/auth/login", json={"email": "c@example.com", "password": "x"}).status_code == 429

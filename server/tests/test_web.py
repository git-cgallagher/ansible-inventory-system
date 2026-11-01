"""The web UI is served as static files at ``/`` without touching any API route."""

from __future__ import annotations

import json

import pytest

from ais import oidc


@pytest.fixture()
def built_ui(tmp_path, monkeypatch):
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<!doctype html><title>ui</title>")
    (tmp_path / "assets" / "index-abc123.js").write_text("console.log('ui')")
    (tmp_path / "secret.txt").write_text("not an asset")
    monkeypatch.setenv("AIS_WEB_DIR", str(tmp_path))
    return tmp_path


def test_index_and_assets_are_served(anon, built_ui):
    r = anon.get("/")
    assert r.status_code == 200
    assert "<title>ui</title>" in r.text
    assert r.headers["cache-control"] == "no-cache"
    a = anon.get("/assets/index-abc123.js")
    assert a.status_code == 200
    assert "javascript" in a.headers["content-type"]
    assert "immutable" in a.headers["cache-control"]


def test_api_routes_are_untouched(anon, built_ui):
    assert anon.get("/api/v1/nodes").status_code == 401  # still the API, never index.html
    assert anon.get("/api/v1/nowhere").status_code in (401, 404)
    assert "html" not in anon.get("/api/v1/nowhere").headers.get("content-type", "")
    h = anon.get("/healthz")
    assert h.status_code == 200 and h.json()["status"] == "ok" and h.json()["oidc"] is False
    assert anon.get("/docs").status_code == 200


def test_asset_paths_cannot_escape(anon, built_ui):
    assert anon.get("/assets/../secret.txt").status_code == 404
    assert anon.get("/assets/%2e%2e/secret.txt").status_code == 404
    assert anon.get("/assets/missing.js").status_code == 404
    # nested and encoded-separator names never match a flat asset name
    assert anon.get("/assets/sub/index-abc123.js").status_code == 404
    assert anon.get("/assets/%2Fetc%2Fpasswd").status_code == 404


def test_missing_build_is_a_404_not_a_crash(anon, tmp_path, monkeypatch):
    monkeypatch.setenv("AIS_WEB_DIR", str(tmp_path / "nothing"))
    r = anon.get("/")
    assert r.status_code == 404 and "npm run build" in r.json()["detail"]
    assert anon.get("/healthz").status_code == 200


def test_oidc_ui_handoff_escapes_the_session():
    page = oidc.ui_handoff({"token": "ais_example-not-secret", "email": "</script><b>x@example.com", "role": "viewer"}).body.decode()
    assert "</script><b>" not in page
    payload = page.split("JSON.stringify(", 1)[1].split("));location", 1)[0]
    assert json.loads(payload)["email"] == "</script><b>x@example.com"
    assert "location.replace('/')" in page

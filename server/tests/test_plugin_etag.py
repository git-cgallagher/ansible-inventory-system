"""Server ETag / If-None-Match → 304, and the plugin's revalidating fetch."""

from __future__ import annotations

import importlib.util
import json
import pathlib

import httpx

from tests.conftest import COLLECTION_PATH, current_rev

PLUGIN = COLLECTION_PATH / "ansible_collections" / "git_cgallagher" / "inventory" / "plugins" / "inventory" / "db.py"


def _plugin_module():
    spec = importlib.util.spec_from_file_location("ais_plugin_db", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_export_304_on_matching_etag(viewer, editor, seeded):
    r = viewer.get("/api/v1/export")
    tag = r.headers["ETag"]
    assert tag.startswith(f'"{seeded.id}.')
    r2 = viewer.get("/api/v1/export", headers={"If-None-Match": tag})
    assert r2.status_code == 304 and r2.headers["ETag"] == tag and r2.content == b""
    # a different parameter set has a different ETag even at the same revision
    sub = viewer.get("/api/v1/export", params={"subtree": "all.federal"})
    assert sub.headers["ETag"] != tag
    assert viewer.get("/api/v1/export", headers={"If-None-Match": tag}, params={"subtree": "all.federal"}).status_code == 200
    # a write moves the revision and invalidates the tag
    root = next(n["id"] for n in editor.get("/api/v1/nodes").json() if n["path"] == "all")
    editor.put(f"/api/v1/nodes/{root}/vars/ntp_server", json={"value": "ntp3.example.com"}, if_match=current_rev(editor))
    r3 = viewer.get("/api/v1/export", headers={"If-None-Match": tag})
    assert r3.status_code == 200 and r3.headers["ETag"] != tag
    assert r3.json()["groups"]["all"]["vars"]["ntp_server"] == "ntp3.example.com"


def test_plugin_fetch_revalidates(server, viewer_token, tmp_path, editor):
    mod = _plugin_module()
    url = f"{server}/api/v1/export?group_separator=_&emit_tags=true"
    headers = {"Accept": "application/json", "Authorization": f"Bearer {viewer_token}"}
    store = mod.etag_store_path(str(tmp_path / "store"), url)
    data, revalidated = mod.fetch_export(url, headers, store_path=store)
    assert revalidated is False and "web01" in data["hosts"]
    saved = json.loads(pathlib.Path(store).read_text())
    assert saved["etag"] and saved["data"] == data
    data2, revalidated = mod.fetch_export(url, headers, store_path=store)
    assert revalidated is True and data2 == data
    # after a change the server answers 200 with the new export and the store is refreshed
    root = next(n["id"] for n in editor.get("/api/v1/nodes").json() if n["path"] == "all")
    editor.put(f"/api/v1/nodes/{root}/vars/ntp_server", json={"value": "ntp4.example.com"}, if_match=current_rev(editor))
    data3, revalidated = mod.fetch_export(url, headers, store_path=store)
    assert revalidated is False and data3["groups"]["all"]["vars"]["ntp_server"] == "ntp4.example.com"
    assert json.loads(pathlib.Path(store).read_text())["etag"] != saved["etag"]
    # no store → always a full fetch; a bad token → HTTPError 401 surfaces to the caller
    assert mod.fetch_export(url, headers)[1] is False
    try:
        mod.fetch_export(url, {"Authorization": "Bearer ais_bad"})
    except mod.HTTPError as exc:
        assert exc.code == 401
    else:
        raise AssertionError("expected 401")
    assert mod.etag_store_path("", url) is None


def test_plugin_end_to_end_uses_304(server, inventory_file, tmp_path):
    """Two ansible-inventory runs: the second revalidates against the store set up by the first."""
    from tests.conftest import ansible_inventory

    first = ansible_inventory(inventory_file, "--list")
    store_dir = pathlib.Path(__import__("os").environ["AIS_ETAG_STORE"])
    files = list(store_dir.glob("export-*.json"))
    assert len(files) == 1
    etag = json.loads(files[0].read_text())["etag"]
    second = ansible_inventory(inventory_file, "--list")
    assert second == first
    assert json.loads(files[0].read_text())["etag"] == etag
    # and the server really does answer 304 for that tag
    r = httpx.get(f"{server}/api/v1/export", params={"group_separator": "_", "emit_tags": "true"}, headers={"Authorization": f"Bearer {__import__('os').environ['AIS_TOKEN']}", "If-None-Match": etag})
    assert r.status_code == 304

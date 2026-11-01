# -*- coding: utf-8 -*-
# Copyright (c) 2026 Chris Gallagher
# MIT License (see LICENSE)
from __future__ import annotations

DOCUMENTATION = r"""
name: db
short_description: Ansible Inventory System (database-backed strict tree)
version_added: "0.1.0"
author: Chris Gallagher (@git-cgallagher)
description:
  - Builds the inventory from the Ansible Inventory System export API.
  - The server emits the real group tree with variables at each node; Ansible merges
    them natively (ancestors first by depth). Variables declared merge/append are
    pre-folded by the server so plain replace yields the right value.
  - Sets C(inventory_revision) on C(all) so every run records what it ran against.
  - "Honours the export's C(ETag): the last export is kept in O(etag_store) and re-sent
    as C(If-None-Match), so an unchanged inventory costs one cheap 304 instead of a
    full download (independent of, and complementary to, Ansible's inventory cache)."
options:
  plugin:
    description: Token that ensures this is a source file for the plugin.
    required: true
    choices: ["git_cgallagher.inventory.db"]
  url:
    description: Base URL of the server.
    required: true
    env:
      - name: AIS_URL
  token:
    description: Bearer token (required by the server; mint one with C(ais token create)).
    type: str
    env:
      - name: AIS_TOKEN
  etag_store:
    description:
      - Directory where the last export and its ETag are kept per source, for
        C(If-None-Match) revalidation. Empty string disables it.
    type: path
    default: ~/.cache/ais
    env:
      - name: AIS_ETAG_STORE
  subtree:
    description: Only export this node and what is below it, e.g. C(all.commercial.prod).
    type: str
  revision:
    description: Pin the export to an inventory revision.
    type: int
  group_separator:
    description: Separator used to build qualified group names from the path.
    type: str
    default: "_"
  emit_tags:
    description: Emit var-less tag groups (C(tier_prod), C(datacenter_dc1), ...) for targeting.
    type: bool
    default: true
  timeout:
    description: HTTP timeout in seconds.
    type: int
    default: 30
  validate_certs:
    description: Validate the server's TLS certificate.
    type: bool
    default: true
extends_documentation_fragment:
  - inventory_cache
"""

EXAMPLES = r"""
plugin: git_cgallagher.inventory.db
url: http://127.0.0.1:8000
subtree: all.commercial.prod
cache: true
cache_plugin: jsonfile
cache_connection: ~/.cache/ais
"""

import hashlib
import json
import os
import tempfile
from urllib.error import HTTPError
from urllib.parse import urlencode

from ansible.errors import AnsibleError
from ansible.module_utils.urls import open_url
from ansible.plugins.inventory import BaseInventoryPlugin, Cacheable


def etag_store_path(store_dir, url):
    """One file per distinct export URL (host, params) inside ``store_dir``."""
    if not store_dir:
        return None
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return os.path.join(os.path.expanduser(store_dir), "export-%s.json" % digest)


def fetch_export(url, headers, timeout=30, validate_certs=True, store_path=None):
    """GET the export, revalidating with ``If-None-Match`` when a stored copy exists.

    Returns ``(data, revalidated)``: ``revalidated`` is True when the server
    answered 304 and the stored copy was used.
    """
    headers = dict(headers)
    stored = None
    if store_path and os.path.exists(store_path):
        try:
            with open(store_path) as fh:
                stored = json.load(fh)
        except (OSError, ValueError):
            stored = None
        if stored and stored.get("etag") and stored.get("data") is not None:
            headers["If-None-Match"] = stored["etag"]
        else:
            stored = None
    try:
        resp = open_url(url, headers=headers, timeout=timeout, validate_certs=validate_certs)
    except HTTPError as exc:
        if exc.code == 304 and stored is not None:
            return stored["data"], True
        raise
    data = json.loads(resp.read())
    etag = resp.headers.get("ETag")
    if store_path and etag:
        try:
            os.makedirs(os.path.dirname(store_path), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(store_path), prefix=".etag-")
            with os.fdopen(fd, "w") as fh:
                json.dump({"etag": etag, "url": url, "data": data}, fh)
            os.replace(tmp, store_path)
        except OSError:
            pass  # the store is an optimisation; never fail the inventory over it
    return data, False


class InventoryModule(BaseInventoryPlugin, Cacheable):
    NAME = "git_cgallagher.inventory.db"

    def verify_file(self, path):
        return super().verify_file(path) and path.endswith(("ais.yml", "ais.yaml", "inventory.yml", "inventory.yaml"))

    def _fetch(self):
        params = {
            "group_separator": self.get_option("group_separator"),
            "emit_tags": "true" if self.get_option("emit_tags") else "false",
        }
        if self.get_option("subtree"):
            params["subtree"] = self.get_option("subtree")
        if self.get_option("revision") is not None:
            params["revision"] = self.get_option("revision")
        url = "%s/api/v1/export?%s" % (self.get_option("url").rstrip("/"), urlencode(params))
        headers = {"Accept": "application/json"}
        token = self.get_option("token")
        if token:
            headers["Authorization"] = "Bearer %s" % token
        try:
            data, revalidated = fetch_export(
                url,
                headers,
                timeout=self.get_option("timeout"),
                validate_certs=self.get_option("validate_certs"),
                store_path=etag_store_path(self.get_option("etag_store"), url),
            )
        except HTTPError as exc:
            if exc.code in (401, 403):
                raise AnsibleError("ais: %s rejected the token (%s); set AIS_TOKEN or the token option" % (url, exc.code))
            raise AnsibleError("ais: failed to fetch %s: %s" % (url, exc))
        except Exception as exc:  # noqa: BLE001 - surface as an inventory error
            raise AnsibleError("ais: failed to fetch %s: %s" % (url, exc))
        if revalidated:
            self.display.vv("ais: export unchanged (304), using the stored copy")
        return data

    def parse(self, inventory, loader, path, cache=True):
        super().parse(inventory, loader, path, cache)
        self._read_config_data(path)

        cache_key = self.get_cache_key(path)
        use_cache = self.get_option("cache") and cache
        data = None
        if use_cache:
            try:
                data = self._cache[cache_key]
            except KeyError:
                pass
        if data is None:
            data = self._fetch()
            if self.get_option("cache"):
                self._cache[cache_key] = data

        self._populate(data)

    def _populate(self, data):
        groups = data["groups"]
        for gname, g in groups.items():
            self.inventory.add_group(gname)
        for gname, g in groups.items():
            for child in g["children"]:
                self.inventory.add_child(gname, child)
            for host in g["hosts"]:
                self.inventory.add_host(host, group=gname)
            for k, v in g["vars"].items():
                self.inventory.set_variable(gname, k, v)
        for hname, h in data["hosts"].items():
            self.inventory.add_host(hname)
            for k, v in h["vars"].items():
                self.inventory.set_variable(hname, k, v)
        for tname, members in data.get("tags", {}).items():
            self.inventory.add_group(tname)
            for host in members:
                self.inventory.add_host(host, group=tname)
        self.inventory.set_variable("all", "inventory_revision", data["revision"])

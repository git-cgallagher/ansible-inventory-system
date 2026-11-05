# -*- coding: utf-8 -*-
# Copyright (c) 2026 Chris Gallagher
# MIT License (see LICENSE)
"""HTTP client shared by the git_cgallagher.inventory modules.

* Bearer token from the ``token`` option or ``AIS_TOKEN``; URL from ``url`` or ``AIS_URL``.
* Every write carries ``If-Match: "<revision>"``: the ``expect_revision`` option when given,
  otherwise the server's current revision read right before the first write; later writes in
  the same task follow the revision the previous write returned.
* A 409 whose body is the server's ``stale revision`` document is reported as such (expected
  vs current revision); any other 409 (name collision, duplicate host, a delete of a non-empty
  subtree without ``recursive``) carries the server's message (a structured detail's
  ``message``). 401/403 name the token; 422 and 404 carry the server's detail.
* Sends ``X-AIS-Source: module`` and one ``X-Request-ID`` per task so audit rows can be tied
  to the run, and ``X-AIS-Change`` from the ``change`` option (or ``AIS_CHANGE``) so the
  revisions of one change can be listed and reverted together.
* A write's ``warnings`` (undeclared keys that look like typos of known ones, server issue
  #24) become Ansible warnings (``module.warn``) and are kept in ``key_warnings`` for the
  module's result: Ansible reserves the result's ``warnings`` for plain strings.
* Check mode asks the server for a dry run of each write (``preview``): the server runs every
  check the write would (schema, strict keys, the delete guard, the export check) and answers
  with its ``impact`` on the hosts, writing nothing. The module still decides ``changed`` from
  what it read; ``impact_result``/``impact_diff`` put the previews into the result and into
  ``--diff`` (a ``prepared`` summary).
"""
from __future__ import annotations

import json
import os
import uuid

from ansible.module_utils.common.text.converters import to_native, to_text
from ansible.module_utils.urls import fetch_url

API = "/api/v1"


def client_argument_spec():
    """Options every module takes (doc fragment ``git_cgallagher.inventory.api``)."""
    return dict(
        url=dict(type="str"),
        token=dict(type="str", no_log=True),
        validate_certs=dict(type="bool", default=True),
        timeout=dict(type="int", default=30),
        change=dict(type="str"),
    )


def write_argument_spec():
    """Options the writing modules add (doc fragment ``git_cgallagher.inventory.write``)."""
    spec = client_argument_spec()
    spec.update(
        expect_revision=dict(type="int"),
        revision_message=dict(type="str"),
    )
    return spec


class ApiError(Exception):
    def __init__(self, status, detail, url):
        super(ApiError, self).__init__("%s -> %s: %s" % (url, status, detail))
        self.status = status
        self.detail = detail
        self.url = url


class Client(object):
    def __init__(self, module):
        self.module = module
        params = module.params
        self.base = (params.get("url") or os.environ.get("AIS_URL") or "").rstrip("/")
        self.token = params.get("token") or os.environ.get("AIS_TOKEN")
        self.timeout = params.get("timeout") or 30
        self.request_id = uuid.uuid4().hex
        self.revision = params.get("expect_revision")  # None: read lazily before the first write
        self.message = params.get("revision_message")
        self.change = params.get("change") or os.environ.get("AIS_CHANGE") or None
        self.key_warnings = []
        self.impacts = []  # the impact of each write check mode previewed
        if not self.base:
            module.fail_json(msg="url is required (option url or AIS_URL)")
        if not self.token:
            module.fail_json(msg="token is required (option token or AIS_TOKEN)")

    # ----------------------------------------------------------------- transport

    def _headers(self, if_match=None):
        headers = {
            "Authorization": "Bearer %s" % self.token,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-AIS-Source": "module",
            "X-Request-ID": self.request_id,
        }
        if if_match is not None:
            headers["If-Match"] = '"%d"' % if_match
        if self.change:
            headers["X-AIS-Change"] = self.change
        return headers

    def request(self, method, path, body=None, if_match=None, allow=(404,), keep_body=False):
        """Return ``(status, data)``. Non-2xx statuses not in ``allow`` fail the module; an
        allowed one returns ``data`` None unless ``keep_body`` (then FastAPI's ``detail``)."""
        url = self.base + API + path
        data = json.dumps(body) if body is not None else None
        resp, info = fetch_url(self.module, url, data=data, headers=self._headers(if_match), method=method, timeout=self.timeout)
        status = info.get("status", -1)
        # On HTTP errors fetch_url returns the (already read and closed) HTTPError as
        # ``resp`` and the body in ``info["body"]``; only a 2xx body is read from ``resp``.
        if resp is not None and 200 <= status < 300:
            raw = resp.read()
        else:
            raw = info.get("body")
        parsed = None
        if raw:
            try:
                parsed = json.loads(to_text(raw))
            except ValueError:
                parsed = to_text(raw)
        if 200 <= status < 300:
            return status, parsed
        if status in allow:
            if keep_body:
                return status, parsed.get("detail") if isinstance(parsed, dict) else parsed
            return status, None
        self._fail(status, parsed, info, url)

    def fail(self, status, detail, url=""):
        """Fail the task the way a refused request does (``detail`` = FastAPI's ``detail``)."""
        self._fail(status, {"detail": detail}, {}, url)

    def _fail(self, status, parsed, info, url):
        detail = parsed.get("detail") if isinstance(parsed, dict) else parsed
        extra = {}
        if isinstance(detail, dict) and detail.get("error") != "stale revision" and "message" in detail:
            extra = dict((k, v) for k, v in detail.items() if k != "message")
            detail = detail["message"]
        if status == -1 or status == 0:
            self.module.fail_json(msg="ais: cannot reach %s: %s" % (url, info.get("msg", "connection failed")))
        if status in (401, 403):
            self.module.fail_json(msg="ais: %s rejected the token (%s): %s" % (url, status, detail), status=status)
        if status == 409 and isinstance(detail, dict) and detail.get("error") == "stale revision":
            self.module.fail_json(
                msg=(
                    "ais: the inventory moved while this task ran (write based on revision %s, server is at %s); "
                    "re-run the task" % (detail.get("expected"), detail.get("current"))
                ),
                status=409,
                expected_revision=detail.get("expected"),
                current_revision=detail.get("current"),
            )
        if status == 409:
            self.module.fail_json(msg="ais: refused: %s" % to_native(detail), status=409, **extra)
        if status == 428:
            self.module.fail_json(msg="ais: the server wants If-Match on this write (client bug): %s" % detail, status=428)
        self.module.fail_json(msg="ais: %s %s: %s" % (url, status, to_native(detail)), status=status, **extra)

    # -------------------------------------------------------------------- reads

    def get(self, path, allow=(404,)):
        return self.request("GET", path, allow=allow)

    def current_revision(self):
        _status, revs = self.request("GET", "/revisions?limit=1", allow=())
        return revs[0]["id"] if revs else 0

    def node(self, ref):
        """Node with its live vars by path or id, or None."""
        status, data = self.get("/nodes/%s" % ref)
        if status == 404 or data is None:
            return None
        if data.get("deleted_rev") is not None:
            return None
        return data

    def definition(self, name):
        status, data = self.get("/variables/%s" % name)
        return None if status == 404 else data

    # ------------------------------------------------------------------- writes

    def collect(self, data):
        """Turn a write response's (or dry run's) ``warnings`` into Ansible warnings, once
        each; the structured list stays in ``key_warnings``."""
        if not isinstance(data, dict):
            return data
        for warning in data.get("warnings") or []:
            if warning not in self.key_warnings:
                self.key_warnings.append(warning)
                self.module.warn("ais: %s" % warning.get("message", warning))
        total = data.get("warnings_total") or 0
        shown = len(data.get("warnings") or [])
        if total > shown:
            self.module.warn("ais: ... and %d more undeclared key(s) that look like typos" % (total - shown))
        return data

    def preview(self, method, path, body=None, in_body=False):
        """Check mode: the server's dry run of a write (``dry_run=true`` in the query, or in
        the body for import/restore/revert). Sends ``If-Match`` only when ``expect_revision``
        was given (a dry run needs none; one that is sent must be current). Refusals fail the
        task exactly as the write would. Keeps the answer's ``impact``."""
        if in_body:
            body = dict(body or {}, dry_run=True)
        else:
            path = path + ("&" if "?" in path else "?") + "dry_run=true"
        _status, data = self.request(method, path, body=body, if_match=self.revision, allow=())
        if isinstance(data, dict) and data.get("impact") is not None:
            self.impacts.append(data["impact"])
        return self.collect(data)

    def write(self, method, path, body=None):
        """Locked write. Reads the current revision once when ``expect_revision`` was
        not given, then follows the revision each write returns."""
        if self.revision is None:
            self.revision = self.current_revision()
        if body is not None and self.message and "message" not in body and method in ("POST", "PUT", "PATCH"):
            body = dict(body, message=self.message)
        _status, data = self.request(method, path, body=body, if_match=self.revision, allow=())
        if isinstance(data, dict) and "revision" in data:
            self.revision = data["revision"]
        return self.collect(data)


# --------------------------------------------------------------- node helpers


def split_path(path):
    if "." not in path:
        return None, path
    parent, _sep, name = path.rpartition(".")
    return parent, name


def var_changes(current, wanted, purge):
    """(to_set, to_unset) turning ``current`` into ``wanted`` (keys not in wanted are
    left alone unless ``purge``)."""
    to_set = dict((k, v) for k, v in wanted.items() if k not in current or current[k] != v)
    to_unset = [k for k in current if k not in wanted] if purge else []
    return to_set, to_unset


def ensure_node(client, module, path, kind=None, inventory_name=None, meta=None, variables=None, purge_vars=False):
    """Create or converge the node at ``path``. Returns ``(changed, node, diff)``.

    Honors check mode: computes what would change from what it read (the verdict), and asks
    the server for a dry run of each write it would make (``Client.preview``), so the
    server's checks apply and ``client.impacts`` holds what each would do to the hosts.
    """
    variables = variables or {}
    node = client.node(path)
    if node is None:
        parent_path, name = split_path(path)
        if parent_path is None:
            module.fail_json(msg="ais: the root %r cannot be created" % path)
        if client.node(parent_path) is None:
            module.fail_json(msg="ais: parent %s does not exist" % parent_path)
        after = {"path": path, "kind": kind, "inventory_name": inventory_name, "meta": meta or {}, "vars": variables}
        diff = {"before": {}, "after": after}
        body = {"parent_path": parent_path, "name": name, "meta": meta or {}, "vars": variables}
        if kind:
            body["kind"] = kind
        if inventory_name:
            body["inventory_name"] = inventory_name
        if module.check_mode:
            client.preview("POST", "/nodes", body)
            return True, after, with_impact(diff, merge_impacts(client.impacts))
        created = client.write("POST", "/nodes", body)["node"]
        return True, created, diff

    if kind and node["kind"] != kind:
        module.fail_json(msg="ais: %s exists as kind %s, not %s (delete and recreate, or move)" % (path, node["kind"], kind))
    before = {"inventory_name": node.get("inventory_name"), "meta": node.get("meta") or {}, "vars": dict(node.get("vars") or {})}
    patch = {}
    if inventory_name is not None and inventory_name != (node.get("inventory_name") or ""):
        if inventory_name == "":
            patch["clear_inventory_name"] = True
        else:
            patch["inventory_name"] = inventory_name
    if meta is not None and meta != (node.get("meta") or {}):
        patch["meta"] = meta
    to_set, to_unset = var_changes(before["vars"], variables, purge_vars)
    after_vars = dict(before["vars"])
    after_vars.update(to_set)
    for k in to_unset:
        after_vars.pop(k, None)
    after = {
        "inventory_name": None if patch.get("clear_inventory_name") else patch.get("inventory_name", before["inventory_name"]),
        "meta": patch.get("meta", before["meta"]),
        "vars": after_vars,
    }
    changed = bool(patch or to_set or to_unset)
    diff = {"before": before, "after": after}
    if changed and module.check_mode:
        if patch:
            client.preview("PATCH", "/nodes/%d" % node["id"], patch)
        for key, value in to_set.items():
            client.preview("PUT", "/nodes/%d/vars/%s" % (node["id"], key), {"value": value})
        for key in to_unset:
            client.preview("DELETE", "/nodes/%d/vars/%s" % (node["id"], key))
        diff = with_impact(diff, merge_impacts(client.impacts))
    if not changed or module.check_mode:
        return changed, dict(node, **after), diff
    if patch:
        node = client.write("PATCH", "/nodes/%d" % node["id"], patch)["node"]
    for key, value in to_set.items():
        client.write("PUT", "/nodes/%d/vars/%s" % (node["id"], key), {"value": value})
    for key in to_unset:
        client.write("DELETE", "/nodes/%d/vars/%s" % (node["id"], key))
    return True, client.node(path), diff


def remove_node(client, module, path, recursive=False):
    """Delete the node at ``path`` if it exists. ``(changed, deleted_paths)``.

    A node with descendants needs ``recursive`` (the server refuses otherwise, with the
    counts, and the module fails with that message, in check mode too: check mode asks the
    server for a dry run, which applies the same rule and lists what would go)."""
    node = client.node(path)
    if node is None:
        return False, []
    if node["kind"] == "all":
        module.fail_json(msg="ais: the root cannot be deleted")
    query = "?recursive=true" if recursive else ""
    if module.check_mode:
        data = client.preview("DELETE", "/nodes/%d%s" % (node["id"], query))
        return True, data.get("deleted", [path])
    data = client.write("DELETE", "/nodes/%d%s" % (node["id"], query))
    return True, data.get("deleted", [path])


# ------------------------------------------------------------- impact helpers

IMPACT_SHOWN = 5  # hosts spelled out in the --diff summary


def merge_impacts(impacts):
    """One impact from the previews of several writes (a node task that patches the node and
    sets two keys previews three writes, each against the current inventory). Hosts are
    merged by name. The counts are exact while no preview was truncated; otherwise each count
    is the largest any one preview had (a lower bound) and ``truncated`` is true."""
    impacts = [i for i in impacts if i]
    if not impacts:
        return None
    if len(impacts) == 1:
        return impacts[0]
    limit = max(i.get("limit", 200) for i in impacts)
    hosts = {}
    for imp in impacts:
        for h in imp.get("hosts", []):
            cur = hosts.get(h["host"])
            if cur is None:
                hosts[h["host"]] = dict(h)
                continue
            for k in ("added", "removed", "changed"):
                if k in h:
                    cur[k] = dict(cur.get(k) or {}, **h[k])
            for k in ("joined", "left"):
                if k in h:
                    cur[k] = sorted(set(cur.get(k) or []) | set(h[k]))
            for k in ("moved_from", "status"):
                if k in h and k not in cur:
                    cur[k] = h[k]
    listed = sorted(hosts.values(), key=lambda h: (h.get("path") or "", h["host"]))
    groups = {}
    for imp in impacts:
        for g in imp.get("groups", []):
            row = groups.setdefault(g["group"], {"group": g["group"], "joined": 0, "left": 0})
            row["joined"] = max(row["joined"], g["joined"])
            row["left"] = max(row["left"], g["left"])
    truncated = any(i.get("truncated") for i in impacts)
    if truncated:
        counts = dict((k, max(i.get(k, 0) for i in impacts)) for k in ("total", "vars_changed", "added", "removed", "regrouped", "moved"))
    else:

        def has(h, *keys):
            return any(h.get(k) for k in keys)

        counts = {
            "total": len(listed),
            "vars_changed": sum(1 for h in listed if has(h, "added", "removed", "changed") and h["status"] == "changed"),
            "added": sum(1 for h in listed if h["status"] == "added"),
            "removed": sum(1 for h in listed if h["status"] == "removed"),
            "regrouped": sum(1 for h in listed if has(h, "joined", "left")),
            "moved": sum(1 for h in listed if h.get("moved_from")),
        }
    out = dict(counts)
    out.update(
        hosts=listed[:limit],
        groups=sorted(groups.values(), key=lambda g: g["group"])[:limit],
        groups_total=len(groups),
        limit=limit,
        truncated=truncated or len(listed) > limit,
    )
    return out


def _short(value, width=60):
    text = json.dumps(value, sort_keys=True, default=str)
    return text if len(text) <= width else text[: width - 3] + "..."


def impact_text(impact, shown=IMPACT_SHOWN):
    """The ``--diff`` summary of an impact: the counts, then the first few hosts."""
    if not impact:
        return ""
    parts = ["%d host(s) affected" % impact["total"]]
    for key, label in (("vars_changed", "vars changed"), ("added", "added"), ("removed", "removed"), ("regrouped", "change groups"), ("moved", "moved")):
        if impact.get(key):
            parts.append("%d %s" % (impact[key], label))
    lines = ["impact: " + ", ".join(parts)]
    for h in impact.get("hosts", [])[:shown]:
        bits = []
        for k, v in sorted((h.get("changed") or {}).items()):
            bits.append("%s: %s -> %s" % (k, _short(v["from"], 30), _short(v["to"], 30)))
        for k, v in sorted((h.get("added") or {}).items()):
            bits.append("+%s: %s" % (k, _short(v, 30)))
        for k in sorted(h.get("removed") or {}):
            bits.append("-%s" % k)
        groups = ["+" + g for g in h.get("joined") or []] + ["-" + g for g in h.get("left") or []]
        if groups:
            bits.append("groups " + " ".join(groups))
        if h.get("moved_from"):
            bits.append("from " + h["moved_from"])
        lines.append("  %s: %s%s" % (h["host"], h["status"], (" (" + "; ".join(bits) + ")") if bits else ""))
    if impact["total"] > shown:
        lines.append("  ... and %d more host(s)" % (impact["total"] - shown))
    return "\n".join(lines) + "\n"


def with_impact(diff, impact):
    """``diff`` (a before/after dict) plus the impact summary as ``prepared``."""
    text = impact_text(impact)
    if not text:
        return diff
    return dict(diff, prepared=text)

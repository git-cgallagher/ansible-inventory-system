# -*- coding: utf-8 -*-
# Copyright (c) 2026 Chris Gallagher
# MIT License (see LICENSE)
"""The ``tag`` module's stale-409 retry (``Client.write_or_stale``), issue #47.

A real race can't be provoked deterministically in an integration target, so these tests run
the module's ``main()`` against a fake server patched in at ``fetch_url`` (where the client
binds it). The real ``Client.request`` still parses every answer, so the HTTP error path is the
real one, down to ``fetch_url`` handing back an already-read response on a 4xx. The fake answers
the tag routes the way ``server/ais/api_tags.py`` does: ``If-Match`` checked before anything
else on a change, an unchanged ``PUT`` answered ``changed: false`` with no precondition.

A "concurrent writer" is a callable the fake runs right before it answers the next write: it
changes the tags and moves the revision on, so the module's ``If-Match`` is stale for real.
"""
from __future__ import annotations

import io
import json

import pytest

from ansible.module_utils import basic
from ansible.module_utils.testing import patch_module_args

from ansible_collections.git_cgallagher.inventory.plugins.module_utils import client as client_mod
from ansible_collections.git_cgallagher.inventory.plugins.modules import tag as tag_mod

BASE = "http://ais.example.com"
API = BASE + "/api/v1"
WEB = "all.commercial.prod.dc1.web"
DB = "all.commercial.prod.dc1.db"
OPS = "all.ops-tools.ops-bastion01"
STALE_MSG = "moved while this task ran"


class ModuleExit(Exception):
    def __init__(self, result):
        super(ModuleExit, self).__init__(result)
        self.result = result


class ModuleFail(Exception):
    def __init__(self, result):
        super(ModuleFail, self).__init__(result)
        self.result = result


def _exit_json(self, **kwargs):  # pylint: disable=unused-argument
    raise ModuleExit(kwargs)


def _fail_json(self, msg, **kwargs):  # pylint: disable=unused-argument
    kwargs["msg"] = msg
    raise ModuleFail(kwargs)


def tag_doc(name, members, description=None):
    return {"name": name, "description": description, "members": sorted(members)}


class FakeServer(object):
    """The routes the ``tag`` module calls: ``GET /revisions?limit=1``, ``GET/PUT/DELETE /tags/{name}``."""

    def __init__(self, revision=10, tags=None):
        self.revision = revision
        self.tags = dict((t["name"], t) for t in (tags or []))
        self.calls = []  # (method, path, if_match) of every request, in order
        self.concurrent = []  # run one before each write answered, while any are left
        self.refusals = []  # (status, detail) answered as {"detail": detail} instead of the next write

    # helpers for the tests
    def count(self, method, prefix):
        return len([c for c in self.calls if c[0] == method and c[1].startswith(prefix)])

    def writes(self):
        return [c for c in self.calls if c[0] in ("PUT", "DELETE")]

    def set_tag(self, doc):
        """A writer elsewhere changes the tag: the revision moves on."""
        self.tags[doc["name"]] = doc
        self.revision += 1

    def drop_tag(self, name):
        self.tags.pop(name)
        self.revision += 1

    # transport
    def fetch_url(self, module, url, data=None, headers=None, method=None, timeout=None):  # pylint: disable=unused-argument
        assert url.startswith(API), url
        path = url[len(API):]
        if_match = (headers or {}).get("If-Match")
        if_match = int(if_match.strip('"')) if if_match is not None else None
        body = json.loads(data) if data else None
        self.calls.append((method, path, if_match))
        status, doc = self.route(method, path, if_match, body)
        raw = json.dumps(doc).encode("utf-8")
        if 200 <= status < 300:
            return io.BytesIO(raw), {"status": status}
        # what fetch_url does on an HTTPError: the response is already read, the body is in info
        return io.BytesIO(b""), {"status": status, "body": raw, "msg": "HTTP Error %d" % status}

    def route(self, method, path, if_match, body):
        if method == "GET" and path == "/revisions?limit=1":
            return 200, [{"id": self.revision}]
        assert path.startswith("/tags/"), path
        name = path[len("/tags/"):]
        if method == "GET":
            if name in self.tags:
                return 200, dict(self.tags[name])
            return 404, {"detail": "tag %r does not exist" % name}
        if self.concurrent:
            self.concurrent.pop(0)(self)
        if self.refusals:
            status, detail = self.refusals.pop(0)
            return status, {"detail": detail}
        if method == "PUT":
            wanted = tag_doc(name, set(body["members"]), body.get("description"))
            if self.tags.get(name) == wanted:  # no-op PUT: no revision, no precondition
                return 200, {"revision": self.revision, "changed": False, "dry_run": False, "tag": wanted}
        stale = self.precondition(if_match)
        if stale:
            return stale
        if method == "PUT":
            self.set_tag(wanted)
            return 200, {"revision": self.revision, "changed": True, "dry_run": False, "tag": wanted}
        assert method == "DELETE", method
        if name not in self.tags:
            return 404, {"detail": "tag %r does not exist" % name}
        self.drop_tag(name)
        return 200, {"revision": self.revision, "changed": True, "dry_run": False, "name": name}

    def precondition(self, if_match):
        if if_match is None:
            return 428, {"detail": "If-Match required"}
        if if_match != self.revision:
            return 409, {"detail": {"error": "stale revision", "expected": if_match, "current": self.revision}}
        return None


def run(monkeypatch, server, **params):
    """Run the module once; ``(failed, result)``."""
    args = dict(url=BASE, token="example-not-secret", name="wave1")
    args.update(params)
    monkeypatch.setattr(basic.AnsibleModule, "exit_json", _exit_json)
    monkeypatch.setattr(basic.AnsibleModule, "fail_json", _fail_json)
    monkeypatch.setattr(client_mod, "fetch_url", server.fetch_url)
    with patch_module_args(args):
        try:
            tag_mod.main()
        except ModuleExit as done:
            return False, done.result
        except ModuleFail as failed:
            return True, failed.result
    raise AssertionError("the module neither exited nor failed")


# ------------------------------------------------------------------ the happy path


def test_no_race_writes_once(monkeypatch):
    server = FakeServer(tags=[tag_doc("wave1", [WEB])])
    failed, result = run(monkeypatch, server, paths=[DB])
    assert not failed, result
    assert result["changed"] is True
    assert result["added"] == [DB]
    assert result["tag"]["members"] == sorted([WEB, DB])
    assert server.writes() == [("PUT", "/tags/wave1", 10)]
    assert result["revision"] == 11


# --------------------------------------------- 1. one stale 409, then a retry or nothing


def test_one_stale_409_rereads_and_retries(monkeypatch):
    server = FakeServer(tags=[tag_doc("wave1", [WEB])])
    # someone adds OPS between the module's read and its write
    server.concurrent.append(lambda s: s.set_tag(tag_doc("wave1", [WEB, OPS])))
    failed, result = run(monkeypatch, server, paths=[DB])
    assert not failed, result
    assert result["changed"] is True
    # the retry is based on the re-read: OPS (the other writer's) stays, the task adds DB
    assert result["tag"]["members"] == sorted([WEB, DB, OPS])
    assert result["added"] == [DB]
    assert result["removed"] == []
    # the second If-Match is the revision read again after the 409, not the first one
    assert server.writes() == [("PUT", "/tags/wave1", 10), ("PUT", "/tags/wave1", 11)]
    assert server.count("GET", "/revisions") == 2
    assert server.count("GET", "/tags/") == 2
    assert server.tags["wave1"]["members"] == sorted([WEB, DB, OPS])
    assert result["revision"] == server.revision == 12


def test_one_stale_409_and_the_change_is_no_longer_needed(monkeypatch):
    server = FakeServer(tags=[tag_doc("wave1", [WEB])])
    # someone else adds the member the task wants, and one more (so the PUT is not a no-op)
    server.concurrent.append(lambda s: s.set_tag(tag_doc("wave1", [WEB, DB, OPS])))
    failed, result = run(monkeypatch, server, paths=[DB])
    assert not failed, result
    assert result["changed"] is False
    assert result["added"] == [] and result["removed"] == []
    assert result["tag"]["members"] == sorted([WEB, DB, OPS])
    assert server.writes() == [("PUT", "/tags/wave1", 10)]  # no second write
    assert server.count("GET", "/tags/") == 2  # re-read after the 409
    assert server.count("GET", "/revisions") == 2  # the first If-Match, then the result's revision
    assert result["revision"] == 11


def test_the_other_writer_already_did_it(monkeypatch):
    """Someone wrote exactly what the task wants before its PUT: the server answers that PUT
    ``changed: false`` without checking If-Match (an unchanged PUT has no precondition)."""
    server = FakeServer(tags=[tag_doc("wave1", [WEB])])
    server.concurrent.append(lambda s: s.set_tag(tag_doc("wave1", [WEB, DB])))
    failed, result = run(monkeypatch, server, paths=[DB])
    assert not failed, result
    assert result["changed"] is False
    assert server.writes() == [("PUT", "/tags/wave1", 10)]
    assert result["revision"] == 11


def test_one_stale_409_and_the_retry_finds_it_done(monkeypatch):
    """The other writer lands between the re-read and the retry: the server answers the
    retry's PUT ``changed: false`` (no precondition), and so does the module."""
    server = FakeServer(tags=[tag_doc("wave1", [WEB])])
    server.concurrent.append(lambda s: s.set_tag(tag_doc("wave1", [WEB, OPS])))
    server.concurrent.append(lambda s: s.set_tag(tag_doc("wave1", [WEB, DB, OPS])))
    failed, result = run(monkeypatch, server, paths=[DB])
    assert not failed, result
    assert result["changed"] is False
    assert len(server.writes()) == 2
    assert result["revision"] == 12


def test_absent_one_stale_409_rereads_and_retries(monkeypatch):
    server = FakeServer(tags=[tag_doc("wave1", [WEB])])
    server.concurrent.append(lambda s: s.set_tag(tag_doc("wave1", [WEB, OPS])))
    failed, result = run(monkeypatch, server, state="absent")
    assert not failed, result
    assert result["changed"] is True
    assert result["removed"] == sorted([WEB, OPS])  # what the re-read found
    assert server.writes() == [("DELETE", "/tags/wave1", 10), ("DELETE", "/tags/wave1", 11)]
    assert "wave1" not in server.tags
    assert result["revision"] == 12


def test_absent_one_stale_409_and_the_tag_is_already_gone(monkeypatch):
    server = FakeServer(tags=[tag_doc("wave1", [WEB])])
    server.concurrent.append(lambda s: s.drop_tag("wave1"))
    failed, result = run(monkeypatch, server, state="absent")
    assert not failed, result
    assert result["changed"] is False
    assert server.writes() == [("DELETE", "/tags/wave1", 10)]
    assert result["revision"] == 11


# ------------------------------------------------------- 2. two stale 409s: fail cleanly


def test_two_stale_409s_fail_cleanly(monkeypatch):
    server = FakeServer(tags=[tag_doc("wave1", [WEB])])
    server.concurrent.append(lambda s: s.set_tag(tag_doc("wave1", [WEB, OPS])))
    server.concurrent.append(lambda s: s.set_tag(tag_doc("wave1", [OPS])))
    failed, result = run(monkeypatch, server, paths=[DB])
    assert failed, result
    assert result["status"] == 409
    assert STALE_MSG in result["msg"] and "re-run the task" in result["msg"]
    assert result["expected_revision"] == 11
    assert result["current_revision"] == 12
    assert server.writes() == [("PUT", "/tags/wave1", 10), ("PUT", "/tags/wave1", 11)]  # one retry only
    assert server.tags["wave1"]["members"] == [OPS]  # the module wrote nothing


def test_absent_two_stale_409s_fail_cleanly(monkeypatch):
    server = FakeServer(tags=[tag_doc("wave1", [WEB])])
    server.concurrent.append(lambda s: s.set_tag(tag_doc("wave1", [WEB, OPS])))
    server.concurrent.append(lambda s: s.set_tag(tag_doc("wave1", [OPS])))
    failed, result = run(monkeypatch, server, state="absent")
    assert failed, result
    assert result["status"] == 409 and STALE_MSG in result["msg"]
    assert len(server.writes()) == 2
    assert "wave1" in server.tags


# ------------------------------------------- 3. a 409 that is not stale: no retry at all


def test_other_409_fails_without_a_retry(monkeypatch):
    server = FakeServer(tags=[tag_doc("wave1", [WEB])])
    server.refusals.append((409, {"message": "export check failed: duplicate host web01", "hosts": ["web01"]}))
    failed, result = run(monkeypatch, server, paths=[DB])
    assert failed, result
    assert result["status"] == 409
    assert result["msg"] == "ais: refused: export check failed: duplicate host web01"
    assert result["hosts"] == ["web01"]  # a structured detail's other keys reach the result
    assert STALE_MSG not in result["msg"]
    assert len(server.writes()) == 1
    assert server.count("GET", "/tags/") == 1  # no re-read
    assert server.tags["wave1"]["members"] == [WEB]


def test_other_409_with_a_plain_detail_fails_without_a_retry(monkeypatch):
    server = FakeServer(tags=[tag_doc("wave1", [WEB])])
    server.refusals.append((409, "tag group collides with another tag"))
    failed, result = run(monkeypatch, server, paths=[DB])
    assert failed, result
    assert result["status"] == 409
    assert result["msg"] == "ais: refused: tag group collides with another tag"
    assert len(server.writes()) == 1


# ------------------------- 4. expect_revision pinned: no retry (Client.can_retry_stale)


def test_pinned_revision_stale_409_fails_without_a_retry(monkeypatch):
    server = FakeServer(tags=[tag_doc("wave1", [WEB])])
    server.revision = 12  # the inventory moved past the pinned revision
    failed, result = run(monkeypatch, server, paths=[DB], expect_revision=10)
    assert failed, result
    assert result["status"] == 409 and STALE_MSG in result["msg"]
    assert result["expected_revision"] == 10 and result["current_revision"] == 12
    assert server.writes() == [("PUT", "/tags/wave1", 10)]
    assert server.count("GET", "/revisions") == 0  # the module never reads the revision itself
    assert server.count("GET", "/tags/") == 1


def test_pinned_revision_absent_stale_409_fails_without_a_retry(monkeypatch):
    server = FakeServer(revision=12, tags=[tag_doc("wave1", [WEB])])
    failed, result = run(monkeypatch, server, state="absent", expect_revision=10)
    assert failed, result
    assert result["status"] == 409 and STALE_MSG in result["msg"]
    assert server.writes() == [("DELETE", "/tags/wave1", 10)]
    assert "wave1" in server.tags


def test_pinned_current_revision_writes(monkeypatch):
    server = FakeServer(tags=[tag_doc("wave1", [WEB])])
    failed, result = run(monkeypatch, server, paths=[DB], expect_revision=10)
    assert not failed, result
    assert result["changed"] is True
    assert server.writes() == [("PUT", "/tags/wave1", 10)]
    assert server.count("GET", "/revisions") == 0


@pytest.mark.parametrize("pinned, allowed", [(None, True), (0, False), (10, False)])
def test_can_retry_stale_only_without_expect_revision(pinned, allowed):
    class Module(object):
        params = {"expect_revision": pinned}

    client = client_mod.Client.__new__(client_mod.Client)
    client.module = Module()
    assert client.can_retry_stale() is allowed

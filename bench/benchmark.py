"""Time the read paths (and one write) of a running server over a large tree.

Measures, against ``--url`` with a viewer token (``AIS_TOKEN``):

* full export: the first request at a revision, a repeat, and a revalidation with
  ``If-None-Match`` (304);
* the background snapshot pass (``GET /api/v1/snapshots/status``): the first full pass over
  every host, and the pass after the write below with how many hosts it stored;
* subtree export of one datacenter;
* single-host render (``POST /render {host}``, Jinja on) and a mock render at a path;
* ``ansible-inventory --graph`` through the real plugin (fetch + parse into Ansible's
  inventory, which is what a playbook run pays), with the ETag store off and warm (304);
* ``ansible-inventory --list``: the same plus ansible-core computing every host's variables
  (vars plugins, ``host_vars`` globs, a JSON dump of everything). A playbook never does this;
  it is listed because it is the obvious thing people time.
* with ``--editor-token`` (``AIS_EDITOR_TOKEN``), one variable write (lock + revision +
  the exporter check every write runs), then the export and the snapshot pass after it.

Prints a Markdown table (wall-clock seconds, median of ``--repeat`` runs where repeating
means something). Not part of CI; results go in ``docs/scale.md``.

    AIS_TOKEN=… python bench/benchmark.py --url http://127.0.0.1:8000 --host h000123 \\
        --subtree all.b0.prod.dc0 --path all.b1.stage.dc2.svc0.role1.pool3
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]


class Api:
    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.token = token

    def call(self, method: str, path: str, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                raw = resp.read()
                status, etag = resp.status, resp.headers.get("ETag")
        except urllib.error.HTTPError as exc:
            raw, status, etag = exc.read(), exc.code, exc.headers.get("ETag")
        return time.perf_counter() - t0, status, raw, etag


def timed(fn, repeat: int) -> tuple[float, object]:
    runs, last = [], None
    for _ in range(repeat):
        dt, last = fn()
        runs.append(dt)
    return statistics.median(runs), last


def ansible_inventory(url: str, token: str, store: str | None, mode: str = "--list") -> float:
    exe = pathlib.Path(sys.executable).with_name("ansible-inventory")
    exe = str(exe) if exe.exists() else shutil.which("ansible-inventory")
    with tempfile.TemporaryDirectory() as tmp:
        inv = pathlib.Path(tmp) / "inventory.yml"
        inv.write_text(f"plugin: git_cgallagher.inventory.db\nurl: {url}\n")
        env = dict(os.environ, AIS_TOKEN=token, ANSIBLE_COLLECTIONS_PATH=str(ROOT / "collection"), ANSIBLE_INVENTORY_UNPARSED_FAILED="1")
        env["AIS_ETAG_STORE"] = store or ""
        t0 = time.perf_counter()
        subprocess.run([exe, "-i", str(inv), mode], check=True, env=env, stdout=subprocess.DEVNULL)
        return time.perf_counter() - t0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--url", default=os.environ.get("AIS_URL", "http://127.0.0.1:8000"))
    p.add_argument("--host", required=True, help="a host name to render")
    p.add_argument("--subtree", required=True, help="a subtree path to export")
    p.add_argument("--path", required=True, help="a path for the mock render")
    p.add_argument("--repeat", type=int, default=5)
    p.add_argument("--skip-ansible", action="store_true")
    args = p.parse_args(argv)
    token = os.environ["AIS_TOKEN"]
    api = Api(args.url, token)
    rows: list[tuple[str, float, str]] = []

    def row(name: str, seconds: float, note: str = "") -> None:
        rows.append((name, seconds, note))
        print(f"{name}: {seconds:.3f} s {note}", file=sys.stderr)

    def wait_for_pass(revision: int | None = None, timeout: float = 600.0) -> dict:
        """Block until the background pass has passed ``revision`` (default: the newest)."""
        deadline = time.monotonic() + timeout
        while True:
            _dt, status, raw, _e = api.call("GET", "/api/v1/snapshots/status")
            assert status == 200, raw[:200]
            st = json.loads(raw)
            target = revision if revision is not None else st["revision"]
            if target and st["passed"] >= target and st["last_pass"] is not None:
                return st
            if time.monotonic() > deadline:
                raise TimeoutError(f"snapshot pass stuck at {st['passed']} < {target}")
            time.sleep(0.5)

    dt, status, raw, etag = api.call("GET", "/api/v1/export")
    assert status == 200, (status, raw[:200])
    data = json.loads(raw)
    row("full export, first at this revision", dt, f"{len(raw) / 1e6:.1f} MB, {len(data['hosts'])} hosts, {len(data['groups'])} groups")
    st = wait_for_pass()
    lp = st["last_pass"]
    row("snapshot pass, first (background)", lp["seconds"], f"{lp['hosts_checked']} hosts checked, {lp['rows_written']} stored")
    med, _ = timed(lambda: (lambda r: (r[0], r))(api.call("GET", "/api/v1/export")), args.repeat)
    row("full export, repeat", med)
    med, last = timed(lambda: (lambda r: (r[0], r))(api.call("GET", "/api/v1/export", headers={"If-None-Match": etag})), args.repeat)
    assert last[1] == 304, last[1]
    row("full export, If-None-Match (304)", med)

    med, last = timed(lambda: (lambda r: (r[0], r))(api.call("GET", f"/api/v1/export?subtree={args.subtree}")), args.repeat)
    sub = json.loads(last[2])
    row(f"subtree export ({args.subtree})", med, f"{len(last[2]) / 1e3:.0f} kB, {len(sub['hosts'])} hosts")

    med, last = timed(lambda: (lambda r: (r[0], r))(api.call("POST", "/api/v1/render", {"host": args.host})), args.repeat)
    assert last[1] == 200, last[2][:200]
    row(f"render host {args.host} (Jinja on)", med)
    med, last = timed(lambda: (lambda r: (r[0], r))(api.call("POST", "/api/v1/render", {"path": args.path, "template": False})), args.repeat)
    assert last[1] == 200, last[2][:200]
    row("render mock path (node page read)", med)

    editor = os.environ.get("AIS_EDITOR_TOKEN")
    if editor:
        ed = Api(args.url, editor)
        _dt, _s, raw, _e = ed.call("GET", f"/api/v1/nodes/{args.path}")
        node_id = json.loads(raw)["id"]
        _dt, _s, raw, _e = ed.call("GET", "/api/v1/revisions?limit=1")
        rev = json.loads(raw)[0]["id"]
        dt, status, raw, _e = ed.call("PUT", f"/api/v1/nodes/{node_id}/vars/bench_marker", {"value": time.time()}, headers={"If-Match": f'"{rev}"'})
        assert status == 200, raw[:200]
        row("variable write (PUT, includes the exporter check)", dt)
        new_rev = json.loads(raw)["revision"]
        dt, _s, raw, _e = api.call("GET", "/api/v1/export")
        row("full export, first after that write", dt)
        lp = wait_for_pass(new_rev)["last_pass"]
        row("snapshot pass after that write (background)", lp["seconds"], f"{lp['hosts_checked']} hosts checked, {lp['rows_written']} stored")

    if not args.skip_ansible:
        for mode, note in (("--graph", "plugin fetch + parse"), ("--list", "plus ansible-core computing and dumping every host's vars")):
            med, _ = timed(lambda: (ansible_inventory(args.url, token, None, mode), None), 3)
            row(f"ansible-inventory {mode} (no ETag store)", med, note)
            with tempfile.TemporaryDirectory() as store:
                ansible_inventory(args.url, token, store, mode)
                med, _ = timed(lambda: (ansible_inventory(args.url, token, store, mode), None), 3)
            row(f"ansible-inventory {mode} (ETag store warm: 304)", med, note)

    print("| Operation | Seconds | Notes |\n|---|---:|---|")
    for name, seconds, note in rows:
        print(f"| {name} | {seconds:.2f} | {note} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

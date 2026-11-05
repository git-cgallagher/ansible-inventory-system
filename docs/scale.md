# Scale proof (2026-11-01, updated the same day for snapshots on change)

Can the server and the plugin carry an inventory of 100,000 hosts? This page has the answer,
the numbers behind it and how to reproduce them. Not part of CI: rerun it by hand after
changes to the resolver, the exporter, snapshots or the plugin.

## What still costs, worst first

1. **The background snapshot pass re-resolves every host after each write or burst of
   writes**: 7.1 s of CPU at 100k hosts, off the request path but in the API process. With
   one uvicorn worker it shares that process with requests for those seconds; the export
   right after a write measured 4.1 s instead of 3.3 s while the pass ran. It stores only the
   hosts that changed (12 rows for the benchmark's write). The very first pass on a tree, or
   after upgrading to this design, stores every host once (14.3 s, ~80 MB). If the pass ever
   needs to be cheaper, resolve only the hosts under the nodes a revision touched instead of
   all of them.
2. **Every write re-runs the exporter over the whole tree** (`check_export`, the guard
   against group-name and tag collisions): 3.3 s per write at 100k hosts, most of it loading
   the tree. That is fine for UI edits and modules, slow for a loop of thousands of module
   writes (use `import` for bulk changes, which checks once).
3. **Loading the tree is 2.4 s** of the 3.6 s repeat export: 110k `Node` and 240k
   `NodeVariable` ORM objects. Reading plain rows instead of ORM objects is the next step if
   it matters.
4. **`GET /api/v1/nodes` without `subtree` returns every node** (110k nodes, ~15 MB). The UI's
   Render page calls it for its host picker, so that page is slow on a large tree.
5. `ansible-inventory --list` is ~23 s, but that is ansible-core computing every host's
   variables (vars plugins glob `host_vars/` 400k times, then a 17 s JSON dump). A playbook
   never does that. The plugin's own cost is the `--graph` line: 4.5 s with a warm ETag store.

## Results, 100k hosts on PostgreSQL 16 (compose)

| Operation | Seconds | Notes |
|---|---:|---|
| full export, first at this revision | 3.46 | 18.9 MB, 100,000 hosts, 9,917 groups |
| snapshot pass, first (background) | 14.26 | 100,000 hosts checked, 100,000 stored (once per tree) |
| full export, repeat | 3.34 | |
| full export, `If-None-Match` (304) | 0.00 | |
| subtree export (`all.b0.prod.dc0`) | 0.06 | 312 kB, 1,680 hosts |
| tree export, whole tree (JSON) | 3.25 | 6.9 MB, the import format (`GET /api/v1/tree`); see below |
| tree export, whole tree (YAML) | 5.27 | 17.5 MB, libyaml's dumper |
| tree export (`all.b0.prod.dc0`, YAML) | 0.09 | 288 kB |
| render host `h012345` (Jinja on) | 0.02 | |
| render mock path (the node page's read) | 0.00 | |
| variable write (`PUT`, includes the exporter check) | 3.29 | |
| full export, first after that write | 4.14 | while the background pass ran |
| snapshot pass after that write (background) | 7.06 | 100,000 hosts checked, 12 stored |
| `ansible-inventory --graph`, no ETag store | 7.96 | plugin fetch + parse |
| `ansible-inventory --graph`, ETag store warm (304) | 4.48 | plugin parse only |
| `ansible-inventory --list`, no ETag store | 26.44 | plus ansible-core computing and dumping every host's vars |
| `ansible-inventory --list`, ETag store warm (304) | 22.86 | same |

Wall-clock seconds from the benchmark client on the same machine. Repeated reads are the
median of 5 runs, the `ansible-inventory` lines of 3; the two pass lines are the pass's own
timing from `GET /api/v1/snapshots/status`. Memory after the run: API container 379 MiB,
PostgreSQL 200 MiB; database 191 MB, of which `host_snapshot` is 81 MB (100,012 rows: the first
pass, then 12 for the write). Measured 2026-11-01 after the snapshots-on-change change; the
earlier run stored 78 MB more on every export after a write.

The three tree-export lines were measured later (2026-11-05, issue 26) and differently: one
`curl` each against `ais serve` on the host (Python 3.12) with the same generated tree in a
PostgreSQL 16 container, where the plugin export measured 3.97 s. In process the medians of 3
were 3.0 s (JSON), 5.2 s (YAML) and 0.09 s (subtree). `bench/benchmark.py` now times them too.

**Tree** (`bench/generate.py --hosts 100000`): depth 8 (`all` → 4 businesses → 3 tiers → 5
datacenters → three nested `group` levels of 4 × 5 × 7 → hosts). That is 109,917 nodes:
100,000 hosts, 9,840 groups, 60 datacenters. It has 239,751 variables: 3–4 per non-host
node, 2 per host and 3 on every tenth host. The fold variables `packages` (unique_append)
and `sysctl` (merge) contribute at five levels, so the export exercises the fold. Bulk
insert: 7.2 s, 208 MB peak.

**Machine**: Apple M3 Max (14 cores, 36 GB), Docker Desktop 29.9.2 (aarch64 VM, 14 CPUs,
7.7 GiB). The API is the compose image (Python 3.12, one uvicorn worker), on PostgreSQL 16.15.
The client and `ansible-inventory` run on the host (Python 3.14, ansible-core 2.21.5).

## Before and after

| | before | after |
|---|---:|---|
| full export, 11,917 nodes (2,000 hosts) on SQLite | 24 s (48 s under the profiler) | 0.3 s |
| full export, 100k hosts on PostgreSQL | HTTP 500 | 3.6 s (18.5 s first at a revision), then 3.3–3.5 s at every revision once snapshots moved to the background |
| first full export after a write, 100k hosts | 18.9 s (wrote 100k snapshots) | 4.1 s |
| snapshot rows stored for one write, 100k hosts | 100,000 (~78 MB), per export | only the changed hosts (12 here), once |
| render one host, 100k hosts | 2.41 s | 0.01 s |
| render a mock path (node page), 100k hosts | 2.45 s | 0.00 s |
| subtree export of one datacenter, 100k hosts | 2.49 s | 0.10 s |
| repeat full export, 100k hosts | 3.55 s (with the index fixes) | 3.63 s; 0.7 s of encoding saved, within noise |

What changed (`server/ais/resolver.py`, `snapshots.py`, `api.py`):

- **Snapshots moved off the export and render path** (the later change): a background pass
  per revision stores a host's resolved variables only where they changed. A revision the
  pass skipped is computed on demand, so every lookup and diff stays exact.

- **`Snapshot.children()` scanned every node for every node** (quadratic: 238 million
  attribute reads for 12k nodes). There is now a children index, a host-name index, and a
  per-snapshot cache of each node's emitted variables (each node's fold is computed once
  and shared, not once per host below it). Tag groups collect the hosts under each tagged
  node in one pass over the hosts' ancestor paths.
- **Snapshot writes hit PostgreSQL's 65,535 bind-parameter limit**: one `IN` list of 100k
  host ids, then one 100k-row insert. Lookups and inserts are chunked (`_CHUNK = 1000`);
  materializing every host reads the revision's existing rows without an id list.
- **Subtree export filters in SQL**, on PostgreSQL through the GiST-indexed `path_ltree`
  column (`path_ltree <@ 'all.b0.prod.dc0'`), elsewhere with an escaped prefix match. Until
  now the resolver ignored that index.
- **Render loads only the host's chain** (`load_chain`): a host's emitted variables depend
  only on its ancestors, so the root-to-host path resolves exactly like the whole tree
  (`tests/test_load_filters.py` checks this for every seeded host; the parity test is
  unchanged and passes). The same applies to diffs that compute a missing snapshot, and to
  the "unknown host" check of `GET /hosts/{name}/snapshots`.
- `/export` serializes its JSON itself instead of letting FastAPI's `jsonable_encoder` walk
  19 MB in Python (0.66 s measured in-process; it did not show up beyond noise end to end).
- **Plugin bug found on the way**: `etag_store` was `type: path`, and Ansible expands an
  empty path to the current directory. `AIS_ETAG_STORE=""` therefore wrote the 19 MB export
  into the cwd instead of disabling the store (and the first "no store" timings were really
  warm). It is `type: str` now, with a test.

## Reproduce

```bash
# PostgreSQL + API from this checkout; the overlay publishes PostgreSQL on 127.0.0.1:55434
# and moves the API to 127.0.0.1:8813 (needs Compose >= 2.24 for `!override`).
C="docker compose -p ais-scale -f deploy/compose.yml -f bench/compose.override.yml"
$C up -d --build
.venv/bin/python bench/generate.py --hosts 100000 --database-url postgresql+psycopg://ais:ais@127.0.0.1:55434/ais
$C exec -T api ais user create bench@example.com --role editor --no-password
export AIS_TOKEN="$($C exec -T api ais token create --user bench@example.com --name bench | tail -1)"
AIS_EDITOR_TOKEN="$AIS_TOKEN" .venv/bin/python bench/benchmark.py --url http://127.0.0.1:8813 \
  --host h012345 --subtree all.b0.prod.dc0 --path all.b1.stage.dc2.svc0.role1.pool3
$C down -v      # the volume holds the 100k tree; drop it when done
```

`generate.py` refuses a database that already holds nodes. Run the benchmark on a freshly
generated tree, or the "first at this revision" line measures a revision whose snapshots
already exist. The benchmark writes one variable (`bench_marker`) when `AIS_EDITOR_TOKEN` is
set.

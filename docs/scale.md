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
4. **`GET /api/v1/nodes` without `q`, `kind`, `limit` or `subtree` returns every node**
   (109,917 nodes, 20.6 MB, 2.8 s). The UI no longer calls it: its pickers use the bounded
   lookup (4–10 ms, see [Node lookup](#node-lookup-2026-11-05-issue-27)). Nor does the `host`
   module any more (issue 43): a task whose host is not at its `parent_path` (a new host, or
   `absent` without a parent) looks the name up with `?q=<name>&kind=host`, the row
   "host `h012345`" below (0.007 s for a hit, at most 0.073 s for a miss), plus a
   `GET /nodes/{id}` of the one host it found, instead of 20.6 MB and 2.8 s.
5. `ansible-inventory --list` is ~23 s, but that is ansible-core computing every host's
   variables (vars plugins glob `host_vars/` 400k times, then a 17 s JSON dump). A playbook
   never does that. The plugin's own cost is the `--graph` line: 4.5 s with a warm ETag store.
6. **Every host's value of one variable over the whole tree** (`GET /api/v1/vars/hosts`
   without `subtree`) is 1.3–2.2 s: it loads every node in scope, like the export. With a
   datacenter as `subtree` it is 0.03 s. See [Variable search](#variable-search-2026-11-05-issue-23).
7. **A write's dry run costs what the write costs, plus its impact.** The impact itself is
   bounded (0.06 s at a datacenter, 0.4–1.1 s at `all`), but the dry run also runs the
   write and its export check (3.1–3.3 s at 100k hosts). A move or a delete also pays
   `api_write.live_subtree`, which reads every live node as ORM objects to find the subtree
   (about 2 s here): the rename below spends 5.6 s, of which 0.4 s is the impact. See
   [Write previews](#write-previews-2026-11-05-issue-25).

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

### Profiles (2026-11-05, issue 22)

The same tree generated with `--profiles`: `role-common` (5 values of every strategy, one
shadowed by the groups' own `app_port`) attached to all 1,200 `roleN` groups, so it reaches
every one of the 100,000 hosts, and `pool-tuning` attached to the 1,200 `pool3` groups (14,286
hosts). 2,400 attachments. The compose image (Python 3.12, one worker) on PostgreSQL 16;
medians of 3 from `bench/benchmark.py --profile role-common --narrow-profile pool-tuning
--skip-ansible`.

| Operation | Seconds | Notes |
|---|---:|---|
| full export, repeat | 3.47 | 19.4 MB (3.34 s without profiles, above) |
| full export, first at this revision | 6.69 | ran while the first snapshot pass (15.3 s) was busy |
| render host `h012345` (Jinja on) | 0.007 | two profiles on its chain |
| render mock path (the node page's read) | 0.005 | |
| tree export, whole tree (JSON / YAML) | 2.72 / 4.44 | with the `profiles:` block and 2,400 node lists |
| `GET /api/v1/profiles/role-common` | 0.008 | with its 1,200 attachments |
| dry run: value change in `role-common` (replace key) | 0.78 | 100,000 hosts affected, 200 listed |
| dry run: value change in `role-common` (`sysctl`, merge) | 0.78 | 100,000 hosts affected |
| dry run: value change in `pool-tuning` | 0.48 | 14,286 hosts affected |
| dry run: detach `role-common` from one `roleN` group | 0.17 | 84 hosts affected |
| profile value write (`PUT /profiles/role-common/vars/…`) | 0.04 | no exporter check: nothing a profile write changes names a group |
| (for comparison) dry run: `packages` at `all` | 4.61 | the node-value path, unchanged |
| (for comparison) variable write at a node | 3.74 | includes the exporter check |

A profile value change resolves only that key, for the hosts under the nodes the profile is
attached to (on both sides of the write), through the same light-row `Snapshot` as any dry
run: the 100,000-host preview costs about a fifth of a node value change at `all` (whose
scope also only has one key, but which reads every value of a fold key twice). The real
profile write is the cheapest write there is. The export pays one more query (live
attachments and the attached profiles' values) and a merge of layers only at the 2,400 nodes
that have profiles. Memory after the run: API container 489 MiB, PostgreSQL 240 MiB.

### Variable search (2026-11-05, issue 23)

Measured like the tree-export lines: `ais serve` on the host (Python 3.14, one worker) against
the same generated tree in a PostgreSQL 16 container, medians of 5 from `bench/benchmark.py`
(the two `ansible_host` lines one `curl` each). Not comparable with the compose rows above.

| Operation | Seconds | Notes |
|---|---:|---|
| where is `app_port` set, whole tree (`GET /vars`) | 0.02 | 1,440 nodes |
| where is `ansible_host` set, whole tree (first page of 10) | 0.95 | 100,000 nodes: a key set on every host |
| hosts with `app_port`, whole tree (`GET /vars/hosts`, first page) | 1.29 | 100,000 hosts, 5 distinct values |
| hosts with `app_port=9003`, whole tree | 1.40 | 19,999 hosts match |
| hosts with `app_port`, `all.b0.prod.dc0` | 0.03 | 1,680 hosts |
| hosts with `packages` (unique_append fold), whole tree | 1.67 | 100,000 hosts, top 50 of the distinct folds |
| hosts with `ansible_host="10.0.48.57"`, whole tree | 2.15 | one match among 100,000 distinct values |

*Where set* is one indexed query: `ix_nodevar_key_live (key, deleted_rev)` (migration 0007)
finds the key's rows, then the node primary key (PostgreSQL plan: bitmap index scan on
`ix_nodevar_key_live` → nested loop on `node_pkey`, 3 ms for 1,440 rows; with `subtree` a
BitmapOr of `ix_node_path_prefix` and the `path_ltree` GiST index, 1.4 ms). Its cost grows with
the number of nodes that set the key, not with the tree.

*Hosts* reuses the resolver: `load_snapshot(subtree, keys=[key])` loads the scope's nodes and
only that key's values, and each host takes `_emitted` of the deepest node on its chain that sets
the key (equal to `resolve(...)["resolved"][key]`; the tests check it host by host). The cost is
loading the scope's nodes as ORM objects, so `subtree` is the lever: 0.03 s for a datacenter.
Paging does not make the whole tree cheaper, because the counts and the value distribution
cover every host in scope (and a value filter has to look at all of them).

Why not the stored host snapshots (`host_snapshot.resolved`)? They lag: a revision the
background pass has not reached yet (seconds after a write at this size, longer for a burst)
has no rows, so the answer would be stale right after the change an operator is investigating,
and a pinned revision the pass skipped has none at all. Querying `resolved->>'key'` across
100,000 JSONB rows would also need a GIN index on a table that grows with history, and would
still miss `from`/`sources`. Computing at the exact revision is exact by construction and fast
enough with a subtree; snapshots stay the source for per-host history and diffs.

*Audit by key* uses `ix_audit_key (key, id)`: with 300,000 synthetic rows, `key = … ORDER BY id
DESC LIMIT 50` is an index scan backward, 0.2 ms; adding `subtree` (the recorded path,
`coalesce(after->>'path', before->>'path')`, unindexed) filters the key's rows, 1.8 ms.

### Write previews (2026-11-05, issue 25)

Every write's `dry_run` runs the write in its transaction, computes `impact` (`ais.impact`) and
rolls back. Measured like the variable-search lines: `ais serve` on the host (Python 3.14, one
worker) against the generated tree in a PostgreSQL 16 container. The first column is
`bench/benchmark.py`'s wall clock (median of 5) for the five dry runs it times, and the
in-process total for the `DELETE` and tag lines; the next two come from timing the same
requests in process (median of 3), split into the impact and the export check.

| Dry run | Seconds | Impact part | Export check | Impact |
|---|---:|---:|---:|---|
| `PUT packages` (unique_append fold) at `all` | 4.33 | 1.08 | 3.12 | 100,000 hosts change |
| `PUT ntp_server` at `all` | 3.74 | 0.42 | 3.16 | 0 hosts: every datacenter sets it |
| `PUT ntp_server` at `all.b0.prod.dc0` | 3.32 | 0.06 | 3.24 | 1,680 hosts change |
| `DELETE` a variable at `all.b0.prod.dc0` | 0.07 | 0.06 | — | 1,680 hosts (this route runs no export check) |
| rename `all.b0.prod.dc0` (`move`) | 5.61 | 0.39 | 3.13 | 1,680 hosts change groups |
| `packages` strategy to `replace` (`PUT /variables`) | 1.17 | 1.10 | — | 100,000 hosts (no export check) |
| tag `all.b0.prod.dc0` (`PUT /tags`) | 3.34 | 0.07 | 3.25 | 1,680 hosts join the tag group |

How the impact stays bounded (the revert preview it replaces resolved every host under the
reverted paths in full, and every host in the tree when a definition was involved):

- **Only what the write can change.** The helper reads back what the revision touched: value
  rows opened or closed (path + key), node rows opened or closed (a create, move, delete or
  restore), and the state that is not temporal (strategies, tag memberships, `inventory_name`)
  as noted before the write and after it. A value at P: the hosts under P, that key only. A
  move: the hosts under the old and new paths, and only the keys set above them or inside the
  moved subtree. A strategy change: the hosts under the nodes that set the key. A schema-only
  change: nothing.
- **Light rows.** Every non-host node (9,917 here) and the hosts in scope are read as six
  plain columns, not ORM objects, and only the keys in scope are read. Unless the revision
  opened or closed node rows, the before and after sides share the same rows.
- **One comparison per setter, not per host.** A host's value of a key is the emitted value of
  the deepest node on its chain that sets it (`resolver._emitted`, the variable-search rule).
  Hosts with no value of their own share their parent's "who sets what" map, so 100,000 hosts
  under one change come down to a few thousand distinct comparisons. Values are compared as
  canonical JSON.
- **Exact counts, capped lists.** Every affected host is counted; only the first 200 by path
  are built into the answer (`heapq.nsmallest`).

What is left at the root is reading 110k light rows twice and folding the setters of a fold
key (18,400 distinct `packages` setters here, most of the 1.08 s). That is about a third of
the export check the write itself runs, so it was not worth more.

### Node lookup (2026-11-05, issue 27)

The UI's host and path pickers used to download every node (`GET /api/v1/nodes`) and filter
in the browser. They now ask `GET /api/v1/nodes?q=…&kind=…&limit=21` per pause in typing
(`ais.nodesearch`). Measured like the variable-search lines: `ais serve` on the host (Python
3.12, one worker, `AIS_SNAPSHOTS=off`) against the generated tree in a PostgreSQL 16.15
container, medians of 5 from `bench/benchmark.py --lookup-only`.

| Lookup (`limit=21`) | Seconds | Notes |
|---|---:|---|
| host prefix `h` | 0.004 | 21 nodes, 3,928 bytes |
| host prefix `h0123` | 0.005 | 21 nodes, 3,970 bytes |
| host `h012345` | 0.007 | 1 node: every tier runs (5 queries) |
| path prefix `all.b0.prod.dc0`, hosts | 0.010 | 21 nodes, 3,865 bytes |
| path prefix `all.b0.prod`, non-host | 0.010 | 21 nodes |
| name substring `ole`, non-host | 0.006 | 21 nodes |
| `x`, hosts: no match at all | 0.073 | 5 queries, the last two sequential scans |
| host prefix `h0` under `all.b0.prod.dc0` | 0.010 | 21 nodes |
| every node (`GET /api/v1/nodes`, what the pickers loaded before) | 2.76 | 109,917 nodes, 20.6 MB |

**How it is bounded.** Five tiers (exact name, name prefix, path prefix, name substring, path
substring), each one `ILIKE` query with `ORDER BY path LIMIT n` that runs only while the answer
is short; a typical keystroke is one or two queries. Migration 0008 adds `pg_trgm` and two
GIN trigram indexes, `ix_node_name_trgm` (5.9 MB) and `ix_node_path_trgm` (11 MB). With no
`revision` the live filter is `deleted_rev IS NULL`, the predicate of the partial index
`uq_node_path_live`, so the planner has two plans and picks by the estimate (`EXPLAIN
(ANALYZE, BUFFERS)`):

- **Few matches** (a rare name, a miss of three or more characters): bitmap index scan on a
  trigram index, then a top-N sort of the handful of rows. 0.01–0.04 ms per tier.
- **Many matches** (`h`, `h0`, a path prefix): an index scan of `uq_node_path_live` in path
  order with the `ILIKE` as a filter, stopping at the limit. `h` reads 8 buffers. A path prefix
  walks the paths that sort before it first: `all.b1.stage.dc2` removed 49,829 rows by filter
  and took 22 ms; `all.b0.prod.dc0`, near the start, 10 ms end to end.
- **One or two characters that match nothing**: no trigram to look up, so the two substring
  tiers scan the table (24 ms and 41 ms for `x`). That is the worst case, 73 ms end to end.

The cost grows with how far a path-order walk goes before it finds `limit` matches, not with
the number of matches. A lookup at a `revision` uses the same indexes (they cover closed rows
too) with `_live_at` as a filter. SQLite (development only) has no trigram index and scans.

**The page, before and after.** Bytes per response as the browser received them (Playwright,
`request.sizes()` and the response bodies), opening each page on a generated tree of 3,000
hosts (`bench/generate.py --hosts 3000`: 12,917 nodes, since the group levels are fixed) on
SQLite. No compression on either side (the server sends none).

| Page | Before: API bytes | After: API bytes | Of which the node list (before) |
|---|---:|---:|---:|
| `#/render` | 2,315,214 | 634 | 2,314,580 |
| `#/render?host=h000123` | 2,318,850 | 4,270 | 2,314,580 |
| `#/diff` | 2,315,344 | 764 | 2,314,580 |
| `#/diff?host=h000123&from=1&to=1` | 2,315,592 | 1,012 | 2,314,580 |

Typing `h00012` in the Host picker then costs one lookup of 1,871 bytes (10 hosts). The page
assets are the same on both sides (the bundle grew from 390,974 to 393,203 bytes). At 100k
hosts the node list alone was 20.6 MB per page open.

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
# add --profiles to attach role-common / pool-tuning (then pass --profile role-common
# --narrow-profile pool-tuning to benchmark.py)
$C exec -T api ais user create bench@example.com --role editor --no-password
export AIS_TOKEN="$($C exec -T api ais token create --user bench@example.com --name bench | tail -1)"
AIS_EDITOR_TOKEN="$AIS_TOKEN" .venv/bin/python bench/benchmark.py --url http://127.0.0.1:8813 \
  --host h012345 --subtree all.b0.prod.dc0 --path all.b1.stage.dc2.svc0.role1.pool3
$C down -v      # the volume holds the 100k tree; drop it when done
```

`generate.py` refuses a database that already holds nodes. Run the benchmark on a freshly
generated tree, or the "first at this revision" line measures a revision whose snapshots
already exist. When `AIS_EDITOR_TOKEN` is set, the benchmark runs the five dry runs above
(nothing written) and then writes one variable (`bench_marker`). `--lookup-only` times just
the node lookups (read-only, seconds).

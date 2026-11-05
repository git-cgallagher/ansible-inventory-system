# AGENTS.md — ansible-inventory-system

Read `~/Code/AGENTS.md` first (workspace rules). Design: `~/Code/ANSIBLE-INVENTORY-PLAN.md`.

## What this is
Authoritative database-backed Ansible inventory. Strict tree; Ansible merges natively;
the server only folds declared merge/append variables. **Phase 1 (core API) done
2026-11-01**: bearer-token auth with roles, locked write endpoints, Jinja render with mock
facts, host snapshots + diff, Alembic migrations, ltree on PostgreSQL, ETag/304 in the
plugin. **Phase 2 (write path) done 2026-11-01**: audit log (`audit_log`, `GET /audit`),
node move/rename (`POST /nodes/{id}/move`), lookup by path, and the collection's write
modules (`node`, `host`, `variable`, `variable_definition`) + info modules (`node_info`,
`render_info`) with `ansible-test sanity` and integration targets in CI. **Phase 3 (web UI)
done 2026-11-01**: React + TypeScript (Vite) single-page app in `web/`, built into
`server/ais/static` and served by FastAPI at `/` (same origin, bearer token from the login):
login (SSO button when OIDC is configured), lazy keyboard tree with own vs inherited vars and
provenance, JSON/YAML variable editor with `If-Match`, render (host or mock path), host diff,
revision and audit browsers. Playwright smoke in CI (job `web`). **Phase 4 (in progress,
2026-11-01)**: tag write API (`/api/v1/tags`), `node_move` and `import` modules, `json_schema`
enforced at write time, bad-token requests audited (`token.rejected`), failed-login limiter
(429), idempotent `POST /import` with `dry_run`; the UI creates, moves and deletes nodes, declares
variables, manages tags and API tokens, and its history links follow a node across moves.
**Scale proof** (`docs/scale.md`, `bench/`): 100k hosts on PostgreSQL. Full export 3.6 s (18.5 s
the first time at a revision), subtree export 0.10 s, render 0.01 s, `ansible-inventory --graph`
4.5 s with a warm ETag store.

## Invariants (do not break)
- **Parity test is load-bearing.** `server/tests/test_parity.py` must keep passing:
  server render == `ansible-inventory --host`. Any change to the resolver, exporter or
  plugin that breaks it is wrong by definition, not "a test to update".
- **One path per node.** No multiple parents, no cross-cutting layers. Cross-cutting
  convenience = var-less tag groups only. **Tags never carry variables.**
- **Fold is emitted at every contributing node as the cumulative fold** from the root
  (`resolver.emitted_vars`), the host included when the host itself contributes. Nodes that
  do not contribute emit nothing for the key. Never only at the deepest node.
- **Group names are qualified** from the path with `_`; collisions are an export error.
- **Snapshots are written only by the background pass, only on change** (`ais.snapshots`).
  Export and render never write. A host's state at revision R is: the row stored exactly at R;
  else, if R has a `snapshot_pass` row, the host's latest row at or before R; else computed on
  demand and **never stored** (storing it would make rule 2 lie for later passes). Passes only
  ever write at a revision higher than every earlier pass. History is keyed by `host_name`.
  The worker runs in every API process (`AIS_SNAPSHOTS=off` disables it; tests set that and call
  `run_pass`); a PostgreSQL advisory lock keeps two processes off the same pass.
- **Every new or relocated node goes through `importer.check_position`** (depth ≤
  `AIS_MAX_DEPTH`, default 32; path ≤ `MAX_PATH_BYTES` = 1,500). Import, `POST /nodes` and move
  call it; a new write path that creates or re-paths nodes must too. The importer walks with an
  explicit stack: never make it recursive again (it died near depth 1,000).
- **Nodes and variables are temporal rows**; never UPDATE a value in place or hard-delete —
  close the row with `deleted_rev` and insert a new one under a new `Revision`. `DELETE
  /nodes/{id}` closes the whole subtree and its live variables, **but only when confirmed**:
  a node with live descendants needs `recursive=true` or `expect_count` = subtree size (node
  included; a wrong count refuses even with `recursive`), else 409 `subtree not empty` with
  `message`/`nodes`/`hosts` (`api_write._delete_guard`). `dry_run=true` is a pure read (no
  revision opened, no `If-Match`) and applies the same guard. Never add a delete path that skips it
  (the module sends `recursive` only when the task says so; the UI sends the previewed count). A pinned revision therefore
  sees exactly the tree of that moment. (`inventory_name`, `meta`, variable *definitions* and
  **tags** (`tag`, `tag_member`) are the documented exceptions: not temporal, changes apply to
  every revision's export. Tag writes still lock, need `If-Match` and open a revision.)
- **Tags are var-less and named by path.** `PUT /tags/{name}` is declarative (`description`
  replaced, `members` = the complete set of node *paths* when given; ids never, a move changes
  them). Names are `[A-Za-z0-9_]`; `business_`/`tier_`/`datacenter_` prefixes and `all` are
  reserved (the automatic tag groups would silently absorb such a tag); a name equal to a
  hierarchy group is the exporter's 409. A tag with no live members is not emitted.
- **`json_schema` is enforced on every value write** (`ais.schema`): `PUT …/vars/{key}`,
  `POST /nodes` vars, the importer (CLI too) and `PUT /variables/{name}`, which validates the
  schema itself and re-checks every live value of the key. The schema checks each node's stored
  contribution, not the folded result. Invariant: every live value satisfies its definition.
- **Every write needs `If-Match: "<revision>"`** (`ais.locking.begin_write`: lock → compare
  with `current_revision` → new `Revision`). 428 without, 409 when stale. Never add a write
  route that skips it.
- **Every `/api/v1` route needs a principal** (`Depends(require(Role.x))`); reads = viewer,
  writes = editor, users = admin, **audit = editor** (viewers must not see who changed what, or
  the failed-login e-mails). `/healthz` is the only open route. There is deliberately no
  "auth off" switch.
- **The render never reaches the server** (`ais.render._guard`). It runs for any viewer, on
  values a viewer sends, so its Templar runs no lookups, refuses `SERVER_FILTERS`/`SERVER_TESTS`
  and every collection filter or test, has an empty loader, and never trusts request `facts`.
  `tests/test_render.py`'s `SERVER_REACHING` cases must stay refused, via an override and via a
  stored value. A new builtin filter or test that touches files, the environment, the network
  or other server state goes into those sets.
- **Tree export round-trips through import.** `ais.tree.export_tree` (`GET /api/v1/tree`,
  `ais export-tree`) emits exactly the importer's format: kinds (always, for non-host nodes:
  a `group` under `all` would re-import as a business), **own** values from
  `snap.vars_by_node` (never `_emitted`: the fold would double on re-import; only `append`
  shows it), declarations and tags. Importing an export reproduces the plugin export
  (`build_export`, ids aside) and every host's resolved vars, and re-importing it into the
  same tree is a no-op (`tests/test_tree_export.py`: empty database, delete + restore at a past
  revision, clone with `as`). Anything new the tree can hold that changes the export (a
  column, a node attribute) must be exportable *and* importable, or be listed as left out in
  the guide (node `meta` is, today). A subtree's ancestors are structure only unless
  `ancestor_vars`: a restore must not roll the parents back. `ais.restore.restore_subtree`
  (`POST /nodes/restore`, `ais restore`) is exactly `export_tree(session, R, path)` then
  `import_tree_report` in-process, plus `meta` (which the format leaves out) and the id map.
- **The CLI writes like the API.** `ais import` (`cli._import`) takes `lock_for_write`, checks the
  export, drops a no-op (no revision) and records one `import` audit row (`tests/test_cli.py`). A
  new CLI write command does the same.
- **Every effective write and every auth event leaves exactly one `audit_log` row, in the same
  transaction** (`ais.audit.record`, after `check_export`). A refused write (409/422/428) therefore
  leaves none, and a no-op (`PUT` of an unchanged var or definition) leaves none either. The one
  deliberate exception to "raise to refuse": a failed login *returns* its 401 so the row commits.
  `before`/`after` never contain a secret (token plaintext, password hash). New write routes call
  `_audit` (or `audit.record`) or they are wrong. A request that **presents** a bearer token
  that does not authenticate leaves one `token.rejected` row (reason, client, method, path),
  written in its own transaction by `auth.audit_bad_token` before the 401 is raised; its
  `target_id` is the token id, the `ais_xxxxxx` display prefix, or `(not an ais_ token)`, never
  more. Requests with no token are not recorded. A throttled login is `login.throttled` (429,
  returned like the 401).
- **Move/rename never updates `path` in place.** `POST /nodes/{id}/move` closes the whole subtree
  and reopens it under the new revision: **node ids change** (the response's `moved` list maps
  old → new), variables and tag memberships are copied to the new rows, host snapshots survive by
  `host_name`. The old revision still exports the old tree. Refused: the root, a move under the
  node's own subtree, `check_kind`, a live node at the new path (409), and whatever `check_export`
  rejects (qualified-name / tag collisions, duplicate host names after a host rename).
- **Modules are idempotent and locked.** Each module reads first and exits unchanged without
  touching the revision; a change sends `If-Match` from `expect_revision` or the current revision
  (`GET /revisions?limit=1`) and follows the revision each write returns. Check mode computes the
  diff and writes nothing. Module writes carry `X-AIS-Source: module` and one `X-Request-ID` per
  task, which is what `revision.source` and `audit_log.source`/`request_id` record.
- **The schema is owned by `ais/migrations`**, not `create_all`. A model change needs a new
  migration (`alembic -c server/alembic.ini revision --autogenerate -m …`, then review it);
  the drift check is "migrated SQLite schema == `Base.metadata`" (columns + indexes).
- Never read `*.db` test artefacts into docs; never commit `examples/ais.db`.
- **The UI is just another API client.** It calls `/api/v1` with the bearer token like the
  plugin and the modules; it never gets a private endpoint or a cookie session. The server adds
  only `GET /` (index, `no-cache`) and `GET /assets/{path}` (hashed files, `immutable`; a
  flat, exact-name lookup in the `assets/` listing, so no file path is ever built from the URL). Routing is by hash (`#/tree?path=…`), so there is **no SPA catch-all**:
  unknown paths stay 404 and no API route can be shadowed (`server/tests/test_web.py`).
  A missing build makes `/` a 404 and nothing else (the `test` job runs without one).
- **UI writes carry `If-Match` = the revision the page shows** (from the render that loaded the
  values), never a revision fetched just before the write, which would defeat the lock. A stale
  409 reloads the values and keeps the user's text; the UI sends `X-AIS-Source: ui`.
- **Node detail reads through `POST /render {path, template: false}`**, also for hosts, so the
  page shows exactly what a mock host at that path would get. (Renders write nothing.) Own vars
  are the provenance entries whose `path` is the node's; ids go in no URL (a move changes them).
- **Structure writes reset the tree.** After a create, move or delete the tree view drops every
  cached node (ids changed) and reveals the path to show next; never patch cached ids in place.
  Pages whose values carry no revision of their own (Variables, Tags) read `GET /revisions?limit=1`
  *together with* the list and send that as `If-Match`. Tokens are not inventory writes (no
  `If-Match`); revoking the token the page runs on signs the user out.
- **History links go by path, not id**: `#/audit?node=<path>&key=<key>`. The server
  (`GET /audit?node=&key=`, `audit.node_lineage`) walks the `node.move` (`after.moved`) and
  `node.restore` (`after.restored`) rows back through every id the node had (its own moves and
  its ancestors', and the deleted rows a restore replaced), so the history survives moves,
  renames, deletes and restores. Both maps cover the **whole subtree**, `[{old_id, id, …}]`; a
  new write that re-creates nodes under new ids must record one too, or the history breaks.
- **A restore makes new rows; it never revives deleted ones.** A node row is live over one
  interval `[created_rev, deleted_rev)`; clearing `deleted_rev` would make every revision between
  the delete and the restore show the subtree (and `host_path` find its hosts there). It
  refuses, before writing: the root (422), a live node at the path (409; never overwrites), a
  parent that is not live (409, naming the highest deleted ancestor: otherwise the export's
  spine would recreate it var-less), host names live elsewhere (409), values a tightened
  definition rejects (422). The default revision is the newest `Revision.id` **below** the
  latest `deleted_rev` at the path, never `deleted_rev - 1` (ids are not contiguous on
  PostgreSQL). The revision's `source` is the client's (`api`/`ui`/`module`/`cli`), not the
  importer's `import`.

## Repo and CI
Public repo `git-cgallagher/ansible-inventory-system` (created 2026-11-01; full new-public-repo
baseline from `~/Code/AGENTS.md`). CI `ci.yml`: job `test` (pytest incl. parity, Python 3.12 +
3.14, plus an `ansible-inventory --graph` smoke), job `image` (docker build) and job `collection`
(`ansible-test sanity` + the eight module integration targets against a live `ais serve` on SQLite,
Python 3.12; the step mints an editor token into the gitignored `integration_config.yml`) and job
`web` (`npm ci`, `npm run build` = `tsc` + Vite, then the Playwright smoke against `e2e/serve.sh`;
the report is uploaded on failure). The `image` job builds the UI too (Node stage in the
Dockerfile). All are required by the "main protection" ruleset (id `26318946`; `collection` and
`web` added 2026-11-01 after their first green runs); the "external contributor review" ruleset means your own PRs merge with
`gh pr merge --admin` once every check is green. Work on branches, never push to `main`.
**Several sessions may share this checkout**: never switch branches in the shared folder for side
work; use `git worktree add` in the scratchpad instead.

**Release** (`release.yml`): **never published to Ansible Galaxy** (Chris, 2026-11-01; there is
no `galaxy` environment or key). Every push to `main` reads the collection's `galaxy.yml` version;
when no GitHub release `v<version>` exists it builds the collection and creates that release with
the tarball, tagging the merge commit for a new version or building from the existing tag's
tree when the tag already exists. A merge that changes the collection without a bump is a no-op
(with a notice). One version for
everything: `server/pyproject.toml`, `ais.__version__`, `galaxy.yml` and `web/package.json` move
together, and a release PR bumps all four plus `CHANGELOG.md`. Merge one release PR at a time
and wait for its run.

## Commands
```bash
python3 -m venv .venv && .venv/bin/pip install -e './server[test,postgres,oidc]'
cd server && ../.venv/bin/python -m pytest -q          # 73 tests, ~10 s (SQLite)
cd examples && export AIS_DATABASE_URL=sqlite:///./ais.db
../.venv/bin/ais import seed.yml                       # also runs the migrations
../.venv/bin/ais user create me@example.com --role admin   # prompts; --no-password for token-only
export AIS_TOKEN="$(../.venv/bin/ais token create --user me@example.com)"   # shown once
../.venv/bin/ais serve                                 # from examples/
../.venv/bin/ansible-inventory --graph                 # examples/ansible.cfg sets collections_path; token from AIS_TOKEN
docker compose -f deploy/compose.yml up --build        # PostgreSQL + API + UI on :8000 (verified 2026-11-01, phase 3: Playwright smoke passed against it)
docker compose -f deploy/compose.yml exec api ais user create admin@example.com --role admin --no-password
docker compose -f deploy/compose.yml exec api ais token create --user admin@example.com
# whole suite against PostgreSQL (publish 5432 with an override file; schema is dropped per test):
AIS_TEST_DATABASE_URL=postgresql+psycopg://ais:ais@127.0.0.1:55432/ais_test ../.venv/bin/python -m pytest -q
# a throwaway PostgreSQL for that: docker run -d --rm --name ais-pg-test -e POSTGRES_USER=ais -e POSTGRES_PASSWORD=ais \
#   -e POSTGRES_DB=ais_test -p 127.0.0.1:55432:5432 postgres:16
# collection (run from the collection dir; the venv's ansible-test accepts --python 3.14):
cd collection/ansible_collections/git_cgallagher/inventory
../../../../.venv/bin/ansible-test sanity --requirements --python 3.14
cp tests/integration/integration_config.yml.template tests/integration/integration_config.yml  # url + an EDITOR token of a server holding seed.yml
../../../../.venv/bin/ansible-test integration --python 3.14            # all targets; or name one: ... integration node
# web UI (Node 22; from web/): build into server/ais/static, which `ais serve` then serves at /
cd web && npm ci && npm run build
npm run dev                                            # Vite on :5173, proxies /api + /healthz to ais serve on :8000
npx playwright install chromium                        # once per machine
npx playwright test                                    # smoke: starts e2e/serve.sh on :8799 (seed tree, e2e@example.com)
E2E_BASE_URL=http://127.0.0.1:8000 npx playwright test # against a server you run (seed imported, e2e user created)
# schema drift check (prints [] on SQLite; `tests/test_migrations.py` asserts it on both databases):
AIS_DATABASE_URL=sqlite:////tmp/drift.db ../.venv/bin/ais init-db && ../.venv/bin/python -c "from alembic.autogenerate import compare_metadata; from alembic.migration import MigrationContext; from ais.db import make_engine; from ais.models import Base; c=make_engine('sqlite:////tmp/drift.db').connect(); print(compare_metadata(MigrationContext.configure(c, opts={'compare_type': True}), Base.metadata))"
```
The venv is Python 3.14 + ansible-core 2.21.5 (no system ansible on this Mac). CI also runs
3.12, so no 3.13+-only syntax.

## Structure
- `server/ais/models.py` — `Node` (kind, path, depth, temporal), `NodeVariable` (temporal),
  `VariableDefinition` (merge strategy), `Revision`, `Tag`/`TagMember`, `User`
  (`user_account`), `ApiToken`, `HostSnapshot`, `AuditLog` (`audit_log`). `LEVEL_ORDER` is the
  level schema; `group` may nest; `TAGGED_KINDS` get auto tag groups; `NAME_RE` = ltree-safe node names.
- `server/ais/migrations/` — Alembic (`env.py` takes a connection from
  `ais.db.init_db`; `0001` = spike schema, `0002` = phase 1 incl. the PostgreSQL ltree
  branch, `0003` = `audit_log`, `0004` = `node.meta` / `node_variable.value` NOT NULL). On SQLite
  `init_db` migrates with `PRAGMA foreign_keys=OFF` and runs `PRAGMA foreign_key_check` before
  turning them back on. `server/alembic.ini` is for hand use only.
- `server/ais/audit.py` — `RequestIdMiddleware` (`X-Request-ID` in/out, `X-AIS-Source` →
  `source`, both as context variables), `record(session, …)`, `audit_out`, `as_utc` (query
  datetimes: naive = UTC, so `since`/`until` work on SQLite's naive strings and PostgreSQL's
  timestamptz alike).
- `server/ais/resolver.py` — `fold`, `load_snapshot(revision, subtree, paths=)` (revision-aware
  for nodes and vars; subtree and paths filter in SQL, the subtree through `path_ltree` on
  PostgreSQL), `load_chain(revision, host=|path=)` (just the ancestor chain, which is all
  `resolve` needs), `build_export`, `resolve(host|path, overrides)`. `Snapshot` indexes children
  and hosts on first use and caches each node's emitted vars (`_emitted`): shared, never mutate.
- `docs/getting-started.md` — the user guide; `README.md` stays the short reference. A change to
  commands, settings, limits, roles or deployment updates both. `deploy/compose.prod.yml` —
  production overrides that guide's section 3 uses.
- `bench/` — `generate.py` (synthetic tree straight into the DB: 100k hosts, ~10k groups, depth
  8), `benchmark.py` (export, subtree, render, write, `ansible-inventory --graph/--list`),
  `compose.override.yml` (publishes PostgreSQL on 55434, API on 8813). Results: `docs/scale.md`.
  Not in CI.
- `server/ais/render.py` — `render_templates(resolved, facts, hostname, chain)` → Templar
  over trusted strings, `(rendered, errors)`.
- `server/ais/snapshots.py` — the background pass (`run_pass`, `catch_up`, `SnapshotWorker`,
  `worker`), `get_snapshot` (the three-way lookup), `list_snapshots` (change points), `diff`,
  `status`. `resolver.resolved_vars` is the pass's provenance-free `resolve`.
- `server/ais/auth.py` (hashing, tokens, `Principal`, `require(role)`, `audit_bad_token`),
  `server/ais/oidc.py` (stub), `server/ais/locking.py` (`If-Match`, advisory lock, `begin_write`),
  `server/ais/ratelimit.py` (`login_limiter`: failed logins per e-mail and per client address in
  a sliding window, in process memory; `AIS_LOGIN_MAX_FAILURES` 5, `AIS_LOGIN_MAX_FAILURES_PER_IP`
  20, `AIS_LOGIN_WINDOW_SECONDS` 300, 0 = off), `server/ais/schema.py` (`check_schema`,
  `check_value`, `check_live`; Draft 2020-12, `format` not asserted).
- `server/ais/api_tags.py` — `GET /tags[/{name}]`, `PUT /tags/{name}`, `DELETE /tags/{name}`.
- `server/ais/api.py` (reads incl. `GET /nodes/{id-or-path}`, export, render, snapshots,
  `GET /audit`, import), `api_write.py` (locked writes incl. `move`), `api_auth.py` (login,
  tokens, users; all audited), `cli.py` (`user`/`token` commands audit with `source=cli`).
- `server/ais/tree.py` — `export_tree(session, revision, subtree, as_path=, ancestor_vars=,
  hosts=)` (the import format; `TreeExportError` for a bad `as`), `dump_yaml` (libyaml when
  present, fixed key order, no folding). Definitions: all for the whole tree, the used ones
  for a subtree; tags: members inside the subtree only (re-rooted with `as`).
- `server/ais/restore.py` — `restore_subtree(session, path, revision, actor=, source=)` →
  `RestoreResult` (`restored` = `[{old_id, id, path}]`, `summary()`), `RestoreError(status,
  message)`, `last_live_revision`. The caller locks, runs `check_export`, records the
  `node.restore` row (`after.restored` is what `node_lineage` reads) or rolls back a dry run.
- `server/ais/importer.py` — YAML tree → nodes/vars under one revision; kind inferred
  from depth unless declared; rejects bad kind order and names, values their schema rejects.
  `import_tree_report` returns an `ImportResult` (counts, `tags_changed`, `changed`, `summary()`;
  `tags:` = name → description + member paths, additive, after the tree; `tag_name_error` is
  shared with `PUT /tags`); `POST /import` rolls back a
  no-op (200, `changed: false`, no revision, no audit row) and a `dry_run` (no `If-Match` needed).
- `collection/.../plugins/inventory/db.py` — the plugin (`fetch_export` does the ETag
  revalidation; `etag_store` option). Accepts `inventory.yml` / `ais.yml` file names.
- `collection/.../plugins/module_utils/client.py` — `Client` (token/url from options or
  `AIS_TOKEN`/`AIS_URL`, `request`, `write` with `If-Match`, 409 stale vs other 409, 401/403),
  `ensure_node`/`remove_node` (shared by `node` and `host`; `remove_node(recursive=)`, check
  mode = the server's delete `dry_run`). A structured 409 detail's `message` becomes the task's
  `msg`, its other keys (`nodes`, `hosts`, …) the result's. `doc_fragments/api.py` holds the
  `api` and `api.write` fragments; `meta/runtime.yml` declares the `inventory` action group
  (`module_defaults: group/git_cgallagher.inventory.inventory`).
- `collection/.../plugins/modules/` — `node`, `host`, `variable`, `variable_definition`,
  `node_move` (path → new_path; done when only new_path is live), `import` (tree dict; check mode =
  server dry run) (write, check + diff mode), `node_info`, `render_info`. `tests/integration/targets/<module>` = check,
  real, repeat, failure cases with an `always:` cleanup, all against one shared server (unique
  `it_*`/`it-*` names per target). `tests/sanity/ignore-2.*.txt` only waive the GPLv3-header
  check (the collection is MIT).
- `server/ais/api.py` (end) — `web_dir()` (`AIS_WEB_DIR` or `ais/static`), `GET /`, `GET /assets/…`;
  `/healthz` reports `oidc`. `ais/oidc.py` `ui_handoff()`: with `/login?ui=1` the callback answers
  with a page that puts the session into `sessionStorage['ais.session']` and goes to `/`.
- `web/` — Vite + React 19 + TypeScript, no component library (deps: `react`, `react-dom`,
  `yaml`). `src/api.ts` (fetch wrapper: bearer, `If-Match`, `X-AIS-Source: ui`, `ApiError.stale`
  for the 409 body `{error, expected, current}`, any 401 → login), `src/router.ts` (hash routes),
  `src/views/` (`Login`, `Tree` (lazy children, ARIA tree keys), `NodeDetail`, `NodeActions`
  (add child, move/rename, delete), `VarEditor`, `Render`, `Diff`, `Revisions`, `Audit`,
  `Variables` (definitions: strategy, description, JSON Schema), `Tags`, `Tokens`), `src/styles.css` (tokens, dark via
  `prefers-color-scheme`). `vite.config.ts` sets `outDir: ../server/ais/static`.
  `e2e/smoke.spec.ts` + `e2e/serve.sh` + `playwright.config.ts`.
- `server/tests/conftest.py` — `engine` (SQLite or `AIS_TEST_DATABASE_URL`), role tokens,
  `AuthClient` (sends the bearer, `if_match=` kwarg), live `server` + real-plugin helpers.

## ⚠️ Gotchas
- **Behind a host proxy, the container's peer is Docker's bridge gateway, not 127.0.0.1.** A
  failed login through `127.0.0.1:8000` was audited as `client: 172.19.0.1`, so
  `FORWARDED_ALLOW_IPS=127.0.0.1` would never trust the proxy's `X-Forwarded-For`, and the
  per-address login limit would count every user as one. `compose.prod.yml` sets `*`, which is
  safe only with the port published on loopback. Measured 2026-11-05.
- `ansible-inventory` is not on PATH in agent shells; tests resolve it next to
  `sys.executable` (the venv). Run pytest with the venv's python.
- Subtree export keeps the ancestor chain (`all` → … → subtree root) so inherited vars
  still apply; it is not just "nodes under X".
- `path` is text on both databases; on PostgreSQL `path_ltree` is a *generated* column
  (`text2ltree(path)`, GiST) that the resolver does not use yet. It is why node names are
  limited to `[A-Za-z0-9_-]` (ltree labels; hyphens need PostgreSQL 16+). It is migration-only,
  not in the ORM model, or SQLite would break.
- `hash_behaviour=merge` must stay off on consumers; the server already folds.
- **Long paths break PostgreSQL's GiST index, not the btree.** `ix_node_path_ltree` holds two
  whole paths per internal key and needs two keys per 8 kB page: one 2,431-byte path failed
  with "failed to add item to index page" (a 500), and 40 siblings at 2,000 bytes failed while
  a single 2,000-byte path passed. 200 at 1,500 and 500 at 1,024 work, hence the 1,500 cap.
  SQLite has no such limit, so only a PostgreSQL run (compose, or `AIS_TEST_DATABASE_URL`)
  can catch a regression here. Measured 2026-11-01.
- **ansible-core 2.19+ only templates strings tagged trusted.** `Templar.template("{{ x }}")`
  on a plain `str` returns it unchanged, silently. `ais.render._trust` wraps every string with
  `trust_as_template` first; catch `AnsibleTemplateError` (covers undefined, syntax,
  recursion). Learned 2026-11-01 (first render returned raw templates with no error).
- **An inventory plugin's strings are not templates in ansible-core 2.19+ unless the plugin sets
  `trusted_by_default = True`.** `ansible-inventory --host` (what the parity test compares)
  never templates, so it could not catch this: until 0.3.0 every playbook saw stored Jinja as
  literal text while the render evaluated it. `test_parity.py`'s
  `test_playbooks_evaluate_stored_templates_like_the_render` runs `ansible` against the server
  and compares with the render. Found 2026-11-05.
- **Trusted templates run lookups wherever they are rendered.** Trusting every stored string
  (above) made the server render a shell for any token: `lookup('pipe', …)` ran on the API host
  until 0.3.0 (found 2026-11-05, while writing the getting-started guide). `disable_lookups` is a
  deprecated no-op in ansible-core 2.21 (warns, never forwarded), so `_guard` replaces the
  `lookup`/`query`/`q` globals and wraps the environment's `filters`/`tests` mappings instead.
  Each Templar gets its own environment, so this touches no other templating. A test value
  must not be named `lookup`, `query` or `q`: the variable shadows the global and the test
  passes for the wrong reason (a recursion error).
- **Alembic batch mode (SQLite) needs named constraints**: `add_column` with an unnamed
  `ForeignKey` inside `batch_alter_table` fails "Constraint must have a name"; the reflected
  spike FKs are unnamed too, hence the `naming_convention` passed to each batch.
- **Plugin `DOCUMENTATION` is YAML**: a `: ` inside a description line breaks every inventory
  parse with an unhelpful "while scanning a simple key"; quote such lines.
- Options set in the inventory file beat env vars (`url:` in `examples/inventory.yml` wins
  over `AIS_URL`); the token is only in `AIS_TOKEN` on purpose.
- The write lock on SQLite is a no-op `UPDATE alembic_version` (takes the DB write lock before
  `current_revision` is read); PostgreSQL uses `pg_advisory_xact_lock`. Two writers on SQLite
  serialize on the 5 s busy timeout; that is fine for dev, not a deployment target.
- A refused write (409/422) rolls back its `Revision` with the request: never commit before
  `check_export`.
- `/export` returns a ready-made JSON `Response` (FastAPI's `jsonable_encoder` walked 19 MB in
  Python); its `ETag` is set on that response, not on an injected `response`.
- The export `ETag` includes a digest of `subtree` / `group_separator` / `emit_tags`, so a
  tag is only valid for the same parameters; the plugin's store is keyed per URL.
- `~/.cache/ais/export-*.json` (the plugin's ETag store) holds a full inventory copy per
  source on the controller. `AIS_ETAG_STORE=""` disables it (tests use a tmp dir).
- **A failed inventory load is a warning in Ansible, and the playbook exits 0 on no hosts**
  (server down, bad token, `revision: 999` → 404, a file name `verify_file` rejects). The
  consumer fix is `any_unparsed_is_failed = True` under **`[inventory]`**; under `[defaults]`
  (either spelling) Ansible ignores it silently. `examples/ansible.cfg` sets it. Tests use
  `ANSIBLE_INVENTORY_UNPARSED_FAILED` / `ANSIBLE_INVENTORY_ANY_UNPARSED_IS_FAILED` for the same
  reason. Measured 2026-11-05 on compose (issue #19, fail-closed inventory).
- **Ansible's inventory cache key is the plugin name + the file's path only**
  (`Cacheable.get_cache_key`). With `cache: true` a fresh cache is served without contacting
  the server, whatever the file now says (`revision`, `subtree`, `url`) and whether the server
  is up; `any_unparsed_is_failed` can't catch it because the parse succeeded. `parse()` warns
  on that branch (`test_inventory_cache_hit_warns`); keep the warning. `--flush-cache` passes
  `cache=False` and refetches.
- The CI plugin smoke serves on :8000; running `docker compose up` at the same time makes two
  servers fight over the port and the smoke reads the wrong one. Run one at a time.
- **A refusal raised as `HTTPException` rolls back the whole session, audit row included.** That
  is right for refused writes and wrong for a failed login, which must be recorded: `login`
  returns a `JSONResponse(401)` instead of raising. Keep it that way.
- **`fetch_url` hands back the HTTPError object as `resp` on 4xx/5xx, already read and closed**;
  `resp.read()` is then `b""` and the body is in `info["body"]`. The client reads `resp` only on
  2xx. Learned when every server 409 arrived as "refused: None".
- **validate-modules rules met the hard way**: `message` is a reserved argument name (hence
  `revision_message`); a `key` option needs an explicit `no_log=False`; `author` needs a
  `(@handle)`; every option in the inventory plugin's `DOCUMENTATION` needs a `description`; a
  second attribute of a doc-fragment class is referenced as `ns.coll.fragment.attr`
  (`git_cgallagher.inventory.api.write`).
- **ansible-test writes `tests/integration/inventory` and `tests/output/` next to the targets**
  when run locally; both are gitignored, as is `integration_config.yml` (it holds a real token;
  commit only the `.template`).
- **Move changes ids.** Anything that caches a node id across a move (nothing in the repo today)
  must re-resolve by path; the modules always look up by path.
- **The build output is generated and gitignored** (`server/ais/static/`); `pyproject.toml`
  package-data (`static/*`, `static/assets/*`) is what puts it into the wheel, and the Dockerfile
  asserts `ais/static/index.html` exists after `pip install`. Vite must keep writing assets flat:
  the asset route serves only files directly in `assets/` (a subfolder would 404 at runtime, and
  would also need a package-data glob).
- **React state updaters run later, after a loop has moved on.** `setExpanded(s => s.add(node.id))`
  inside a `for` loop over ancestors expanded only the last node (deep links opened a collapsed
  tree). Copy the value into a `const` first. Learned 2026-11-01 from the screenshot check.
- **Playwright roles**: an `<input list=…>` (datalist) is a `combobox`, not a `textbox`.
- **TypeScript 7 is the native (Go) compiler**; it still installs `tsc`, and the config avoids
  options it dropped (`baseUrl`, `node10` resolution).
- The Playwright smoke serves on **:8799** (not 8000, which compose and the CI plugin smoke use).
  It asserts exact revisions (1 → 4) and audit counts, so it needs a **fresh database** per run
  (serve.sh makes one; an `E2E_BASE_URL` server must be freshly seeded) and runs with no retries.
- **Node lookups by path need the `GET /nodes/{ref}` route declared with `ref: str`**: FastAPI
  would 422 `all.commercial` against an `int` path parameter. Digits = id, anything else = path.

- **Never scan `snap.nodes` per node or per host.** `Snapshot.children()` used to, and a 12k-node
  export took 24 s (110k nodes: unbounded). Use the indexes (`children`, `host`) or one pass.
- **PostgreSQL caps a statement at 65,535 bind parameters.** A 100k-element `IN` list or a
  100k-row `insert().values()` fails ("number of parameters must be between 0 and 65535"; the
  full export was a 500). Chunk (`snapshots._CHUNK`), or filter without an id list.
- **`_under()` names `node.path_ltree` as raw SQL** (the column is migration-only, not in the
  ORM model). It breaks if `Node` is ever aliased in that query; on SQLite it is an escaped
  `startswith` (`_` in a name is a LIKE wildcard).
- **An empty `path` option is not empty.** Ansible expands `type: path` `""` to the current
  directory: `AIS_ETAG_STORE=""` wrote the export into the cwd until the option became `str`.
- **`bench/compose.override.yml` uses `!override`** (Compose >= 2.24) to replace the API's
  port list; without it the ports merge and :8000 is published too.
- **SQLite batch migrations with foreign keys on delete data.** Batch mode rebuilds a table (copy,
  `DROP`, rename); the `DROP` of `node` cascaded and emptied `node_variable` (found by
  `test_0004_fills_spike_nulls`). `PRAGMA foreign_keys` is a no-op inside a transaction, so
  `init_db` sets it on the connection before `begin()`. Any new batch migration relies on that.
- **Revision ids are increasing, not contiguous on PostgreSQL.** A refused write, a no-op
  `POST /import` and an import `dry_run` roll back a `Revision` insert, which still uses up a
  sequence value. Never assume `next == current + 1` in a test (SQLite reuses the id, so a
  test that does passes locally and fails with `AIS_TEST_DATABASE_URL`).
- **Definitions and tags are not temporal, so a tree export at an old revision carries today's
  declarations and today's tag names/descriptions**, with the memberships of the nodes live at
  that revision (`DELETE /nodes` leaves `tag_member` rows, which is what lets a restore bring a
  deleted subtree's memberships back; a hard tag delete loses them). An empty tag is not exported.
- **A same-inventory clone with hosts is always refused** (host names are unique: `check_export`
  409s on the duplicate), and a cloned node keeps its `inventory_name` (a group-name collision).
  Hence `hosts=false` and the guide's "edit the file" advice. The importer also never updates
  `inventory_name` (or `kind`) on an existing node, so editing that in an export and
  re-importing is a silent no-op.
- **Deleting a `Tag` with `session.delete` on its `TagMember` rows first warns** ("expected to
  delete 1 row(s); 0 were matched"): the FK cascade removed them already. Bulk
  `delete(TagMember)` then `session.delete(tag)`.
- **The login limiter is process-global state.** `conftest.py` resets it around every test
  (autouse); a new test that logs in badly more than 5 times with one e-mail sets
  `AIS_LOGIN_MAX_FAILURES` itself. Behind a proxy every client is the proxy's address unless
  uvicorn runs with `--proxy-headers --forwarded-allow-ips`.
- **A module named `import` works** (ansible-test sanity and AnsiballZ both load it by path)
  even though it is a Python keyword; it cannot be imported with a plain `import` statement,
  which nothing needs.

## TODO (phase 4 and leftovers, see the plan)
- Phase 1/2 leftovers: OIDC is a stub that has never run against a real provider (set the
  four `AIS_OIDC_*`/`AIS_SECRET_KEY` vars and test before relying on it); no `tag` module (the API exists); the login limiter is per process (a
  multi-worker deployment multiplies the limits); `token.rejected` rows are not rate limited, so
  a client retrying a revoked token every second writes a row a second.
- Phase 3 leftovers: the OIDC button and `ui_handoff` are as unverified as the rest of OIDC; one
  ~375 kB bundle, no code splitting; tree children have no count, so every non-host node shows
  an expander until opened; no user management page (admins use `POST/PATCH /users` or the CLI);
  the node page does not list the tags a node belongs to (the Tags page does).
- Scale (`docs/scale.md`): every write re-runs the exporter over the whole tree (3.3 s at 100k). Loading the
  tree as ORM objects is 2.4 s. `GET /nodes` without `subtree` returns everything, and the UI's
  Render page host picker calls it.
- Release: no container image is published (compose builds from the checkout).
- Render cost is not bounded: a template like `{{ 'a' * 10**9 }}` (Jinja's sandbox caps
  `range`, not string repetition) allocates that much in the API process. Any viewer can send
  one. Needs a per-render time/memory limit (a subprocess) or a size check on results.
- The deploy image runs Python 3.14 and builds the UI on Node 26 (Dependabot PRs 15 and 6,
  2026-11-02; CI's `web` job and local builds still use Node 22). Never Python 3.15 while on
  ansible-core 2.21: it does not import there, and `.github/dependabot.yml` ignores python >= 3.15
  (lift that with a supporting ansible-core). CI's `image` job boots the image, so a base bump
  that builds but cannot start fails there.
- Out of phase 4's scope: RBAC beyond admin/editor/viewer. Backups: `pg_dump` plus the logical
  tree export (`ais export-tree`); there is no scheduled backup job.
- Tree export leaves out node `meta` (the import format has nowhere to put it, hosts least of
  all), and import never updates `inventory_name` on an existing node.

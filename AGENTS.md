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
  convenience = var-less tag groups only. **Tags never carry variables.** Profiles (below) are
  not a second parent: a profile's values are folded into the node it is attached to.
- **Profiles are a level between a node's parent and the node** (issue #22, `ais.profiles`,
  `api_profiles.py`). `Snapshot.layers(node)` = the attached profiles' values in the node's
  list order, then the node's own; `resolver._emitted` folds over layers, so the plugin export
  carries profile values on the node as if set there and the parity test covers them (the seed
  attaches `web-baseline`). `vars_by_node` stays **own** values only: the tree export writes
  it, and every "who contributes key k at node n" question must use `Snapshot.sets` /
  `layers` (varsearch, impact's setters, provenance), never `vars_by_node` alone. Profiles
  attach to **non-host** nodes only (the import format has no place for a host's list) and do
  not nest. All three tables (`profile`, `profile_variable`, `profile_attachment`) are
  temporal and keyed by profile **name** (the `profile` row is reopened on a description
  change); never UPDATE a value or position in place. The unit of attachment change is a
  node's ordered list (`profiles.set_node_profiles`: dense positions 0..n-1, first = lowest
  precedence); its audit row (`profile.attach`/`profile.detach`/`node.profiles`, target the
  node) holds the list before and after. Profile rows (`profile.create`/`update`/`delete`,
  target `profile`) hold the doc before and after (a delete also every detached node's list)
  and list the changed keys in `audit_key`. `move_subtree` copies attachments, `delete_subtree`
  closes them; a **restore never rolls a profile back** (`profile_values=False`: it is shared)
  and refuses (409) a profile that no longer exists. No `check_export` on profile writes
  (nothing they change names a group). `load_snapshot(paths=|keys=)` filters attachments and
  profile values the same way.
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
  `message`/`nodes`/`hosts` (`api_write._delete_guard`). `dry_run=true` deletes in its
  transaction and rolls back, like every dry run (below), so it applies the same guard. Never add a delete path that skips it
  (the module sends `recursive` only when the task says so; the UI sends the previewed count). A pinned revision therefore
  sees exactly the tree of that moment. (`inventory_name`, `meta`, variable *definitions* and
  **tags** (`tag`, `tag_member`) are the documented exceptions: not temporal, changes apply to
  every revision's export. Tag writes still lock, need `If-Match` and open a revision.)
- **Tags are var-less and named by path.** `PUT /tags/{name}` is declarative (`description`
  replaced, `members` = the complete set of node *paths* when given; ids never, a move changes
  them). **One name rule** with nodes (issue #28): `NAME_RE` (`[A-Za-z0-9_-]`), and the tag's
  group is `resolver.sanitize(name)` (`gpu-canary` → `gpu_canary`), like a node's. Reserved,
  checked on that group name: `all` and the `business_`/`tier_`/`datacenter_` prefixes (the
  automatic tag groups would silently absorb such a tag). `importer.tag_conflict_error` refuses
  (422) a tag whose group is another tag's or a live hierarchy group's (`qualified_name`,
  `inventory_name` included; default separator), **with or without members**, in
  `converge_tag` (so `PUT /tags`, its dry run and a revert) and `_import_tags`; the tag table
  never holds two tags for one group. `build_export` refuses the same collisions (tag vs
  hierarchy group, two tags for one group) as the backstop `check_export` turns into 409 for
  writes that skip that check (a node write creating a group a tag already uses). A tag with
  no live members is not emitted.
- **`json_schema` is enforced on every value write** (`ais.schema`): `PUT …/vars/{key}`,
  `POST /nodes` vars, the importer (CLI too) and `PUT /variables/{name}`, which validates the
  schema itself and re-checks every live value of the key. The schema checks each node's stored
  contribution, not the folded result. Invariant: every live value satisfies its definition.
- **Every write route has `dry_run`, and every dry run answers `impact`** (`ais.impact`, issue
  #25). A dry run is the real write with two differences only: how the revision is opened
  (`api_write.begin(dry_run=True)` / `lock_preview`: the write lock, `If-Match` checked **only
  when sent**, an `impact.Preview` taken *before* anything changes) and the tail
  (`preview_answer`: measure, `session.rollback()`, answer the base revision). Everything in
  between (schema, strict keys, `_delete_guard`, `check_export`, `review_keys`, even `_audit`)
  runs unchanged: that is the validation parity, by construction; never give a dry run its own
  code path through the checks. The body is the write's plus `dry_run: true` and `impact`; ids a
  rolled-back create/move/restore allocated are null (`phantom`); a no-op `PUT` answers
  `changed: false` with `impact.empty()`. Query parameter on the routes that had none
  (`?dry_run=true`); body field on import, restore and revert (kept). A new write route gets
  `dry_run` the same way and a case in `tests/test_impact.py`, whose oracle (export + `resolve`
  of every host before and after the real write) must match the impact host by host. The
  impact derives its scope from the database (rows with `created_rev`/`deleted_rev` = R, and
  the non-temporal state noted before vs after: strategies, tag memberships, `inventory_name`),
  never from route arguments, and resolves only the hosts and keys in scope from light rows
  with `resolver._emitted` (the varsearch rule; never a second fold). Lists cap at
  `impact.LIMIT` (200), counts never. A non-temporal table a write can change in place (like
  those three) must be added to `impact._Fixed`, or its previews will miss hosts.
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
  transaction** (`ais.audit.record`, after `check_export`). The one multi-row write is a revert:
  one ordinary row per inverse operation plus one `revert` row, all under its one revision. A refused write (409/422/428) therefore
  leaves none, and a no-op (`PUT` of an unchanged var or definition) leaves none either. The one
  deliberate exception to "raise to refuse": a failed login *returns* its 401 so the row commits.
  `before`/`after` never contain a secret (token plaintext, password hash). New write routes call
  `_audit` (or `audit.record`) or they are wrong. A dry run's rows are rolled back with it. A request that **presents** a bearer token
  that does not authenticate leaves one `token.rejected` row (reason, client, method, path),
  written in its own transaction by `auth.audit_bad_token` before the 401 is raised; its
  `target_id` is the token id, the `ais_xxxxxx` display prefix, or `(not an ais_ token)`, never
  more. Requests with no token are not recorded. A throttled login is `login.throttled` (429,
  returned like the 401).
- **Every write kind is revertible** (`ais.revert`, `POST /revisions/{id}/revert`, `POST
  /changes/{change}/revert`, `ais revert`). A revert inverts a revision's audit rows newest first
  under one new revision and records each inverse as an **ordinary** row of the forward kind
  (`variable.set`, `node.move` with `moved`, `node.restore` with `restored`, `tag.set`, …), so
  lineage, history and reverting a revert need nothing special; the `revert` summary row inverts
  to nothing. So a row's `before`/`after` must hold what its inverse needs. A new write route
  (or a new action) also goes into `revert.INVERTIBLE`, `_invert` and `footprint` and gets a case
  in `tests/test_revert.py`'s `KINDS` round trip (write, revert: tree export, plugin export,
  `meta` and every host's resolved vars equal the state before; revert the revert: equal the
  state after). Rows recorded before an action carried enough are refused (422) with the reason,
  never guessed: today, `import` rows without `before` that changed definitions or tags. The
  routes' mutations live in functions that take the revision (`move_subtree`, `delete_subtree`,
  `set_definition`, `converge_tag`, `remove_tag`, `import_tree_report(rev=)`,
  `restore_subtree(rev=)`); reuse them rather than calling a route's body. Conflicts are by
  footprint (paths, path+key, definition and tag names; a create/move/delete/restore covers the
  whole subtree): a later revision outside the reverted set that overlaps refuses with 409 unless
  `force`, and `force` never skips an inverse that cannot apply.
- **Variable search answers what a playbook sees, at the exact revision** (`ais.varsearch`,
  `GET /vars`, `GET /vars/hosts`, `ais vars`, the `variable_info` module, the UI's Search page).
  A host's value is `resolver._emitted` of the deepest node on its chain that sets the key, over
  `load_snapshot(keys=[key])`; never a second fold or merge implementation, and never the stored
  host snapshots (they lag the pass). `tests/test_varsearch.py` compares it with `resolve` host by
  host for every strategy; a resolver change that breaks that is wrong like a parity break.
  `load_snapshot(keys=)` holds only those keys: never export or `check_export` such a snapshot.
  `value=` is JSON with its type (`550.90` ≠ `"550.90"`; non-JSON text = a plain string); nodes
  match their own value, hosts the whole resolved value. Read-only: no revision, no audit row.
- **Every write that stores values reviews its undeclared keys** (`ais.keycheck.review` over
  the rows with `created_rev` = the write's revision, after `check_export`, through
  `api_write.review_keys` in the routes): `PUT …/vars/{key}`, `POST /nodes`, `POST /import` (dry
  run too), `POST /nodes/restore`, both revert routes, and `ais import`/`restore`/`revert`. The
  answer carries `warnings` + `warnings_total` (no-op `PUT`s return before and carry neither). A
  new write path that stores values calls it too. **Strict mode** (`AIS_STRICT_KEYS`, global on
  purpose: no stored flag to revert) refuses only keys live on no node before the revision, and
  **restores and reverts pass `strict=False`**: an undo must never be refused
  (`test_revert.py`'s round trip runs every kind with strict on to hold that). The "close" rule
  (declared ≤ 2 edits, widely used undeclared ≤ 1 and more used than the key, less for short
  keys) is the one `lint` uses; keep them one function (`Candidates.suggest`).
- **Audit rows know their variable.** `audit.record` sets `audit_log.key` for `node_variable`
  rows (`target_id` = `"<node id>/<key>"`, key = everything after the first `/`) and
  `variable_definition` rows (the name); migration 0007 backfilled older rows the same way.
  `GET /audit?key=` without `node` filters on it (`ix_audit_key`). A new row kind about one
  variable sets it the same way, through `audit.row_key`.
- **Change ids** (`X-AIS-Change`, module option `change`, CLI `--change`) are validated in
  `RequestIdMiddleware` (`audit.CHANGE_RE`: 1-64 of `[A-Za-z0-9._:+@-]`, no `ais_` prefix; 400
  otherwise, and the message never contains the value). They are one URL path segment as they
  are (`/changes/{change}/revert`), so never allow `/`, `#`, `?` or `%`. `begin_write` and
  `import_tree_report` put `audit.current_change()` on the `Revision`, `audit.record` on rows
  that have a revision.
- **Move/rename never updates `path` in place.** `POST /nodes/{id}/move` closes the whole subtree
  and reopens it under the new revision: **node ids change** (the response's `moved` list maps
  old → new), variables and tag memberships are copied to the new rows, host snapshots survive by
  `host_name`. The old revision still exports the old tree. Refused: the root, a move under the
  node's own subtree, `check_kind`, a live node at the new path (409), and whatever `check_export`
  rejects (qualified-name / tag collisions, duplicate host names after a host rename).
- **Modules are idempotent and locked.** Each module reads first and exits unchanged without
  touching the revision; a change sends `If-Match` from `expect_revision` or the current revision
  (`GET /revisions?limit=1`) and follows the revision each write returns. Check mode computes the
  diff from what it read (that verdict is `changed`, exactly) and asks the server for a dry run of
  each write it would make (`Client.preview`, `If-Match` only with `expect_revision`), so it
  refuses what the write would; the result's `impact` (merged by host for a task with several
  writes: `merge_impacts`) and `--diff`'s `prepared` summary (`with_impact`) come from there. Module writes carry `X-AIS-Source: module` and one `X-Request-ID` per
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
  values), never a revision fetched just before the write, which would defeat the lock. Value
  saves and removals, moves, deletes and declaration saves/undeclares preview first: the dry
  run carries the page's revision as `If-Match` (a stale page refuses there), the confirmation
  carries the **preview's** `revision`, and any edit to the form drops the preview. A stale
  409 reloads the values and keeps the user's text; the UI sends `X-AIS-Source: ui`.
- **UI views never load the whole node list** (issue #27). `GET /api/v1/nodes` without `q`,
  `kind` or `limit` returns every node (20.6 MB at 100k hosts) and stays only for the plugin's
  callers and scripts. Anything in the UI that needs to find a host or a path uses `NodePicker`
  (`web/src/NodePicker.tsx`), the bounded lookup `GET /api/v1/nodes?q=…&limit=21`
  (`ais.nodesearch`); the tree loads children per node. Deep links (`#/render?host=`,
  `?path=`, `#/diff?host=&from=&to=`) carry the name or path and need no lookup.
  `e2e/pickers.spec.ts` fails if Render or Diff ever request the unbounded list. A new picker
  uses `NodePicker`; never a `<select>`/`<datalist>` filled from a list of nodes. **The
  collection's modules don't either**: the `host` module's `find_host` (a host by name) asks
  `GET /nodes?q=<name>&kind=host` and keeps the row whose name is equal, case-sensitively
  (issue #43). It relies on tier 1 (name equal to `q`, ignoring case) coming first with the
  whole `limit`, so never reorder `nodesearch`'s tiers or cap a tier below `limit`;
  `test_exact_name_first_however_many_partial_matches` guards it.
- **Node detail reads through `POST /render {path, template: false}`**, also for hosts, so the
  page shows exactly what a mock host at that path would get. (Renders write nothing.) Own vars
  are the provenance entries whose `path` is the node's **and that have no `profile`** (a
  profile's value applied at the node is listed with its source, not as "set here"); the
  render's `profiles` (path → list) feeds the node's profile list. Ids go in no URL (a move
  changes them).
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
(`ansible-test sanity`, `ansible-test units` (no server) and the module integration targets
against a live `ais serve` on SQLite, Python 3.12; the step mints an editor token into the gitignored `integration_config.yml`) and job
`web` (`npm ci`, `npm run build` = `tsc` + Vite, then the Playwright smoke against `e2e/serve.sh`;
the report is uploaded on failure). The `image` job builds the UI too (Node stage in the
Dockerfile). All are required by the "main protection" ruleset (id `26318946`; `collection` and
`web` added 2026-11-01 after their first green runs); the "external contributor review" ruleset means your own PRs merge with
`gh pr merge --admin` once every check is green. The `test` job runs pytest twice: on SQLite and,
through a `postgres:16` service, on PostgreSQL (`AIS_TEST_DATABASE_URL`). Work on branches, never push to `main`.
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
../.venv/bin/ais vars packages --hosts --subtree all.federal   # variable search (GET /api/v1/vars[/hosts]); --value, --revision, --json
../.venv/bin/ais lint                                  # undeclared keys that look like typos (GET /api/v1/vars/lint); exit 1 when any
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
../../../../.venv/bin/ansible-test units --requirements --python 3.14   # tests/unit: module logic against a fake server, no API
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
  branch, `0003` = `audit_log`, `0004` = `node.meta` / `node_variable.value` NOT NULL, `0005` =
  `snapshot_pass`, `0006` = `revision.change` / `audit_log.change` + `ix_audit_revision`, `0007` =
  `ix_nodevar_key_live (key, deleted_rev)` + `audit_log.key` (backfilled) + `ix_audit_key`, `0008` =
`pg_trgm` + `ix_node_name_trgm`/`ix_node_path_trgm` (GIN, PostgreSQL only, migration-only like
`path_ltree`; the drift test skips them by name)). On SQLite
  `init_db` migrates with `PRAGMA foreign_keys=OFF` and runs `PRAGMA foreign_key_check` before
  turning them back on. `server/alembic.ini` is for hand use only.
- `server/ais/audit.py` — `RequestIdMiddleware` (`X-Request-ID` in/out, `X-AIS-Source` →
  `source`, both as context variables), `record(session, …)`, `audit_out`, `as_utc` (query
  datetimes: naive = UTC, so `since`/`until` work on SQLite's naive strings and PostgreSQL's
  timestamptz alike).
- `server/ais/keycheck.py` — `distance` (optimal string alignment with a limit), `Candidates`
  (declared names + undeclared keys on ≥ `AIS_TYPO_MIN_NODES` nodes, top `MAX_WIDELY_USED`;
  `suggest`), `review(session, rev_id, strict=)` → `KeyReview` (`body()`, `lines()` for the CLI)
  or `UndeclaredKeys` (`detail` = the 422 body), `lint` (`GET /api/v1/vars/lint`, `ais lint`),
  `strict_keys()`. Tests: `tests/test_keycheck.py`.
- `server/ais/varsearch.py` — `where_set` (the nodes that set a key, own value, `effective`
  fold, `above_subtree`) and `host_values` (each host's resolved value, `from`/`sources`, counts
  and the `values` distribution), `parse_value`/`Wanted` (the `value=` rule), `canonical`.
  `GET /api/v1/vars` and `/vars/hosts` in `api.py`, `cli._vars` the CLI twin.
- `server/ais/nodesearch.py` — `search(session, q, kinds=, subtree=, revision=, limit=)`: the
  pickers' lookup behind `GET /api/v1/nodes?q=` (tiers exact name, name prefix, path prefix,
  name substring, path substring; each one `ILIKE … ORDER BY path LIMIT n`, run only while the
  answer is short; `%`/`_` escaped). Live filter `deleted_rev IS NULL` without a revision (the
  partial indexes' predicate), `_live_at` with one. Tests: `tests/test_nodesearch.py`; timings
  and plans: `docs/scale.md` "Node lookup".
- `server/ais/resolver.py` — `fold`, `load_snapshot(revision, subtree, paths=, keys=)` (revision-aware
  for nodes and vars; subtree and paths filter in SQL, the subtree through `path_ltree` on
  PostgreSQL), `load_chain(revision, host=|path=)` (just the ancestor chain, which is all
  `resolve` needs), `build_export`, `resolve(host|path, overrides)`. `Snapshot` (also built over light rows by `ais.impact`) indexes children
  and hosts on first use and caches each node's emitted vars (`_emitted`): shared, never mutate.
  Not-found is a `KeyError("unknown path 'all.x'")`; turn one into an HTTP detail or CLI error
  with `key_error_message(exc)`, never `str(exc)` (that quotes it twice, issue #49).
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
- `server/ais/profiles.py` — profile mutations shared by routes, importer and revert:
  `set_profile(session, rev, name, wanted|None, check=)`, `set_node_profiles`,
  `delete_profile(detach=)`, `doc(name, rev)`, `attachments(name, rev)`, `node_list`,
  `attach_action`, `changed_keys`, `ProfileError(status, detail)`. `server/ais/api_profiles.py`
  — the routes (`/profiles`, `/profiles/{name}/vars/{key}`, `/nodes/{id}/profiles[/{name}]`).
  Tests: `tests/test_profiles.py` (incl. the issue's proof estate and its real-plugin parity).
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
- `server/ais/revert.py` — `revert(session, revisions, actor=, source=, force=, hosts=,
  change=)` → `RevertResult`, `RevertError(status, detail)`, `footprint`, `find_conflicts`,
  `revisions_of_change`. `api_revert.py` holds the two routes (dry run = apply + rollback, like
  restore's); `cli._revert` the CLI twin. `hosts=True` (the dry run) takes an `impact.Preview`
  before inverting; `RevertResult.impact`, and `hosts_changed`/`hosts` (= `impact.total` /
  `impact.hosts`, kept for older clients).
- `server/ais/impact.py` — the blast-radius preview: `Preview(session)` (after the lock, before
  the write: base revision + `_Fixed`, the non-temporal state), `.impact(rev)` → the `impact`
  dict; `empty()`, `summary()` (CLI line), `LIMIT`. Internals: `_scope` (what R touched),
  `_rows` (light `_Row`s: every non-host node + the hosts in scope; shared by both sides unless
  R opened or closed nodes), `_capture` (a `resolver.Snapshot` over light rows, per host the
  deepest setter of each key and the groups), `_diff` (memoized per setter pair, exact counts,
  `heapq.nsmallest` for the listed hosts). `api_write.begin`/`answer`/`preview_answer`/
  `lock_preview`/`unchanged`/`phantom` are the route side. Tests: `tests/test_impact.py`.
- `server/ais/importer.py` — YAML tree → nodes/vars under one revision; kind inferred
  from depth unless declared; rejects bad kind order and names, values their schema rejects.
  `import_tree_report` returns an `ImportResult` (counts, `tags_changed`, `changed`, `summary()`;
  `tags:` = name → description + member paths, additive, after the tree; `tag_name_error` is
  shared with `PUT /tags`, and so is `tag_conflict_error` (over `hierarchy_groups`, computed
  once per import); `before()` = the changed definitions and tags as they were, which the
  `import` audit row records as its `before` for revert; `definition_doc`/`tag_doc` are the
  shapes the API and audit rows use); `POST /import` rolls back a
  no-op (200, `changed: false`, no revision, no audit row) and a `dry_run` (no `If-Match` needed).
- `collection/.../plugins/inventory/db.py` — the plugin (`fetch_export` does the ETag
  revalidation; `etag_store` option). Accepts `inventory.yml` / `ais.yml` file names.
- `collection/.../plugins/module_utils/client.py` — `Client` (token/url from options or
  `AIS_TOKEN`/`AIS_URL`, `change` from the option or `AIS_CHANGE` sent as `X-AIS-Change` on every
  request (the option is in the shared `api` fragment, so a group `module_defaults` works for info
  modules too), `request(keep_body=)` returns an allowed status's `detail`, `request`, `write` with `If-Match`, `write_or_stale` (None on a stale 409 so the caller can re-read and retry once; `write` when `expect_revision` is set), 409 stale vs other 409, 401/403),
  `ensure_node`/`remove_node` (shared by `node` and `host`; `remove_node(recursive=)`, check
  mode = the server's delete `dry_run`). A structured 409 detail's `message` becomes the task's
  `msg`, its other keys (`nodes`, `hosts`, …) the result's. `doc_fragments/api.py` holds the
  `api` and `api.write` fragments; `meta/runtime.yml` declares the `inventory` action group
  (`module_defaults: group/git_cgallagher.inventory.inventory`).
- `collection/.../plugins/modules/` — `profile` (`vars`, `purge_vars`, `attach`/`detach` paths
  appended last in a node's list, `state`, `detach_all`) and `profile_info`; `node`, `host`, `variable`, `variable_definition`,
  `tag` (`paths` added, `exclusive` = exactly them, `remove_paths`, `state`; description kept
  when omitted; the one module that retries: a stale 409 re-reads the tag and writes once more
  only if it still differs, through `Client.write_or_stale`, never with `expect_revision`),
  `node_move` (path → new_path; done when only new_path is live), `import` (tree dict; check mode =
  server dry run), `revert` (`revision` or `revert_change`, `force`; dry run first: check mode
  stops there, 409 `already reverted` = not changed) (write, check + diff mode), `node_info`, `render_info`, `tag_info` (one tag or the list), `variable_info` (`/vars` + `/vars/hosts`, pages up to `limit`; its `value_counts` is the API's `values`, a name Jinja would resolve to the dict method). `tests/integration/targets/<module>` = check,
  real, repeat, failure cases with an `always:` cleanup, all against one shared server (unique
  `it_*`/`it-*` names per target). `tests/unit/plugins/modules/test_<module>.py` = what a live
  server can't provoke on demand: `test_tag.py` runs `tag.main()` against a fake server patched in
  at `client.fetch_url` (the real `Client.request` still parses every answer) whose "concurrent
  writer" moves the revision between the module's read and its write, for the stale-409 retry
  (once, then fail; never on a non-stale 409 or with `expect_revision`). Args go in through
  `ansible.module_utils.testing.patch_module_args` (ansible-core 2.19+), and `exit_json`/
  `fail_json` are patched to raise. `tests/sanity/ignore-2.*.txt` only waive the GPLv3-header
  check (the collection is MIT).
- `server/ais/api.py` (end) — `web_dir()` (`AIS_WEB_DIR` or `ais/static`), `GET /`, `GET /assets/…`;
  `/healthz` reports `oidc`. `ais/oidc.py` `ui_handoff()`: with `/login?ui=1` the callback answers
  with a page that puts the session into `sessionStorage['ais.session']` and goes to `/`.
- `web/` — Vite + React 19 + TypeScript, no component library (deps: `react`, `react-dom`,
  `yaml`). `src/api.ts` (fetch wrapper: bearer, `If-Match`, `X-AIS-Source: ui`, `ApiError.stale`
  for the 409 body `{error, expected, current}`, any 401 → login), `src/router.ts` (hash routes),
  `src/views/` (`Login`, `Tree` (lazy children, ARIA tree keys), `NodeDetail`, `NodeActions`
  (add child, move/rename, delete), `VarEditor`, `Render`, `Diff`, `Revisions` (change filter,
  Revert panel: dry run, then `If-Match` = the dry run's revision), `Audit` (`key` alone, `subtree`),
  `NodePicker.tsx` (the node typeahead: ARIA combobox + listbox, 150 ms debounce, newest answer
  wins, `pick` = name or path, `NON_HOST_KINDS`; used by Render, Diff, the move dialog, Search's
  subtree and the audit filters),
  `Profiles` (`#/profiles[?name=]`: list, values with `VarEditor` (`url` prop), attach through
  `NodePicker`, detach, delete; every write previewed), `NodeDetail`'s `NodeProfiles` (attach
  from the profile list, detach; previewed), `SourceLink`/`SourceLabel` (`profile:x @ path`),
  `Search` (`#/search?key=&value=&subtree=&revision=`: where set + host values; the value box is YAML, sent as JSON; reached from the find box above the tree, "where set" on the Variables and node pages),
  `Variables` (definitions: strategy, description, JSON Schema), `Tags`, `Tokens`), `src/styles.css` (tokens, dark via
  `prefers-color-scheme`). `vite.config.ts` sets `outDir: ../server/ais/static`.
  `e2e/smoke.spec.ts` + `e2e/pickers.spec.ts` (read-only, runs first: the smoke counts
  revisions from 1) + `e2e/z-profiles.spec.ts` (runs last, writes, pins no revision or
  count the smoke can change) + `e2e/serve.sh` + `playwright.config.ts`.
- `server/tests/conftest.py` — `engine` (SQLite or `AIS_TEST_DATABASE_URL`), role tokens,
  `AuthClient` (sends the bearer, `if_match=` kwarg), live `server` + real-plugin helpers.

## ⚠️ Gotchas
- **A restore exports profiles at the old revision, which a plain import would roll back.**
  Profiles are temporal and shared, so `restore_subtree` imports with `profile_values=False`
  (the block only has to name live profiles) and refuses a profile deleted since. Anything new
  built on `export_tree` + import of a *subtree* into a live tree needs the same care.
- **`vars_by_node` is own values, not contributions.** With profiles a node can contribute a
  key it does not set itself; `key in snap.vars_by_node[n]` misses it (it broke provenance with
  a KeyError and would have hidden profile values from search and previews). Use
  `Snapshot.sets(node_id, key)` / `layers(node_id)`.
- **`GET /vars/hosts?value=570.86` is the number 570.86.** A driver version stored as a
  string needs `value="570.86"` (quoted JSON); the proof test tripped on it.
- **A dry run must take its `impact.Preview` before the first change.** The non-temporal state
  (strategies, tag memberships, `inventory_name`) is changed in place and cannot be read "as of
  the base revision" afterwards; nodes and values can (temporal rows). A preview taken late
  reports nothing for a strategy or tag change. Restore, import and revert open their own
  revision inside the call, so their routes take the preview first and pass `result.revision.id`.
- **A move re-creates every value row of its subtree**, so "keys whose rows R opened or closed"
  includes every key the subtree sets. The impact needs that (a revert can move a subtree back
  and change a value inside it in one revision; ignoring rows on moved nodes missed exactly that
  in `test_change_set_revert_undoes_many_revisions_as_one`), and it is bounded by the subtree.
- **`impact` compares values as canonical JSON** (`varsearch.canonical`): in Python `1 == True`,
  so `==` would hide a change from `1` to `true`.
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
  `check_export`. **On PostgreSQL the rolled-back id stays used** (sequences are not
  transactional), so every refusal raised after `begin`/`begin_write` or the importer's or
  revert's own `Revision(...)` insert, and every no-op import (API and CLI insert first, then
  roll back), skips one id; SQLite's `revision.id` is a rowid alias without AUTOINCREMENT and
  reuses it. Documented in the guide's section 13 (issue #50, measured 2026-11-06),
  pinned by `test_refused_writes_skip_revision_ids_only_on_postgresql`. Never compute a
  revision as `id - 1`/`id + 1` except under the write lock (`impact.Preview.revision`).
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
- **Playwright roles**: an `<input list=…>` (datalist) is a `combobox`, not a `textbox`; so is
  `NodePicker`'s input (`getByRole("combobox", { name: "Host" })`), and its options' accessible
  names are the picked value (`option`, `{ name: "web02", exact: true }`; the path for path
  pickers). Its live region is `aria-live`, not `role="status"`: the pages' notices own that
  role and tests use `getByRole("status")` in strict mode.
- **Playwright runs spec files in name order on one server** (`workers: 1`):
  `pickers.spec.ts` runs before `smoke.spec.ts`, so it must not write.
- **`pg_trgm` cannot serve a one- or two-character substring** (no trigram to look up): those
  tiers of a lookup that matches nothing scan the table (73 ms at 100k hosts). Prefixes are
  fine either way (path-order index walk). Measured 2026-11-05.
- **TypeScript 7 is the native (Go) compiler**; it still installs `tsc`, and the config avoids
  options it dropped (`baseUrl`, `node10` resolution).
- The Playwright smoke serves on **:8799** (not 8000, which compose and the CI plugin smoke use).
  It asserts exact revisions (1 → 4) and audit counts, so it needs a **fresh database** per run
  (serve.sh makes one; an `E2E_BASE_URL` server must be freshly seeded) and runs with no retries.
- **Node lookups by path need the `GET /nodes/{ref}` route declared with `ref: str`**: FastAPI
  would 422 `all.commercial` against an `int` path parameter. Digits = id, anything else = path.

- **`node_variable.value` is `JSON`, not `JSONB`, on PostgreSQL**, and `json = json` has no
  operator there: a SQL `NodeVariable.value == x` passes on SQLite and fails on PostgreSQL. The
  variable search therefore filters `value=` in Python after the indexed key lookup.
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
- **Revision ids are increasing, not contiguous on PostgreSQL.** A refused write and a no-op
  `POST /import` roll back a `Revision` insert, which still uses up a sequence value. Dry runs
  don't: `impact.Preview.revision` sets the id to `base + 1` explicitly (safe under the write
  lock), so a preview never makes the next revision number jump. Never assume `next == current + 1` in a test (SQLite reuses the id, so a
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
- **A templated play var is re-evaluated on every use.** `vars: {id: "x-{{ '%s' | strftime }}"}`
  gave each task of the `revert` integration target a different change id. Pin such values with
  `set_fact` first.
- **Ansible reserves a module result's `warnings`** for plain strings (each is passed to
  `module.warn`; a dict there breaks). The server's structured `warnings` therefore reach module
  results as `key_warnings`, and `Client.collect` calls `module.warn(message)` once per entry.
  A registered result's `warnings` list does hold those messages (the `variable` target asserts it).
- **`select(Node.path).join(Node, …)` with the filter on `NodeVariable` fails** ("Don't know how
  to join to Node"): the select's only entity is the join target. Start from the variable table
  with `.select_from(NodeVariable)`.
- **Running a module's `main()` outside Ansible (a one-off harness) needs a serialization
  profile** in ansible-core 2.19+: set `basic._ANSIBLE_ARGS` (the `ANSIBLE_MODULE_ARGS` JSON)
  *and* `basic._ANSIBLE_PROFILE = "legacy"`, or `AnsibleModule` raises "No serialization profile
  was specified". `ansible.module_utils.testing.patch_module_args` sets both; the unit tests
  (`tests/unit/plugins/modules/test_tag.py`, the `tag` module's stale retry, which integration
  can't race) use it.
- **A module named `import` works** (ansible-test sanity and AnsiballZ both load it by path)
  even though it is a Python keyword; it cannot be imported with a plain `import` statement,
  which nothing needs.

## TODO (phase 4 and leftovers, see the plan)
- Phase 1/2 leftovers: OIDC is a stub that has never run against a real provider (set the
  four `AIS_OIDC_*`/`AIS_SECRET_KEY` vars and test before relying on it); the login limiter is per process (a
  multi-worker deployment multiplies the limits); `token.rejected` rows are not rate limited, so
  a client retrying a revoked token every second writes a row a second.
- Phase 3 leftovers: the OIDC button and `ui_handoff` are as unverified as the rest of OIDC; one
  ~375 kB bundle, no code splitting; tree children have no count, so every non-host node shows
  an expander until opened; no user management page (admins use `POST/PATCH /users` or the CLI);
  the node page does not list the tags a node belongs to (the Tags page does).
- Scale (`docs/scale.md`): every write re-runs the exporter over the whole tree (3.3 s at 100k);
  so does its dry run. `api_write.live_subtree` (move, delete) reads every live node as ORM
  objects to pick the subtree (~2 s at 100k), where `resolver._under` would read only the
  subtree. `PUT`/`DELETE /variables/{name}` and `DELETE …/vars/{key}` run no `check_export`
  (nothing they change can collide). Loading the
  tree as ORM objects is 2.4 s. `GET /nodes` without `q`/`limit`/`subtree` returns everything
  (neither the UI nor any module calls it; the `host` module's `find_host` uses the bounded
  lookup since issue #43). The unbounded list also loads every variable (`load_snapshot`)
  only to drop them.
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

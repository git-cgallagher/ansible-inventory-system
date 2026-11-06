# Changelog

## Unreleased

### Added
- **Profiles (role templates)** (#22, 80% of group variables were copies of the same role
  settings). A profile is a named bundle of variables (`gpu-a100: {gpu_driver: "560.35",
  node_labels: {gpu: a100}}`) attached to any number of non-host nodes. At each node it applies
  as a level between the node's parent and the node: below the node's own values, above what
  the node inherits, with the same merge strategies; several profiles on one node apply in the
  node's list order (first = lowest precedence, the last wins). No nesting in this version.
  The plugin export carries the values on the node as if they were set there, so playbooks
  need nothing new (the parity test covers profiles: the seed attaches `web-baseline`).
  - API: `GET /api/v1/profiles[/{name}?revision=]`, `PUT /profiles/{name}` (declarative
    `description` and `vars`), `PUT`/`DELETE /profiles/{name}/vars/{key}`, `DELETE
    /profiles/{name}` (409 while attached unless `detach=true`), `PUT /nodes/{id}/profiles`
    (the node's ordered list), `PUT`/`DELETE /nodes/{id}/profiles/{name}` (attach at the end or
    at `position`; detach). All with `If-Match`, `dry_run` + `impact` (a value change lists
    every host under every node the profile is attached to; it resolves only the touched keys:
    0.78 s for a profile attached over all 100k bench hosts) and change ids; one audit row
    each (`profile.create`/`update`/`delete`, `profile.attach`/`detach`, `node.profiles`), every
    one reverted exactly by `POST /revisions/{id}/revert`.
  - History: profiles, their values and attachments are temporal rows (migration `0009`), so
    a pinned revision renders and exports them as they were.
  - Provenance: the render lists a profile's value as `{path, profile, source:
    "profile:gpu-a100 @ <path>"}` and adds `profiles` (path → list along the chain); `GET
    /nodes/{ref}` returns the node's `profiles`.
  - Variable search: `GET /vars` lists each attachment that carries the key (`profile`,
    `source`) and `profiles`, the profiles that hold it; `/vars/hosts` adds `profile`/`source`
    when a host's value comes from one and names profile layers in a fold's `sources`.
  - Audit: profile rows record the variables they changed in the new `audit_key` table, so
    `GET /audit?key=` finds them.
  - Schemas, typo warnings and strict mode check profile values like node values
    (`profile:<name>` in messages); `PUT /variables/{name}` re-checks profile values too.
  - Import/export: a `profiles:` block (created if missing, values set additively) and
    `profiles: [names]` on non-host nodes (the list, set exactly). Tree exports carry them
    (every profile for the whole tree; the attached ones for a subtree). Moves carry
    attachments, deletes close them, and a restore re-attaches them without rolling the shared
    profile back (409 when one was deleted since).
  - Modules `profile` (values, `purge_vars`, `attach`/`detach` paths, `state`, `detach_all`;
    check mode = the server's dry run with `impact`) and `profile_info`, with integration
    targets; in the `inventory` action group.
  - UI: a Profiles page (list, create, values with the same editor and preview as a node,
    where it is attached, attach through the node picker, detach, delete; every write
    previewed), a Profiles section on node pages (attach and detach with previews), and
    profile sources in provenance and on the Search page. Playwright `e2e/z-profiles.spec.ts`.
  - `ais vars` prints profile sources. `bench/generate.py --profiles` and `benchmark.py
    --profile/--narrow-profile` measure them (`docs/scale.md`).
- **`tag` and `tag_info` modules** (#28, no tag module). `git_cgallagher.inventory.tag`
  manages a tag from a playbook: `name`, `description` (kept when omitted), `paths` (added to
  the members by default; with `exclusive: true` the members become exactly `paths`),
  `remove_paths`, `state: absent`, plus the shared `api`/`write` options (`change`,
  `expect_revision`, `revision_message`). It reads first and changes nothing when the tag
  matches; it handles `If-Match` itself and, on a stale 409, reads the tag again and retries
  once, only when the tag still differs and `expect_revision` was not given. Check mode is the
  server's dry run of `PUT`/`DELETE /tags/{name}`; `--diff` shows the members before and after
  plus the impact summary. `tag_info` reads one tag or lists them. Both are in the
  `inventory` action group. The `import` integration target uses them instead of `uri`.

- **Node lookup for pickers** (#27, Render and Diff downloaded every node). `GET
  /api/v1/nodes?q=<text>&kind=host&limit=20` returns at most `limit` (1–100, default 20) live
  nodes whose name or path contains `q`, ignoring case, ranked exact name, name prefix, path
  prefix, name substring, path substring (path order within each tier; `%` and `_` match
  themselves). `kind` repeats; `subtree` and `revision` work as on the plain list. Each tier is
  a `LIMIT`ed query that runs only while the answer is short (`ais.nodesearch`). Migration
  `0008` adds `pg_trgm` and trigram GIN indexes on `node.name` and `node.path` on PostgreSQL (a
  no-op on SQLite): 4–10 ms per lookup at 100k hosts, 73 ms for a one-letter miss
  (`docs/scale.md`). Without `q`, `kind` or `limit` the route lists every node as before.

- **Blast-radius preview: `dry_run` with `impact` on every write** (#25, blast-radius preview).
  `?dry_run=true` on `POST /api/v1/nodes`, `PATCH /nodes/{id}`, `POST /nodes/{id}/move`,
  `PUT`/`DELETE /nodes/{id}/vars/{key}`, `PUT`/`DELETE /variables/{name}` and
  `PUT`/`DELETE /tags/{name}`, next to the existing ones (`DELETE /nodes/{id}`, `POST /import`,
  `POST /nodes/restore`, the revert routes). A dry run runs the write in its transaction with
  every check the write runs (schema, strict mode, the delete guard, the export check), answers
  the same body plus `impact`, and rolls back: no revision, no audit row. It needs no
  `If-Match`; one that is sent must be current (409), and the real write sent with `If-Match` =
  the preview's `revision` refuses if anything changed in between. `impact` has one shape for
  every write: `total` distinct hosts affected; `vars_changed`, `added`, `removed`,
  `regrouped`, `moved`; `hosts` sorted by path with the keys `added`/`removed`/`changed`
  (`from`/`to`), the groups `joined`/`left` and `moved_from`; `groups` with how many hosts
  join or leave each; lists capped at `limit` (200, `truncated`), counts exact. One helper
  (`ais.impact`) serves every route and the CLI, and is bounded: it resolves only the hosts and
  keys the write can change, from light rows (0.06 s at a datacenter, about 1 s at `all` with
  every one of 100k hosts changing; `docs/scale.md`). The **UI** shows "This changes N hosts"
  (with the list on demand) before saving or removing a value, a move, a delete and a
  declaration change or undeclare, and saves with `If-Match` = the preview's revision; the
  typo hint shows in the preview too. **Module check mode** is the server's dry run in the
  `variable`, `variable_definition`, `node`, `host`, `node_move`, `import` and `revert` modules
  (so check mode now refuses what the write would, and warns about likely typos); they return
  `impact`, and `--diff` prints a summary (counts plus the first five hosts). `ais import`,
  `ais restore` and `ais revert` print it with `--dry-run`.

- **Typo warnings and strict mode** (#24, warn on undeclared keys that look like typos). A write
  that stores an undeclared key close to a known one still succeeds and answers `warnings`
  (`key`, `path`, `did_you_mean`, `declared`, `message`) and `warnings_total`: `PUT
  /api/v1/nodes/{id}/vars/{key}`, `POST /nodes` (its `vars`), `POST /import` (dry runs too; one
  entry per key and path, every key before any key's second path, at most 50), `POST
  /nodes/restore` and both revert routes. Known keys are the declared ones (close = up to 2
  edits after lower-casing and reading `-` as `_`; optimal string alignment, so a swap of two
  neighbours is 1) and undeclared keys set on at least `AIS_TYPO_MIN_NODES` nodes (default 5;
  `0` = off) and on more nodes than the new key (up to 1 edit). Short keys get less slack (1
  edit up to 6 characters, none up to 3). The `variable`, `node`, `host`, `import` and `revert`
  modules raise each as an Ansible warning and return `key_warnings`; `ais import`, `ais
  restore` and `ais revert` print them on stderr; the UI's node page shows the hint after a
  save. **`AIS_STRICT_KEYS=true`** refuses keys that are neither declared nor set on any node
  yet (422 `undeclared key`, with the suggestion and how to declare one); existing undeclared
  keys keep working, and restores and reverts are never refused. **`ais lint`** (exit 1 when
  it finds any) and `GET /api/v1/vars/lint` (viewer) list the likely typos already in the tree.
- **Variable search** (#23, variable search). `GET /api/v1/vars?key=` (viewer) lists the nodes
  that set a key in their own variables, by path, with their values, `effective` (the fold each
  emits) for merge/append keys, and with `subtree` the ancestors that set it (`above_subtree`).
  `GET /api/v1/vars/hosts?key=` gives every host's resolved value (what a playbook sees) with
  `from` / `sources`, counts over the whole scope (`total`, `with_key`, `without_key`) and the
  distribution of values. Both take `value` (JSON with its type: `550.90` is not `"550.90"`;
  text that is not JSON is a plain string; hosts match the whole resolved value, the fold for
  merge/append keys), `subtree`, `revision`, `limit` and `offset`. Also `ais vars KEY [--hosts]
  [--value] [--subtree] [--revision] [--json]`, the `variable_info` module, and a Search page in
  the UI, reached from a find box above the tree and a "where set" link on every variable of
  the Variables page and of a node page. 0.02 s for where a key is set and 1.3–1.7 s for every
  host's value at 100,000 hosts, 0.03 s for a datacenter (`docs/scale.md`).
- `GET /api/v1/audit?subtree=`: only rows whose recorded path (as written then) is at or below it.
- **Change ids.** Writes can carry a change id: the `X-AIS-Change` header (1-64 characters from
  letters, digits and `._:+@-`; a bad one, or one that looks like an `ais_` token, is a 400 that
  never repeats the value), the `change` option on every module (or `AIS_CHANGE`), and `--change`
  on `ais import`, `ais restore` and `ais revert`. Revisions and their audit rows record it
  (migration `0006`), `GET /api/v1/revisions?change=` and `GET /api/v1/audit?change=` filter by
  it, and the UI's Revisions and Audit pages show and filter by it (#21, revert and change sets).
- **Revert.** `POST /api/v1/revisions/{id}/revert` undoes one revision and `POST
  /api/v1/changes/{change}/revert` every revision of a change id, as one new revision (editor,
  `If-Match`, 201; `force`, `dry_run`, `message`). The server applies the inverse of each audited
  write, newest first, and records each as an ordinary audit row plus one `revert` row, so node
  and key history follow it and a revert can itself be reverted. Every write kind reverts
  exactly: variable set and unset, node create, patch, move, delete (through the restore) and
  restore, variable definitions, tags and imports. A later revision outside the reverted set
  that changed the same node or subtree, key, definition or tag refuses it with 409 and the
  list (`conflicts`) unless `force`; an inverse that cannot apply now is refused either way;
  something already reverted answers 409 `already reverted`. `dry_run` (no `If-Match`) lists the
  operations and the hosts whose resolved variables would change. Also `ais revert --revision N
  | --of-change ID [--force] [--dry-run]`, the `revert` module (check mode = the dry run; already
  reverted = not changed), and a Revert button on each revision and on a change in the UI, which
  shows the dry run before it writes (#21).
- CI runs the whole server test suite on PostgreSQL 16 too (a service container in the `test`
  job), not only on SQLite.
- `docs/getting-started.md`: a getting-started guide, from the ideas through a PostgreSQL
  deployment (TLS, backups, upgrades), tree design, every write path, secrets, roles, history,
  settings, limits and troubleshooting. Each step was run against 0.3.0.
- `ais import --dry-run`: reports what an import would change and writes nothing.
- Tree export in the import format: `GET /api/v1/tree` (viewer; `subtree`, `revision`, `as`,
  `ancestor_vars`, `hosts`, `format=json|yaml`; ETag and `X-AIS-Revision`) and `ais export-tree`
  (`--subtree`, `--revision`, `--as`, `--ancestor-vars`, `--no-hosts`, `--format`). It returns
  the nodes with their kinds and own values, the variable declarations they use and the tags,
  exactly as `ais import` / `POST /api/v1/import` read them, so backup, restore (export a
  deleted subtree at an earlier revision and import it), cloning (`as` re-roots the copy) and
  keeping the tree in Git each take one call instead of a scraping script (#26, tree export).
  Stored Jinja is returned verbatim. Node `meta` is not exported.
- The import format takes `tags:` (tag name → `description`, `members` as node paths). Additive
  like the rest of an import: it creates a tag, sets its description when given and adds
  members, and never removes one. `POST /api/v1/import`, `ais import`, the audit row and the
  `import` module report the changed tags as `tags`.
- `deploy/compose.prod.yml`: production overrides for compose (database password from
  `AIS_DB_PASSWORD`, API on loopback for a TLS proxy, `FORWARDED_ALLOW_IPS: "*"`, restart
  policies).
- The inventory plugin warns whenever it answers from Ansible's inventory cache (`cache:
  true`) instead of the server: "inventory revision N comes from Ansible's inventory cache".
  That cache is keyed by the file's path only, so it used to serve hosts up to `cache_timeout`
  old without a word, with the server down or a new `revision:` pinned (#19, fail-closed
  inventory).

- **Restore a deleted subtree in one call**: `POST /api/v1/nodes/restore` (editor, `If-Match`;
  `path`, optional `revision`, `dry_run`, `message`) and `ais restore PATH [--revision N]
  [--dry-run] [--message]`. The subtree comes back as it was at that revision (default: the
  last one before its latest delete) with its nodes, own values, node `meta` and tag
  memberships, under one new revision and one `node.restore` audit row. Built on the tree
  export + import in-process, so ancestors are never rolled back. Refused, writing nothing: the
  root (422), a live node at the path (409: a restore never overwrites), a parent that is gone
  too (409: restore the highest deleted ancestor), a host name now used elsewhere (409), a
  value a tightened definition rejects (422) (#20, delete guard and restore).
- **Audit history survives a delete and restore.** The restored nodes are new rows (a node row
  is live over one span of revisions, so reviving the old ones would make the revisions in
  between show them), and the `node.restore` row maps every old id to its new one, like a
  move's. `GET /api/v1/audit?node=<path>[&key=]` and the UI's history links follow that map,
  so they show the changes from before the delete; the restored root's history shows the delete
  and the restore. Host snapshots continue by host name: `/diff` from before the delete to
  after the restore is identical, and revisions in between answer 404 for those hosts.
- `DELETE /api/v1/nodes/{id}?dry_run=true` lists what a delete would take (`deleted`, `nodes`,
  `hosts`) and writes nothing (no `If-Match`); every delete answer now carries `nodes` and
  `hosts`.
- The `node` module's `recursive` option (with `state: absent`); check and diff mode list every
  path that would be deleted.

### Changed
- `POST /api/v1/import` (and `ais import`) answers two more counts, `profiles` and
  `attachments` (#22). `examples/seed.yml` declares and attaches a profile (`web-baseline`).
- **One name rule for tags and nodes** (#28, tag and node names differed). Tag names now
  allow `-` like node names (`[A-Za-z0-9_-]`, up to 255), and the tag's group turns it into `_`
  the way a node's group does (`gpu-canary` is the group `gpu_canary`), so a tag can be named
  after the node it tracks. A tag whose group would already exist is refused with **422** when
  it is written (`PUT /tags/{name}`, its dry run, a revert that recreates it) or imported:
  another tag with the same group (`gpu-canary` next to `gpu_canary`) or a live hierarchy
  group (`commercial-prod` or `commercial_prod` while `all.commercial.prod` exists,
  `inventory_name` included), with or without members. Before, a tag named like a hierarchy
  group was the export check's **409**, and only once it had members. The reserved names
  (`all`, the `business_`/`tier_`/`datacenter_` prefixes) are checked on the group name, so
  `tier-prod` is reserved too. The export refuses two tags with one group (409) as a backstop
  for writes that skip the tag check, instead of merging them silently. No migration: no stored
  tag can hold a `-`. One edge: an *empty* tag named like a hierarchy group, which the old
  export check let through, now refuses any change (422) until it is deleted (`DELETE` still
  works) or the group goes away. The UI's Tags page accepts the new names and shows the group
  when it differs.

- **Dry runs take the write lock and write in their transaction** (#25). `DELETE /nodes/{id}`'s
  `dry_run` was a pure read; it now deletes and rolls back like the others, so it can report
  `impact`. Dry runs of import, restore and revert lock too, so the preview and its `revision`
  are consistent. Every write's answer has `dry_run` (`false` for a real write).
- **The revert dry run no longer resolves every host** (#25): its `hosts`/`hosts_changed`
  (kept, now `impact.hosts`/`impact.total`) come from `ais.impact` instead of resolving every
  host under the reverted paths (the whole tree when a definition was involved). They now also
  list hosts whose groups change, and the host list is sorted by path.
- **`GET /api/v1/audit?key=` works without `node`** (it answered 422 "key needs node"): the
  key's `variable.set` / `variable.unset` rows on every node and its definition's rows, newest
  first. Migration `0007` adds `audit_log.key` (set on every new variable or definition row,
  backfilled from `target_id` for older ones) with an index, and an index on
  `node_variable (key, deleted_rev)` for the search. Audit rows now include `key`. Rows that
  change many keys at once (`import`, `node.create` with variables, a subtree delete or restore)
  are not found by key (#23).
- The `import` audit row's `before` now records the definitions and tags the import changed, as
  they were, and a `tag.delete` row's `before` the memberships of deleted nodes
  (`deleted_members`), so both can be reverted. An `import` row written by an earlier version
  that changed definitions or tags cannot be reverted (422, saying why); a tag deleted by an
  earlier version is reverted without those memberships.
- Audit rows carry `change` (null without a change id), and revision rows in `GET
  /api/v1/revisions` carry `change`.
- **Breaking: deleting a node that has descendants needs a confirmation.** `DELETE
  /api/v1/nodes/{id}` used to delete the whole subtree silently (one editor call removed 45
  nodes, 21 of them hosts). It now answers 409 (`error: "subtree not empty"`, with `message`,
  `nodes` and `hosts`) unless the request says `recursive=true` or gives `expect_count` equal to
  the number of nodes deleted, the node included; a wrong `expect_count` is a 409 `count
  mismatch` even with `recursive`. A host or an empty group deletes as before. Callers that
  delete non-empty groups must add `?recursive=true`; the `node` module with `state: absent`
  must add `recursive: true` (the task fails with the counts otherwise, in check mode too). The
  web UI's Delete dialog shows the node and host counts and sends them as `expect_count`.
- The modules and the web UI show a structured error's `message` instead of its raw JSON.
- The server requires ansible-core >= 2.19 (it already needed 2.19's trusted-template API to
  run; now pip refuses an older one). The collection still supports ansible-core 2.18.
- `.gitignore` ignores `*.token`.
- `examples/ansible.cfg`, the getting-started guide, the README and the plugin's docs set
  `any_unparsed_is_failed = True` under `[inventory]`. Without it a playbook whose inventory
  fails to load (server down, bad token, unknown revision pin) exits 0 having run on no hosts.
  Under `[defaults]` Ansible ignores the key silently.

### Fixed
- **The UI's Render and Diff pages no longer download every node** (#27). Their host `<select>`
  and path list loaded the whole `GET /api/v1/nodes` (2.3 MB for a 3,000-host generated tree,
  about 21 MB at 100k hosts) on every open. Both now use a typeahead (an ARIA combobox:
  Down/Up, Enter, Escape, mouse; the path under each name) over the new bounded lookup: opening
  Render on that tree now costs 634 bytes of API responses instead of 2,315,214, and a lookup
  about 2 kB. Deep links (`#/render?host=…`, `#/render?path=…`, `#/diff?host=…&from=…&to=…`)
  render without any lookup. The same picker now suggests nodes for the move dialog's new
  parent, Search's subtree and the audit log's node and subtree filters (free text still works
  in all of them).
- The getting-started guide said Ansible's inventory cache "also works on top of" the ETag
  store. It does, but it serves stale hosts silently while the server is down; the guide and
  the plugin's docs now say to leave `cache` off in deploy pipelines, and the plugin's example
  no longer sets it. The guide's troubleshooting table covers "no hosts matched", "could not
  be verified by inventory plugin" and the new cache warning.
- `ais import` now behaves like `POST /api/v1/import`. It takes the write lock and refuses a
  tree that would no longer export. When nothing changed it writes no revision: it used to
  record an empty one on every run. A real import now leaves its `import` audit row; the CLI
  wrote none before.

## 0.3.0 — 2026-11-05

### Security
- **The render no longer reaches the API server.** `POST /api/v1/render` (any role, viewer
  included), the UI's Render page and the `render_info` module evaluated Ansible lookups on
  the server: an override such as `{"x": "{{ lookup('pipe', 'id') }}"}` ran a shell command as
  the API's user, and `expandvars` read its environment (`AIS_DATABASE_URL` included). The
  render now runs no lookups (`lookup`, `query`, `q`), refuses the builtin filters and tests
  that read the server's environment or files (`expandvars`, `expanduser`, `fileglob`,
  `realpath`, `relpath`, `vault`, `unvault`; `file`, `exists`, `directory`, `link`, `mount`,
  `same_file`, `vaulted_file` and their `is_` spellings) and every collection filter or test,
  has no template loader (`{% include %}`), and no longer templates the request's `facts`.
  A refused call is that key's render error; playbooks still evaluate the same values. Every
  0.2.0 server should be upgraded; rotate the database password and `AIS_SECRET_KEY` of any
  0.2.0 server that untrusted token holders could reach.

### Added
- Depth and path limits: node depth ≤ 32 (`AIS_MAX_DEPTH`) and path ≤ 1,500 bytes, refused with
  a 422 on import, create and move. Before this, a path near 2,000 bytes on PostgreSQL failed
  with a 500 from the ltree index.
- `GET /api/v1/snapshots/status` and `ais snapshots run|status`; `AIS_SNAPSHOTS=off` and
  `AIS_SNAPSHOT_POLL_SECONDS` configure the worker.

### Changed
- Host snapshots are stored only where a host's resolved variables changed, by a background
  pass in the API process, instead of one row per host on every export and render. Export and
  render no longer write. Every revision still answers exactly (a revision the pass skipped is
  computed on demand). `GET /hosts/{name}/snapshots` now lists the revisions where the host
  changed; the single-snapshot response adds `stored_revision`. Migration `0005` adds
  `snapshot_pass` and drops stored rows that repeat the same host's previous row.
- The Diff view's ● now means "this host changed in that revision".
- Releases go to GitHub only: `release.yml` builds the collection and creates the GitHub
  release `v<version>` with the tarball (from the existing tag's tree when the tag predates the
  release). It no longer publishes to Ansible Galaxy, and the `galaxy` environment is gone.
- CI's `image` job starts the built image and requires `/healthz` and the UI, not just a build.
- Dependabot ignores python >= 3.15 for the deploy image: ansible-core 2.21 does not import there.
- Deploy image: Python 3.12 → 3.14 and the UI build stage Node 22 → 26; `@types/node` 26
  (Dependabot PRs 15, 6 and 7, verified by the `image` boot check on main).

### Fixed
- **Playbooks now evaluate Jinja stored in the inventory.** ansible-core 2.19+ templates an
  inventory plugin's strings only when the plugin declares its source trusted; the `db` plugin
  did not, so a playbook saw a stored `{{ ansible_host }}:{{ http_port }}` as that literal
  text while the server's render showed it evaluated. The plugin now sets
  `trusted_by_default`, so a playbook and the render agree (a new test runs `ansible` against
  the server and compares). Stored values are therefore trusted templates on the controller,
  exactly like a YAML inventory: the editor role can run code on every controller that uses
  the inventory.
- The importer no longer recurses, so very deep trees fail on the depth limit instead of
  Python's recursion limit (near depth 1,000).

## 0.2.0 — 2026-11-01

First release. The collection `git_cgallagher.inventory` 0.2.0 goes to Ansible Galaxy through
`release.yml` once `GALAXY_API_KEY` is set; the server, UI and collection share the version.

### Release
- `release.yml`: on every push to `main`, publish the collection to Galaxy when Galaxy lacks
  `galaxy.yml`'s version (main-only `galaxy` environment, `GALAXY_API_KEY`), then create the
  GitHub release `v<version>` with the tarball; warns and stops while the key is unset; refuses
  a collection that changed since an existing `v<version>` tag.
- Versions: server 0.1.0 → 0.2.0, UI package 0.3.0 → 0.2.0, collection 0.2.0 (its first
  release; the inventory plugin's `version_added` is 0.2.0 too). The collection now ships
  `LICENSE`, and `build_ignore` keeps a local `integration_config.yml` (a real token) and the
  generated test inventory out of the tarball.
- Migration `0004`: `node.meta` and `node_variable.value` NOT NULL, as the models always said
  (spike-era NULLs become `{}` and JSON `null`). The schema drift check is now a test
  (`tests/test_migrations.py`).

### Fixed (release)
- **SQLite migrations no longer delete variables.** Batch mode rebuilt `node` with foreign keys
  on, and dropping the old table cascaded into `node_variable`; `init_db` now migrates SQLite
  with `PRAGMA foreign_keys=OFF` and checks `PRAGMA foreign_key_check` afterwards.

### Added
- **Phase 4 — scale proof.** `bench/generate.py` (100k hosts, ~10k groups, depth 8, straight
  into the database), `bench/benchmark.py` and `bench/compose.override.yml`; results in
  `docs/scale.md`.

### Fixed
- Full export of a large tree: `Snapshot.children()` scanned every node per node (12k nodes:
  24 s, now 0.3 s); children/host indexes and a per-snapshot cache of emitted variables.
- Full export at 100k hosts was an HTTP 500 on PostgreSQL (65,535 bind-parameter limit in the
  snapshot lookup and insert); both are chunked.
- Subtree export filters in SQL (`path_ltree` GiST index on PostgreSQL); render, diffs of a
  missing snapshot and the snapshot list's host check load only the host's ancestor chain.
  100k hosts: render 2.4 s → 0.01 s, subtree export 2.5 s → 0.10 s.
- Plugin: `AIS_ETAG_STORE=""` wrote the export into the current directory instead of
  disabling the store (`etag_store` was `type: path`; it is `str` now).

### Added (earlier phases)
- **Phase 4 — UI write gaps.** From the node page an editor adds a child, moves/renames a node
  with its subtree and deletes a subtree (all with `If-Match` from the page; the tree reloads
  because ids change). New pages: **Variables** (declare merge strategy, description, JSON
  Schema; undeclare), **Tags** (create, edit members by path, delete) and **Tokens** (mint with a
  role cap and expiry, secret shown once; revoke; admins can list everyone's).
- `GET /api/v1/audit?node=<id or path>[&key=]`: a node's rows and its variables' rows under every
  id the node had, following `node.move` records (its own moves and its ancestors'). The UI's
  "history" links use it with the path, so they no longer lose the rows from before a move.
- **Phase 4 — write gaps.** Tag write API: `GET /api/v1/tags[/{name}]` (viewer),
  `PUT /api/v1/tags/{name}` (declarative: `description`, `members` as node paths; an unchanged
  tag is `changed: false` without a revision) and `DELETE /api/v1/tags/{name}` (editor,
  `If-Match`, audited as `tag.set` / `tag.delete`). Tags never carry variables; names are
  `[A-Za-z0-9_]` and may not start with `business_`, `tier_` or `datacenter_` (the automatic tag
  groups).
- Modules `git_cgallagher.inventory.node_move` (move/rename a node with its subtree; done when only
  the new path is live) and `git_cgallagher.inventory.import` (bulk tree import; additive,
  idempotent, check mode = server dry run), each with an ansible-test integration target.
- `json_schema` on variable definitions is enforced (`jsonschema` is a new server dependency):
  `PUT /nodes/{id}/vars/{key}`, `POST /nodes` vars, the importer and `ais import` refuse values
  the schema rejects (422); `PUT /variables/{name}` refuses an invalid schema or one that a live
  value of the key violates.
- Requests that present a bad bearer token leave a `token.rejected` audit row (reason, client,
  method, path; the token's id or display prefix only). Failed logins are limited per e-mail (5)
  and per client address (20) in a 5-minute window: 429 with `Retry-After`, audited as
  `login.throttled`; `login.failure` rows now record the client address.

- **Behaviour change:** `POST /api/v1/import` answers **200 `changed: false`** and writes nothing (no revision, no audit
  row) when the tree already matches, and 201 with `changed: true` otherwise; `dry_run: true`
  reports the same counts and rolls back. `definitions` in the response now lists only the
  definitions the import created or changed.
- **Phase 3 — web UI.** React + TypeScript single-page app in `web/` (Vite, no component
  library; `react`, `react-dom`, `yaml`), built into `server/ais/static` and served by the API
  at `GET /` and `GET /assets/…` (`AIS_WEB_DIR` overrides the folder; a missing build is a 404 on
  `/` only). Views: sign-in (local accounts; SSO button when `AIS_OIDC_*` is set), lazy tree with
  ARIA keyboard navigation and node detail (own vars, inherited vars greyed with the winning or
  contributing nodes from render provenance), JSON/YAML variable editor (merge-strategy badges
  from `/variables`, `If-Match` from the revision shown, stale 409 reloads and keeps the text),
  render (host or mock path, overrides, facts, Jinja results and per-key errors), host diff
  between any two revisions, revision and audit browsers. UI requests send `X-AIS-Source: ui`.
- `GET /healthz` reports `oidc` (whether single sign-on is configured).
  `GET /api/v1/auth/oidc/login?ui=1` makes the callback hand the session to the UI through
  `sessionStorage` instead of answering JSON (unverified against a real provider).
- The Docker image builds the UI in a Node 22 stage; `docker compose -f deploy/compose.yml up
  --build` serves it at `http://127.0.0.1:8000/`. `.dockerignore` added.
- CI job `web`: `npm ci`, `npm run build` (typecheck + Vite), Playwright smoke against a
  throwaway server on the seed tree (sign in, keyboard tree, edit with a forced 409, render,
  diff, audit, deep link). `server/tests/test_web.py` pins the static routes (index and asset
  headers, traversal refused, API routes and `/healthz` untouched, missing build). Dependabot
  covers `web/` (npm, monthly, grouped).
- **Phase 2 — write path.** `audit_log` (migration `0003`): one row per effective write
  (`node.create|update|move|delete`, `variable.set|unset`, `definition.set|delete`, `import`)
  and per auth event (`login.success|failure`, `logout`, `token.create|revoke`,
  `user.create|update`; `ais user create` / `ais token create|revoke` with `source=cli`),
  written in the same transaction as the change with actor, target, `before`/`after`,
  `source`, `request_id` and the revision. `GET /api/v1/audit` (editor; filters `actor`,
  `action` or `action` prefix ending in `.`, `target_type`, `target_id`, `revision`,
  `request_id`, `since`, `until`; `limit`/`before` paging). Requests may send
  `X-Request-ID` (echoed) and `X-AIS-Source: module|ui` (recorded on revisions and audit rows).
- `POST /api/v1/nodes/{id}/move` (new parent and/or name, `If-Match`): closes the subtree and
  reopens it with new paths, depths and ids under the new revision, carrying variables and
  tag memberships; the response lists `moved` (old id → new id, path → path). Refuses the
  root, a move under the node's own subtree, a child kind the parent cannot have, a live node
  at the new path (409) and anything the exporter rejects (409).
- `GET /api/v1/nodes/{ref}` also accepts a path (`all.commercial.prod`). `POST /import`
  returns `nodes_created`, `variables_set`, `definitions`. `PUT /variables/{name}` with an
  unchanged declaration is a no-op (`changed: false`, no revision), like `PUT …/vars/{key}`.
- Collection `git_cgallagher.inventory` 0.2.0: modules `node`, `host`, `variable`,
  `variable_definition` (idempotent, check + diff mode, `If-Match` locking via
  `module_utils/client.py`, `expect_revision`, `revision_message`) and `node_info`,
  `render_info`; doc fragments `api` / `api.write`; action group `inventory` for
  `module_defaults`. `ansible-test sanity` (ignore files for 2.18–2.22, GPL header only) and an
  integration target per module (check, real, repeat, failure, cleanup).
- CI job `collection`: sanity + integration against a live `ais serve` with a minted editor
  token (`timeout-minutes: 25`). Tests: audit rows (per write, before/after, auth events,
  refused writes leave none, filters/paging, 403 for viewers), move/rename (path rewrite,
  history, 409/422 refusals, tags carried), path lookup; 51 pytest tests, green on SQLite
  and PostgreSQL 16.
- **Phase 1 — core API.** Every `/api/v1` route now needs a bearer token (`Authorization:
  Bearer ais_…`); `/healthz` stays open. Local users with scrypt password hashes, API tokens
  hashed at rest (`api_token`), roles `viewer` < `editor` < `admin` (reads incl. export/render
  need viewer, writes editor, user management admin). `POST /api/v1/auth/login` issues a
  short-lived session token; `POST/GET/DELETE /api/v1/tokens`; `GET/POST/PATCH /api/v1/users`;
  `ais user create|list`, `ais token create|revoke|list`. OIDC (Authlib, extra `oidc`) is a stub
  behind `AIS_OIDC_*`, answering 501 until configured.
- Write endpoints with optimistic locking: `POST/PATCH/DELETE /api/v1/nodes[/{id}]`,
  `PUT/DELETE /api/v1/nodes/{id}/vars/{key}`, `PUT/DELETE /api/v1/variables/{name}`,
  `POST /api/v1/import`. Every write needs `If-Match: "<revision>"` (428 without, 409 when
  stale, with the current revision in the body and `ETag`), opens a new `Revision` recorded
  with the caller's email, and closes/reopens temporal rows. Child kind order, node names and
  group-name / tag-group collisions are enforced at write time (the exporter re-runs on the
  live tree and a collision is a 409).
- Jinja render: `POST /api/v1/render` takes `facts` (mock facts, with or without the
  `ansible_` prefix) and adds `rendered` (ansible-core `Templar` over the resolved vars, with
  `inventory_hostname` / `group_names`) and per-key `errors` instead of failing. `template:
  false` skips it. `ais render --facts`.
- `host_snapshot` (host, revision, resolved, hash), materialized on render and export;
  `GET /api/v1/hosts/{name}/snapshots[/{revision}]` and `GET /api/v1/diff?host=&from=&to=`
  (missing snapshots are computed on demand). Also `GET /api/v1/nodes/{id}[/children|/vars/{key}]`,
  `GET /api/v1/variables[/{name}]`, `GET /api/v1/revisions`.
- Alembic migrations (`ais/migrations`) replace `create_all`; `ais init-db` (alias `migrate`)
  upgrades to head and stamps a phase-0 database first. On PostgreSQL `node.path_ltree`
  (generated `ltree`, GiST index) is added without changing the resolver.
- Plugin: keeps the last export per source in `etag_store` (default `~/.cache/ais`,
  `AIS_ETAG_STORE`) and revalidates with `If-None-Match`; an unchanged inventory is a 304.
  Export `ETag` is now `"<revision>.<params-digest>"`.
- Tests: auth (401/403, login, token lifecycle), locking (428/409), temporal writes and
  deletes, collisions, render errors, snapshots/diff, server 304 and plugin revalidation
  (41 tests). `AIS_TEST_DATABASE_URL` runs the suite against PostgreSQL.

### Changed
- **Nodes are temporal** (`created_rev` / `deleted_rev`): a pinned export or render now shows
  exactly the nodes that existed at that revision (nodes created later are absent), and
  `DELETE /nodes/{id}` closes the subtree and its variables instead of removing rows. The
  unique constraints on `path` and `(parent_id, name)` are partial (live rows only), so a
  deleted node can be recreated.
- Node names are restricted to letters, digits, `_` and `-` (valid `ltree` labels).
- CI: the plugin smoke step mints a token; jobs have `timeout-minutes`.

### Added (phase 0)
- Public GitHub repo with the standard baseline: `SECURITY.md`, CI (`test` + `image`), Dependabot,
  CodeQL default setup, secret scanning + push protection, rulesets.
- Phase-0 spike: strict-tree data model with temporal variables and revisions;
  per-variable merge strategies (`replace`, `merge`, `append`, `unique_append`) folded
  server-side at contributing nodes; `/api/v1/export`, `/api/v1/render` (real or mock
  host, with provenance), `/api/v1/nodes`, `/api/v1/import`; `ais` CLI; YAML bulk
  importer; inventory plugin `git_cgallagher.inventory.db` (subtree, revision pin,
  cache plugin support, tag groups); compose deployment; tests incl. the
  `ansible-inventory --host` parity test.

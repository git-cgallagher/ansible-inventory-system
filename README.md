# Ansible Inventory System

[![CI](https://github.com/git-cgallagher/ansible-inventory-system/actions/workflows/ci.yml/badge.svg)](https://github.com/git-cgallagher/ansible-inventory-system/actions/workflows/ci.yml)

An authoritative, database-backed Ansible inventory with a **strict tree hierarchy**
(`all → business → tier → datacenter → group → … → host`), a long precedence chain,
per-variable merge strategies, revision history, and a render endpoint that shows what
any real or hypothetical host resolves to.

**Status: 0.4.1 (2026-11-06): fixes (the `host` module's lookup no longer downloads the tree, plain-text 404 details) and docs (revision ids can have gaps on PostgreSQL). 0.4.0 (2026-11-05): profiles, revert and change ids, restore, previews of every write, variable search, typo warnings, tree export, a tag module (see `CHANGELOG.md`; deleting a non-empty group now needs confirmation). 0.3.0 (2026-11-05) was a security fix to the render; 0.2.0 was the first release (2026-11-01).** Phase 4 closed the write gaps (tags, `node_move`
and `import` modules, enforced JSON Schemas, bad-token audit, login limiter), gave the UI node,
variable, tag and token management, proved 100k hosts (`docs/scale.md`) and added release
automation for the collection (GitHub releases; it is not published to Ansible Galaxy). Before that, phase 3 added the web UI on top of phase 1 (bearer-token auth with
roles, locked write endpoints, Jinja render with mock facts, per-host snapshots and diffs,
Alembic migrations, an ETag-revalidating inventory plugin) and phase 2 (an **audit log** of
every write and auth event, **node move/rename** with history, and the collection's
**Ansible modules**, idempotent with check and diff mode, tested by `ansible-test` in CI):
a **web UI** served by the API at `/` for browsing the tree, editing variables, rendering
real or mock hosts, diffing a host between revisions and reading the revision and audit
history. Still tested by a golden test that the server's render equals
`ansible-inventory --host`. See
`ANSIBLE-INVENTORY-PLAN.md` in `~/Code` for the full plan and `AGENTS.md` here for how
to work on it.

**New here? Start with [`docs/getting-started.md`](docs/getting-started.md)**: the ideas, a
first local run, a PostgreSQL deployment with TLS and backups, how to design the tree, every
way to write to it, secrets, roles and troubleshooting. This README is the short reference.

## How it works

- **Ansible does the merging.** The plugin emits the real group tree with variables
  at each node. In a strict tree a host's groups form one chain with no two at the
  same depth, so Ansible's native ancestors-first precedence *is* ours.
- **Per-variable merge/append** (which Ansible cannot express) is handled by the
  server folding the value down the path and emitting the cumulative fold at every
  contributing node. Ansible's plain replace then yields the right answer.
- **Qualified group names** (`commercial_prod_dc1_web`) avoid collisions between
  `commercial/prod` and `federal/prod`; var-less **tag groups** (`tier_prod`,
  `datacenter_dc1`) exist for targeting only.
- **The server's render is safe to expose to viewers**: it evaluates Jinja expressions and pure
  filters, but runs no lookups and refuses filters and tests that would read the API server's
  environment or files (they render as that key's error; a playbook still runs them).
- **Revisions** are immutable; nodes and variables are temporal rows, so any revision
  can be exported or rendered exactly as it was. The plugin can pin one, and sets
  `inventory_revision` on `all` so every run records what it ran against.
- **Writes are optimistically locked**: every write carries `If-Match: "<revision>"`
  and is refused (409) when the inventory moved underneath it.
- **Auth**: local users (scrypt) and bearer tokens hashed at rest; roles `viewer`
  (export/render), `editor` (writes, audit), `admin` (users). OIDC is a config-flag stub.
- **Audit**: every effective write and every login/token/user event is one `audit_log` row
  (actor, action, target, before/after, source, request id, revision), written in the same
  transaction as the change. `GET /api/v1/audit` filters by actor, action (or prefix),
  target, revision, request id, time, a variable's key (on every node) and a subtree, newest first.
- **Variable search.** `GET /api/v1/vars?key=gpu_driver` lists the nodes that set a key
  themselves, with their values; `GET /api/v1/vars/hosts?key=gpu_driver&value="550.90"` lists the
  hosts whose resolved value matches (as a playbook sees it), with the node it comes from and
  the distribution of values. Both take `subtree`, `revision` and paging; `ais vars` and the
  `variable_info` module are the same search, and the UI has a Search page with "where set"
  links on the Variables and node pages.
- **Typo warnings and strict mode.** A write that stores an undeclared key close to a declared
  one (or to an undeclared key many nodes set: `AIS_TYPO_MIN_NODES`, default 5) still succeeds, with `warnings: [{key, path,
  did_you_mean, declared, message}]` in the response, an Ansible warning (and `key_warnings`) in
  the modules, a line on stderr from the CLI and a hint in the UI. `AIS_STRICT_KEYS=true`
  refuses keys that are neither declared nor set anywhere yet (422 with the hint); restores and
  reverts are never refused. `ais lint` / `GET /api/v1/vars/lint` list the likely typos already
  in the tree.
- **Move/rename** (`POST /api/v1/nodes/{id}/move`) rewrites the subtree's paths as new
  temporal rows, so the previous revision still shows the old tree; node ids change.
- **Tags** (`PUT /api/v1/tags/{name}` with member paths, or the `tag` module) are extra var-less
  groups under `all` for targeting (`--limit canary`); a non-host member contributes every host
  under it. Tag names follow the node name rule (letters, digits, `_`, `-`) and a `-` becomes `_`
  in the group, as for nodes (`gpu-canary` is the group `gpu_canary`); a name whose group is
  another tag's or a hierarchy group's is refused (422).
- **Profiles** (role templates): a named bundle of variables (`gpu-a100: {gpu_driver: …}`)
  attached to any number of groups. At each node it applies as a level between the node's
  parent and the node (below the node's own values, same merge strategies; several on one node
  apply in the node's list order, the last wins). Bumping a value is one write that reaches
  every node it is attached to, previewed host by host. The plugin sees plain group vars, the
  render's provenance says `profile:gpu-a100 @ <path>`, and history, search, audit, schemas,
  revert, import/export and the UI all know them. `PUT /api/v1/profiles/{name}`, the `profile`
  module, a `profiles:` block in the import format; see the guide's "Profiles".
- **Tree export** (`GET /api/v1/tree`, `ais export-tree`) writes the tree or one subtree, at
  any revision, in the format `ais import` and `POST /api/v1/import` read: nodes with their
  kinds, attached profiles and own values, the declarations and profiles they use, and tags. Importing it reproduces the
  inventory, so backup, cloning (`as=` re-roots the copy, `hosts=false` leaves hosts out) and keeping the tree in Git
  are one call each. The import format takes `tags:` (member paths, additive).
- **Preview any write** (`dry_run`): every write route runs the write with all its checks,
  answers what it would do and rolls back. The answer's `impact` is the same everywhere: the
  hosts whose resolved variables change (per-host `added`/`removed`/`changed` keys, `from` →
  `to`), hosts added or removed, hosts that join or leave groups, exact counts, and the first
  200 hosts. No `If-Match` needed; one that is sent must be current. Then send the write with
  `If-Match` = the preview's `revision`, and it refuses if anything changed in between. The
  UI shows "This changes N hosts" before saving; module check mode is the server's dry run
  and `--diff` prints the summary. It resolves only the hosts and keys a write can change:
  0.06 s for a value at a datacenter, about 1 s at `all` with 100k hosts.
- **Delete asks first, restore is one call.** `DELETE /api/v1/nodes/{id}` refuses (409, with
  the node and host counts) to delete a node that has descendants unless the call says
  `recursive=true` or gives `expect_count` = the subtree's size; `dry_run=true` lists what would
  go and its `impact`. `POST /api/v1/nodes/restore {path, revision?}` (or `ais restore PATH`) brings a deleted
  subtree back as it was at a revision (default: the last one before the delete), with its own
  values, node meta and tag memberships, and links the new node ids to the old ones so the audit
  history from before the delete stays visible. It never overwrites a live node.
- **Change ids and revert.** Writes can carry a change id (`X-AIS-Change: SRE-1234`, the
  modules' `change` option, `ais import --change`); revisions and audit rows record it and
  `GET /api/v1/revisions?change=` / `GET /api/v1/audit?change=` filter by it.
  `POST /api/v1/revisions/{id}/revert` undoes one revision and `POST /api/v1/changes/{change}/revert`
  every revision of a change, as one new revision: the inverse of each audited write, newest
  first, each recorded as an ordinary audit row (so a revert can be reverted too). Every write
  kind reverts exactly (variables, nodes incl. moves, deletes and restores, definitions, tags,
  imports). It refuses (409, listing them) when a later revision changed the same node, key,
  definition or tag, unless `force=true`; `dry_run=true` lists the operations and their `impact`
  on the hosts. `ais revert` and the `revert` module do the same; the UI has a
  Revert button on each revision and on a change.
- **Variable schemas**: a definition's `json_schema` is enforced on every write of that key
  (API, import, modules), and a new schema is refused while any live value violates it.
- **Hardening**: requests with a bad bearer token are audited (`token.rejected`, never the
  token itself); failed logins are limited per e-mail and per client address (429 with
  `Retry-After`; `AIS_LOGIN_MAX_FAILURES`, `AIS_LOGIN_MAX_FAILURES_PER_IP`,
  `AIS_LOGIN_WINDOW_SECONDS`).
- **Per-host history**: a background pass in the API process stores a host's resolved
  variables only in revisions where they changed, so storage grows with real changes, not with
  exports, and nothing is written on the export or render path. Every revision still answers
  exactly: a revision the pass skipped (writes arriving in a burst are passed once) is computed
  on demand. `ais snapshots run` runs a pass by hand; `AIS_SNAPSHOTS=off` turns the worker off,
  `AIS_SNAPSHOT_POLL_SECONDS` (default 10) sets how often it checks for missed writes.
- **Depth and path limits**: groups nest freely below a datacenter, up to a node depth of 32
  (`all` is 0, a business 1, a tier 2, a datacenter 3; `AIS_MAX_DEPTH` raises it), and a node's
  path may be at most 1,500 bytes. Both are refused with a 422 naming the limit, on import,
  create and move (a move checks the whole relocated subtree). The path cap is PostgreSQL's:
  its ltree index breaks on many paths near 2,000 bytes, so on realistic names the path cap,
  not the depth cap, is what binds first (about 50 levels at 30-character names).

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -e './server[test]'
cd examples
export AIS_DATABASE_URL=sqlite:///./ais.db
../.venv/bin/ais import seed.yml                     # creates/migrates the schema too
../.venv/bin/ais user create me@example.com --role admin      # prompts for a password
export AIS_TOKEN="$(../.venv/bin/ais token create --user me@example.com)"   # printed once
../.venv/bin/ais serve &            # http://127.0.0.1:8000
../.venv/bin/ansible-inventory --graph         # uses examples/ansible.cfg + AIS_TOKEN
../.venv/bin/ansible-inventory --host web02
../.venv/bin/ais render --path all.commercial.lab.dc1.web --facts '{"os_family": "RedHat"}'
../.venv/bin/ais export-tree --subtree all.federal > federal.yml   # import format; --revision, --as, --no-hosts
../.venv/bin/ais vars packages                       # where a key is set; --hosts, --value, --subtree, --revision
../.venv/bin/ais lint                                # undeclared keys that look like typos (exit 1 when any)
```

Open `http://127.0.0.1:8000/` for the web UI once it is built (next section). API docs at
`http://127.0.0.1:8000/docs`. Every `/api/v1` call needs
`Authorization: Bearer $AIS_TOKEN`; writes also need `If-Match: "<revision>"` (the
current revision is the first entry of `GET /api/v1/revisions`, or the `ETag` of the
last write):

```bash
H="Authorization: Bearer $AIS_TOKEN"
curl -s -H "$H" 'http://127.0.0.1:8000/api/v1/revisions?limit=1'
curl -s -H "$H" -H 'If-Match: "1"' -H 'Content-Type: application/json' \
  -X PUT http://127.0.0.1:8000/api/v1/nodes/5/vars/http_port -d '{"value": 9090}'
curl -s -H "$H" 'http://127.0.0.1:8000/api/v1/diff?host=web01&from=1&to=2'
curl -s -H "$H" -H 'Content-Type: application/json' -X POST http://127.0.0.1:8000/api/v1/render \
  -d '{"host": "web01", "facts": {"default_ipv4": {"address": "10.1.1.11"}}}'
```

### Web UI

A React + TypeScript single-page app in `web/`, built with Vite into `server/ais/static` and
served by the API itself at `/` (same origin; it signs in through `POST /api/v1/auth/login`
and then calls `/api/v1` with that bearer token, like any other client). The Docker image
builds it in; from a checkout:

```bash
cd web && npm ci && npm run build      # Node 22; then `ais serve` serves it at /
npm run dev                            # or: hot reload on :5173, API proxied to :8000
```

- **Sign in** with a local account (`ais user create … --role editor` prompts for the
  password). A single sign-on button appears when `AIS_OIDC_*` is configured (unverified, like
  the OIDC stub itself).
- **Tree**: loads one level at a time and works from the keyboard (arrows, Home/End, Enter).
  A node shows the variables set on it and, greyed, everything it inherits with the node that
  wins (or, for merge/append variables, every node that contributes).
- **Variable editor** (editor role): JSON or YAML per key, with the merge-strategy badge and
  description from `/variables`. Saves carry `If-Match` with the revision on screen; if someone
  else wrote first, the page says so, reloads the values and keeps your text to re-apply.
- **Render**: a real host, or a mock host at any path, with optional overrides and facts;
  shows each resolved value, where it came from, the Jinja result and per-key template errors.
- **Node pickers** (Render's host and path, Diff's host, the move dialog's new parent, Search's
  subtree, the audit log's node and subtree): type part of a name or path and pick from up to
  20 matches, each with its path under the name (Down/Up, Enter, Escape, or the mouse). They
  ask the server for a few nodes per pause in typing (`GET /api/v1/nodes?q=`), so they stay
  small at any tree size; links like `#/render?host=web01` or
  `#/diff?host=web01&from=3&to=7` work without a lookup.
- **Structure** (editor role): add a child, move or rename a node with its subtree, delete a
  subtree (the dialog shows how many nodes and hosts go and confirms that count), from the node
  page. Saving or removing a value, a move and a delete first show how many hosts it changes
  (expand for the list); the confirmation carries the preview's revision as `If-Match`.
- **Variables**: declare a variable's merge strategy, description and JSON Schema (a save or an
  undeclare shows the hosts it changes first).
- **Tags**: create, edit and delete tags and their member paths.
- **Tokens**: mint API tokens for the plugin and the modules (capped role, expiry; the secret is
  shown once) and revoke them.
- **Diff** a host between any two revisions, and browse **revisions** and the **audit log**
  (editor role), including "history" links from a node or a key that follow it across moves and
  renames. Both filter by change id; **Revert** on a revision (or on a change) shows the dry run
  (operations, hosts that would change, or the conflicts) before it writes.

### Managing the inventory from Ansible

The collection's modules take `url`/`token` (or `AIS_URL`/`AIS_TOKEN`) and need an
**editor** token for writes. Every write is optimistically locked (the module reads the
current revision, sends `If-Match`, and fails clearly if someone wrote in between) and
audited with `source=module`. All support check mode and `--diff`.

```yaml
- hosts: localhost
  gather_facts: false
  module_defaults:
    group/git_cgallagher.inventory.inventory:
      url: http://127.0.0.1:8000
      # token: via AIS_TOKEN
  tasks:
    - name: packages accumulate down the tree
      git_cgallagher.inventory.variable_definition:
        name: packages
        merge_strategy: unique_append

    - name: A cache group under dc1
      git_cgallagher.inventory.node:
        path: all.commercial.prod.dc1.cache
        vars: {redis_port: 6379, packages: [redis]}
        meta: {owner: platform}

    - name: A host in it
      git_cgallagher.inventory.host:
        name: cache01
        parent_path: all.commercial.prod.dc1.cache
        ansible_host: 10.1.3.11

    - name: One variable at a node
      git_cgallagher.inventory.variable:
        path: all.commercial.prod
        key: log_level
        value: error

    - name: One role's settings, attached wherever the role appears (one write to change)
      git_cgallagher.inventory.profile:
        name: cache-role
        vars: {redis_maxmemory: 2gb}
        attach: [all.commercial.prod.dc1.cache]

    - name: What would a host there see?
      git_cgallagher.inventory.render_info:
        path: all.commercial.prod.dc1.cache
        facts: {default_ipv4: {address: 10.1.3.99}}
      register: mock

    - git_cgallagher.inventory.node_info:
        path: all.commercial.prod.dc1
      register: dc1

    - name: Patch the cache group first (paths are added; exclusive=true replaces them)
      git_cgallagher.inventory.tag:
        name: patch-wave-1        # the group is patch_wave_1
        paths: [all.commercial.prod.dc1.cache]

    - name: Move it to another datacenter (ids change; history keeps the old place)
      git_cgallagher.inventory.node_move:
        path: all.commercial.prod.dc1.cache
        new_path: all.commercial.lab.dc1.cache

    - name: Bulk load (additive; a matching tree is not a change; check mode is a server dry run)
      git_cgallagher.inventory.import:
        tree: "{{ lookup('ansible.builtin.file', 'seed.yml') | from_yaml }}"

    - name: Retire the group (whole subtree)
      git_cgallagher.inventory.node:
        path: all.commercial.lab.dc1.cache
        state: absent
        recursive: true
```

Give the tasks of one change the same `change` id (once, in `module_defaults`) and the
`revert` module undoes them together, for example in a `rescue:`:

```yaml
- hosts: localhost
  gather_facts: false
  module_defaults:
    group/git_cgallagher.inventory.inventory:
      url: http://127.0.0.1:8000
      change: SRE-1234          # X-AIS-Change on every request; recorded on every revision
  tasks:
    - block:
        - git_cgallagher.inventory.node_move:
            path: all.commercial.prod.dc1.cache
            new_path: all.commercial.prod.dc2.cache
        - git_cgallagher.inventory.variable:
            path: all.commercial.prod.dc2.cache
            key: dns_servers
            value: [10.2.0.2]
      rescue:
        - name: Undo everything SRE-1234 changed, as one revision
          git_cgallagher.inventory.revert:
            revert_change: SRE-1234
            change: SRE-1234-rollback
```

Tags have the `tag` module (`paths`, `exclusive`, `remove_paths`, `state`; it reads the revision
for `If-Match` itself and retries once on a stale 409 when the tag still differs) and
`tag_info`.

### Install the collection

The collection is **not on Ansible Galaxy**. Each version is a GitHub release with the built
tarball attached; install it from there:

```bash
ansible-galaxy collection install \
  https://github.com/git-cgallagher/ansible-inventory-system/releases/download/v0.4.1/git_cgallagher-inventory-0.4.1.tar.gz
```

or pin it in a `requirements.yml`:

```yaml
collections:
  - name: https://github.com/git-cgallagher/ansible-inventory-system/releases/download/v0.4.1/git_cgallagher-inventory-0.4.1.tar.gz
    type: url
```

From a checkout: `ansible-galaxy collection install
./collection/ansible_collections/git_cgallagher/inventory` (or point `collections_path` at
`collection/`, as `examples/ansible.cfg` does). The collection and the server share a version.

Consumers should run with this in `ansible.cfg`, as `examples/ansible.cfg` does:

```ini
[inventory]
any_unparsed_is_failed = True
```

Without it, an inventory that fails to load (server down, bad token, a pinned revision that
doesn't exist, a file name the plugin doesn't accept) is only a warning, and a playbook exits
0 having run on no hosts. Under `[defaults]` Ansible ignores the key silently. Leave Ansible's
inventory cache (`cache: true`) off in deploy pipelines: while it is fresh (`cache_timeout`,
default 3600 s) the plugin never asks the server, so changes, a new revision pin and an outage
all go unnoticed (the plugin warns on each such run). The plugin's own ETag store only
revalidates and never serves a copy when the server is down. Details: section 8 of
[the getting-started guide](docs/getting-started.md).

### PostgreSQL (compose)

```bash
docker compose -f deploy/compose.yml up --build      # API + web UI on :8000, migrations run at start
docker compose -f deploy/compose.yml exec api ais user create admin@example.com --role admin --no-password
docker compose -f deploy/compose.yml exec api ais token create --user admin@example.com
```

That is the development setup (password `ais`, port 8000 on every interface). For a real
deployment add `-f deploy/compose.prod.yml` (password from `AIS_DB_PASSWORD`, loopback-only
port for a TLS proxy); see section 3 of the getting-started guide.

### API overview

| Area | Routes | Role |
|---|---|---|
| Export / render | `GET /api/v1/export` (ETag, `If-None-Match` → 304), `POST /api/v1/render` (`host` or mock `path`, `overrides`, `facts`, `revision`) | viewer |
| Tree export | `GET /api/v1/tree?subtree=&revision=&as=&ancestor_vars=&hosts=&format=json\|yaml`: the import format (`POST /import`'s `tree`); ETag, `X-AIS-Revision` | viewer |
| Variable search | `GET /api/v1/vars?key=&value=&subtree=&revision=&limit=&offset=`: the nodes that set the key themselves (own `value`; `effective` = the fold each emits for merge/append keys; `above_subtree` for the subtree's ancestors). `GET /api/v1/vars/hosts?…` (same parameters): each host's resolved value with `from` (and `sources` for a fold), `total`/`with_key`/`without_key` and the `values` distribution over the whole scope. `value` is JSON (`550.90` a number, `"550.90"` a string; text that is not JSON is a plain string); hosts match the whole resolved value. `GET /api/v1/vars/lint`: undeclared keys that look like typos of known ones (`keys` with `did_you_mean`, `declared`, `nodes`, `paths`) | viewer |
| Node lookup | `GET /api/v1/nodes?q=<text>[&kind=host&kind=…][&subtree=][&revision=][&limit=20]`: at most `limit` (1–100, default 20) live nodes whose name or path contains `q`, ignoring case, ranked exact name, name prefix, path prefix, name substring, path substring (path order within each). Any of `q`, `kind`, `limit` selects this bounded lookup; without them the route lists every node as before | viewer |
| Reads | `GET /api/v1/nodes[?subtree=&revision=]` (every node, unbounded), `/nodes/{id-or-path}`, `/nodes/{id}/children`, `/nodes/{id}/vars/{key}`, `/variables[/{name}]`, `/revisions[?change=&before=&limit=]` | viewer |
| Snapshots | `GET /api/v1/hosts/{name}/snapshots` (the revisions where the host changed), `/hosts/{name}/snapshots/{revision}` (its state at any revision; `stored_revision` says which stored row it came from, null when computed on demand), `GET /api/v1/diff?host=&from=&to=`, `GET /api/v1/snapshots/status` | viewer |
| Profiles | `GET /api/v1/profiles` (name, description, keys, attached count), `GET /profiles/{name}?revision=` (values and `attached` `[{path, kind, position}]`) (viewer); `PUT /profiles/{name}` (`description`, `vars` = the complete set; kept when omitted), `PUT/DELETE /profiles/{name}/vars/{key}`, `DELETE /profiles/{name}` (409 while attached unless `?detach=true`), `PUT /nodes/{id}/profiles` (`profiles` = the node's ordered list), `PUT/DELETE /nodes/{id}/profiles/{name}` (attach, optional `position`; detach) (editor, `If-Match`, `dry_run`) | viewer / editor |
| Tags | `GET /api/v1/tags[/{name}]` (viewer); `PUT /tags/{name}` (`description`, `members` = node paths), `DELETE /tags/{name}` (editor, `If-Match`) | viewer / editor |
| Revert (`If-Match`) | `POST /api/v1/revisions/{id}/revert`, `POST /api/v1/changes/{change}/revert` (`force`, `dry_run`, `message`; 201): one new revision; 409 `conflict` (with `conflicts`) when a later revision changed the same things, 409 `already reverted`, 422 when nothing recorded can be inverted; `dry_run` needs no `If-Match` and adds `impact` (and `hosts_changed`/`hosts`, its older names) | editor |
| Writes (`If-Match`) | `POST /api/v1/nodes`, `PATCH /nodes/{id}`, `DELETE /nodes/{id}` (a node with descendants needs `?recursive=true` or `?expect_count=<subtree size>`, else 409 with the counts), `POST /nodes/restore` (`path`, `revision`, `dry_run`; 201), `POST /nodes/{id}/move`, `PUT/DELETE /nodes/{id}/vars/{key}`, `PUT/DELETE /variables/{name}`, `PUT/DELETE /tags/{name}`, the profile routes, `POST /import` (a no-op answers 200 `changed: false`). Writes that store values answer `warnings` + `warnings_total` (undeclared keys that look like typos); with `AIS_STRICT_KEYS=true` a new undeclared key is a 422 `undeclared key` (restore and revert excepted) | editor |
| Dry run (any write) | `?dry_run=true` on every write route above (and on tags); `"dry_run": true` in the body of `POST /import`, `/nodes/restore` and the revert routes. Runs the write and every check it runs, rolls back, answers 200 with the write's body (ids it would allocate `null`), `dry_run: true`, `revision` = the base revision, and `impact`: `total`, `vars_changed`, `added`, `removed`, `regrouped`, `moved`, `hosts` (by path, with `added`/`removed`/`changed` keys, `joined`/`left` groups, `moved_from`), `groups` (`joined`/`left` counts), `groups_total`, `limit` (200), `truncated`. Refuses exactly what the write refuses. No `If-Match` needed; one that is sent must be current (409) | editor |
| Audit | `GET /api/v1/audit?actor=&action=&target_type=&target_id=&revision=&request_id=&change=&since=&until=&limit=&before=`; `node=<id or path>[&key=]` = that node's (and its variables') rows under every id it had (across moves, renames and restores); `key=` alone = that variable's set/unset rows on every node and its definition's rows; `subtree=` = rows whose recorded path (as written then) is at or below it | editor |
| Auth | `POST /auth/login`, `POST /auth/logout`, `GET /auth/me`, `POST/GET/DELETE /tokens`; `GET/POST/PATCH /users` | any / admin |
| Open | `GET /healthz` (`status`, `version`, `oidc`), `GET /` + `/assets/…` (the built web UI, static files only) | none |

Requests may send `X-Request-ID` (echoed, stored on audit rows), `X-AIS-Source: module|ui`
(recorded as the revision's and audit row's `source`) and `X-AIS-Change: <id>` (recorded as the
revision's and its audit rows' `change`: 1-64 characters from letters, digits and `._:+@-`,
starting with a letter or digit; anything else, or a value that looks like an `ais_` token, is a
400 that does not repeat the value).

## Tests

```bash
cd server && ../.venv/bin/python -m pytest
AIS_TEST_DATABASE_URL=postgresql+psycopg://ais:ais@127.0.0.1:55432/ais_test ../.venv/bin/python -m pytest   # same suite on PostgreSQL
```

`tests/test_parity.py` starts the API, points the real plugin at it (bearer token from
`AIS_TOKEN`) and asserts the server's render equals `ansible-inventory --host` for every
seeded host. The rest cover the fold, export, auth (401/403, bad-token audit, login
limiter), locking (428/409), temporal writes, move/rename, tags, schema enforcement, import
(dry run, no-op), the audit log, render errors, snapshots/diff and the plugin's 304
revalidation.

The web UI has a Playwright smoke test (sign in, keyboard tree, inherited values, an edit
that hits and recovers from a 409, render, diff, audit, a deep link; then add, move and delete
a node with the key history surviving the move, a schema-checked definition, a tag, a token; deleting a
group confirms its node and host counts; reverting a change; searching a variable from the tree, the
Variables page and a node page, by value, and its history by key) against a throwaway
server on the seed tree; CI runs it in the `web` job:

```bash
cd web && npm ci && npm run build && npx playwright install chromium && npx playwright test
```

The collection has `ansible-test sanity`, unit tests for what a live server can't provoke on
demand (the `tag` module's retry after a stale 409, against a fake server; no API needed), and
one integration target per module (check mode, real change, idempotent repeat, failure cases)
that CI runs against a live server:

```bash
cd collection/ansible_collections/git_cgallagher/inventory
../../../../.venv/bin/ansible-test sanity --requirements --python 3.14
../../../../.venv/bin/ansible-test units --requirements --python 3.14
cp tests/integration/integration_config.yml.template tests/integration/integration_config.yml   # url + editor token
../../../../.venv/bin/ansible-test integration --python 3.14
```

## Scale

`docs/scale.md` has the numbers for 100,000 hosts on PostgreSQL (full export 3.3–3.5 s at any
revision; subtree export 0.06 s; render 0.02 s; the background snapshot pass 7 s after a write,
storing only changed hosts; `ansible-inventory --graph` 4.5 s with a warm ETag store; variable
search 0.02 s for where a key is set, 1.3–1.7 s for every host's value, 0.03 s per datacenter), what still costs,
and how to rerun them (`bench/generate.py`, `bench/benchmark.py`).

## Layout

| Path | What |
|---|---|
| `server/ais/` | FastAPI app (`api*.py`), models, Alembic `migrations/`, resolver (fold/export/resolve), `tree.py` (tree export in the import format), `restore.py`, `revert.py` (revert engine), `render.py`, `snapshots.py`, `auth.py`/`oidc.py`/`locking.py`, importer, CLI |
| `server/tests/` | fold property tests, export, tree export round trips, auth, writes/locking, render, snapshots, plugin ETag, Ansible parity |
| `collection/ansible_collections/git_cgallagher/inventory/` | inventory plugin `db`, modules `node`/`host`/`variable`/`variable_definition`/`node_move`/`import`/`revert`/`node_info`/`render_info`, `module_utils/client.py`, unit tests (`tests/unit`), integration targets |
| `web/` | React + TypeScript UI (Vite; builds into `server/ais/static`), Playwright smoke in `e2e/` |
| `examples/` | `seed.yml` tree, `inventory.yml`, `ansible.cfg` |
| `docs/getting-started.md` | the long walk: concepts, deployment, design, operations |
| `bench/`, `docs/scale.md` | synthetic 100k-host generator, benchmark, compose overlay; results |
| `deploy/` | `compose.yml` + `Dockerfile` (PostgreSQL + API; the image builds the UI in a Node stage); `compose.prod.yml` production overrides |

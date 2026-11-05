# Ansible Inventory System

[![CI](https://github.com/git-cgallagher/ansible-inventory-system/actions/workflows/ci.yml/badge.svg)](https://github.com/git-cgallagher/ansible-inventory-system/actions/workflows/ci.yml)

An authoritative, database-backed Ansible inventory with a **strict tree hierarchy**
(`all → business → tier → datacenter → group → … → host`), a long precedence chain,
per-variable merge strategies, revision history, and a render endpoint that shows what
any real or hypothetical host resolves to.

**Status: 0.3.0 (2026-11-05): a security fix to the render (see `CHANGELOG.md`), per-host history stored only on change, depth and path limits. 0.2.0 was the first release (2026-11-01).** Phase 4 closed the write gaps (tags, `node_move`
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
  target, revision, request id and time, newest first.
- **Move/rename** (`POST /api/v1/nodes/{id}/move`) rewrites the subtree's paths as new
  temporal rows, so the previous revision still shows the old tree; node ids change.
- **Tags** (`PUT /api/v1/tags/{name}` with member paths) are extra var-less groups under `all`
  for targeting (`--limit canary`); a non-host member contributes every host under it.
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
- **Structure** (editor role): add a child, move or rename a node with its subtree, delete a
  subtree, from the node page.
- **Variables**: declare a variable's merge strategy, description and JSON Schema.
- **Tags**: create, edit and delete tags and their member paths.
- **Tokens**: mint API tokens for the plugin and the modules (capped role, expiry; the secret is
  shown once) and revoke them.
- **Diff** a host between any two revisions, and browse **revisions** and the **audit log**
  (editor role), including "history" links from a node or a key that follow it across moves and
  renames.

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

    - name: What would a host there see?
      git_cgallagher.inventory.render_info:
        path: all.commercial.prod.dc1.cache
        facts: {default_ipv4: {address: 10.1.3.99}}
      register: mock

    - git_cgallagher.inventory.node_info:
        path: all.commercial.prod.dc1
      register: dc1

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
```

Tags have an API (`PUT /api/v1/tags/{name}`) but no module yet.

### Install the collection

The collection is **not on Ansible Galaxy**. Each version is a GitHub release with the built
tarball attached; install it from there:

```bash
ansible-galaxy collection install \
  https://github.com/git-cgallagher/ansible-inventory-system/releases/download/v0.3.0/git_cgallagher-inventory-0.3.0.tar.gz
```

or pin it in a `requirements.yml`:

```yaml
collections:
  - name: https://github.com/git-cgallagher/ansible-inventory-system/releases/download/v0.3.0/git_cgallagher-inventory-0.3.0.tar.gz
    type: url
```

From a checkout: `ansible-galaxy collection install
./collection/ansible_collections/git_cgallagher/inventory` (or point `collections_path` at
`collection/`, as `examples/ansible.cfg` does). The collection and the server share a version.

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
| Reads | `GET /api/v1/nodes[?subtree=&revision=]`, `/nodes/{id-or-path}`, `/nodes/{id}/children`, `/nodes/{id}/vars/{key}`, `/variables[/{name}]`, `/revisions` | viewer |
| Snapshots | `GET /api/v1/hosts/{name}/snapshots` (the revisions where the host changed), `/hosts/{name}/snapshots/{revision}` (its state at any revision; `stored_revision` says which stored row it came from, null when computed on demand), `GET /api/v1/diff?host=&from=&to=`, `GET /api/v1/snapshots/status` | viewer |
| Tags | `GET /api/v1/tags[/{name}]` (viewer); `PUT /tags/{name}` (`description`, `members` = node paths), `DELETE /tags/{name}` (editor, `If-Match`) | viewer / editor |
| Writes (`If-Match`) | `POST /api/v1/nodes`, `PATCH/DELETE /nodes/{id}`, `POST /nodes/{id}/move`, `PUT/DELETE /nodes/{id}/vars/{key}`, `PUT/DELETE /variables/{name}`, `POST /import` (`dry_run: true` needs no `If-Match`; a no-op answers 200 `changed: false`) | editor |
| Audit | `GET /api/v1/audit?actor=&action=&target_type=&target_id=&revision=&request_id=&since=&until=&limit=&before=`; `node=<id or path>[&key=]` = that node's (and its variables') rows under every id it had | editor |
| Auth | `POST /auth/login`, `POST /auth/logout`, `GET /auth/me`, `POST/GET/DELETE /tokens`; `GET/POST/PATCH /users` | any / admin |
| Open | `GET /healthz` (`status`, `version`, `oidc`), `GET /` + `/assets/…` (the built web UI, static files only) | none |

Requests may send `X-Request-ID` (echoed, stored on audit rows) and `X-AIS-Source: module|ui`
(recorded as the revision's and audit row's `source`).

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
a node with the key history surviving the move, a schema-checked definition, a tag, a token) against a throwaway
server on the seed tree; CI runs it in the `web` job:

```bash
cd web && npm ci && npm run build && npx playwright install chromium && npx playwright test
```

The collection has `ansible-test sanity` and one integration target per module (check
mode, real change, idempotent repeat, failure cases) that CI runs against a live server:

```bash
cd collection/ansible_collections/git_cgallagher/inventory
../../../../.venv/bin/ansible-test sanity --requirements --python 3.14
cp tests/integration/integration_config.yml.template tests/integration/integration_config.yml   # url + editor token
../../../../.venv/bin/ansible-test integration --python 3.14
```

## Scale

`docs/scale.md` has the numbers for 100,000 hosts on PostgreSQL (full export 3.3–3.5 s at any
revision; subtree export 0.06 s; render 0.02 s; the background snapshot pass 7 s after a write,
storing only changed hosts; `ansible-inventory --graph` 4.5 s with a warm ETag store), what still costs,
and how to rerun them (`bench/generate.py`, `bench/benchmark.py`).

## Layout

| Path | What |
|---|---|
| `server/ais/` | FastAPI app (`api*.py`), models, Alembic `migrations/`, resolver (fold/export/resolve), `render.py`, `snapshots.py`, `auth.py`/`oidc.py`/`locking.py`, importer, CLI |
| `server/tests/` | fold property tests, export, auth, writes/locking, render, snapshots, plugin ETag, Ansible parity |
| `collection/ansible_collections/git_cgallagher/inventory/` | inventory plugin `db`, modules `node`/`host`/`variable`/`variable_definition`/`node_move`/`import`/`node_info`/`render_info`, `module_utils/client.py`, integration targets |
| `web/` | React + TypeScript UI (Vite; builds into `server/ais/static`), Playwright smoke in `e2e/` |
| `examples/` | `seed.yml` tree, `inventory.yml`, `ansible.cfg` |
| `docs/getting-started.md` | the long walk: concepts, deployment, design, operations |
| `bench/`, `docs/scale.md` | synthetic 100k-host generator, benchmark, compose overlay; results |
| `deploy/` | `compose.yml` + `Dockerfile` (PostgreSQL + API; the image builds the UI in a Node stage); `compose.prod.yml` production overrides |

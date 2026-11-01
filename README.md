# Ansible Inventory System

[![CI](https://github.com/git-cgallagher/ansible-inventory-system/actions/workflows/ci.yml/badge.svg)](https://github.com/git-cgallagher/ansible-inventory-system/actions/workflows/ci.yml)

An authoritative, database-backed Ansible inventory with a **strict tree hierarchy**
(`all → business → tier → datacenter → group → … → host`), a long precedence chain,
per-variable merge strategies, revision history, and a render endpoint that shows what
any real or hypothetical host resolves to.

**Status: phase 1, core API (2026-11-01).** Bearer-token auth with roles, locked write
endpoints, Jinja render with mock facts, per-host snapshots and diffs, Alembic migrations
and an ETag-revalidating inventory plugin, on top of the phase-0 export/import/render
core. Still tested by a golden test that the server's render equals
`ansible-inventory --host`. No web UI or Ansible modules yet. See
`ANSIBLE-INVENTORY-PLAN.md` in `~/Code` for the full plan and `AGENTS.md` here for how
to work on it.

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
- **Revisions** are immutable; nodes and variables are temporal rows, so any revision
  can be exported or rendered exactly as it was. The plugin can pin one, and sets
  `inventory_revision` on `all` so every run records what it ran against.
- **Writes are optimistically locked**: every write carries `If-Match: "<revision>"`
  and is refused (409) when the inventory moved underneath it.
- **Auth**: local users (scrypt) and bearer tokens hashed at rest; roles `viewer`
  (export/render), `editor` (writes), `admin` (users). OIDC is a config-flag stub.

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

API docs at `http://127.0.0.1:8000/docs`. Every `/api/v1` call needs
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

### PostgreSQL (compose)

```bash
docker compose -f deploy/compose.yml up --build      # API on :8000, migrations run at start
docker compose -f deploy/compose.yml exec api ais user create admin@example.com --role admin --no-password
docker compose -f deploy/compose.yml exec api ais token create --user admin@example.com
```

### API overview

| Area | Routes | Role |
|---|---|---|
| Export / render | `GET /api/v1/export` (ETag, `If-None-Match` → 304), `POST /api/v1/render` (`host` or mock `path`, `overrides`, `facts`, `revision`) | viewer |
| Reads | `GET /api/v1/nodes[?subtree=&revision=]`, `/nodes/{id}`, `/nodes/{id}/children`, `/nodes/{id}/vars/{key}`, `/variables[/{name}]`, `/revisions` | viewer |
| Snapshots | `GET /api/v1/hosts/{name}/snapshots[/{revision}]`, `GET /api/v1/diff?host=&from=&to=` | viewer |
| Writes (`If-Match`) | `POST /api/v1/nodes`, `PATCH/DELETE /nodes/{id}`, `PUT/DELETE /nodes/{id}/vars/{key}`, `PUT/DELETE /variables/{name}`, `POST /import` | editor |
| Auth | `POST /auth/login`, `POST /auth/logout`, `GET /auth/me`, `POST/GET/DELETE /tokens`; `GET/POST/PATCH /users` | any / admin |

## Tests

```bash
cd server && ../.venv/bin/python -m pytest
AIS_TEST_DATABASE_URL=postgresql+psycopg://ais:ais@127.0.0.1:55432/ais_test ../.venv/bin/python -m pytest   # same suite on PostgreSQL
```

`tests/test_parity.py` starts the API, points the real plugin at it (bearer token from
`AIS_TOKEN`) and asserts the server's render equals `ansible-inventory --host` for every
seeded host. The rest cover the fold, export, auth (401/403), locking (428/409),
temporal writes, render errors, snapshots/diff and the plugin's 304 revalidation.

## Layout

| Path | What |
|---|---|
| `server/ais/` | FastAPI app (`api*.py`), models, Alembic `migrations/`, resolver (fold/export/resolve), `render.py`, `snapshots.py`, `auth.py`/`oidc.py`/`locking.py`, importer, CLI |
| `server/tests/` | fold property tests, export, auth, writes/locking, render, snapshots, plugin ETag, Ansible parity |
| `collection/ansible_collections/git_cgallagher/inventory/` | inventory plugin `db` (modules in phase 2) |
| `examples/` | `seed.yml` tree, `inventory.yml`, `ansible.cfg` |
| `deploy/` | `compose.yml` + `Dockerfile` (PostgreSQL + API) |

# Ansible Inventory System

An authoritative, database-backed Ansible inventory with a **strict tree hierarchy**
(`all → business → tier → datacenter → group → … → host`), a long precedence chain,
per-variable merge strategies, revision history, and a render endpoint that shows what
any real or hypothetical host resolves to.

**Status: phase-0 spike (2026-11-01).** The export API, the inventory plugin, bulk
import, revision pinning and the render endpoint work and are tested, including a
golden test that the server's render equals `ansible-inventory --host`. No auth, no
UI, no write modules yet. See `ANSIBLE-INVENTORY-PLAN.md` in `~/Code` for the full
plan and `AGENTS.md` here for how to work on it.

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
- **Revisions** are immutable; the plugin can pin one, and sets `inventory_revision`
  on `all` so every run records what it ran against.

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -e './server[test]'
cd examples
export AIS_DATABASE_URL=sqlite:///./ais.db
../.venv/bin/ais import seed.yml
../.venv/bin/ais serve &            # http://127.0.0.1:8000
../.venv/bin/ansible-inventory --graph         # uses examples/ansible.cfg
../.venv/bin/ansible-inventory --host web02
../.venv/bin/ais render --path all.commercial.lab.dc1.web   # mock host
```

API docs at `http://127.0.0.1:8000/docs`.

## Tests

```bash
cd server && ../.venv/bin/python -m pytest
```

`tests/test_parity.py` starts the API, points the real plugin at it and asserts the
server's render equals `ansible-inventory --host` for every seeded host.

## Layout

| Path | What |
|---|---|
| `server/ais/` | FastAPI app, SQLAlchemy models, resolver (fold/export/resolve), importer, CLI |
| `server/tests/` | fold property tests, export tests, Ansible parity test |
| `collection/ansible_collections/git_cgallagher/inventory/` | inventory plugin `db` (modules in phase 2) |
| `examples/` | `seed.yml` tree, `inventory.yml`, `ansible.cfg` |
| `deploy/` | `compose.yml` + `Dockerfile` (PostgreSQL + API) |

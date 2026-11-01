# AGENTS.md — ansible-inventory-system

Read `~/Code/AGENTS.md` first (workspace rules). Design: `~/Code/ANSIBLE-INVENTORY-PLAN.md`.

## What this is
Authoritative database-backed Ansible inventory. Strict tree; Ansible merges natively;
the server only folds declared merge/append variables. **Phase-0 spike** as of
2026-11-01: export + plugin + import + render + tests. No auth, UI or write modules yet.

## Invariants (do not break)
- **Parity test is load-bearing.** `server/tests/test_parity.py` must keep passing:
  server render == `ansible-inventory --host`. Any change to the resolver, exporter or
  plugin that breaks it is wrong by definition, not "a test to update".
- **One path per node.** No multiple parents, no cross-cutting layers. Cross-cutting
  convenience = var-less tag groups only. **Tags never carry variables.**
- **Fold is emitted at every contributing node as the cumulative fold** from the root
  (`resolver.emitted_vars`). Not at the host, not only at the deepest node.
- **Group names are qualified** from the path with `_`; collisions are an export error.
- **Variables are temporal rows**; never UPDATE a value in place — close the row with
  `deleted_rev` and insert a new one under a new `Revision`.
- Never read `*.db` test artefacts into docs; never commit `examples/ais.db`.

## Commands
```bash
python3 -m venv .venv && .venv/bin/pip install -e './server[test]'
cd server && ../.venv/bin/python -m pytest -q          # 14 tests, ~4 s
cd examples && AIS_DATABASE_URL=sqlite:///./ais.db ../.venv/bin/ais import seed.yml
AIS_DATABASE_URL=sqlite:///./ais.db ../.venv/bin/ais serve      # from examples/
../.venv/bin/ansible-inventory --graph                           # examples/ansible.cfg sets collections_path
docker compose -f deploy/compose.yml up --build                  # PostgreSQL + API on :8000
```
The venv is Python 3.14 + ansible-core 2.21.5 (no system ansible on this Mac).

## Structure
- `server/ais/models.py` — `Node` (kind, path, depth), `NodeVariable` (temporal),
  `VariableDefinition` (merge strategy), `Revision`, `Tag`/`TagMember`. `LEVEL_ORDER`
  is the level schema; `group` may nest; `TAGGED_KINDS` get auto tag groups.
- `server/ais/resolver.py` — `fold`, `load_snapshot(revision, subtree)`,
  `build_export`, `resolve(host|path, overrides)`.
- `server/ais/importer.py` — YAML tree → nodes/vars under one revision; kind inferred
  from depth unless declared; rejects bad kind order.
- `server/ais/api.py`, `server/ais/cli.py`.
- `collection/.../plugins/inventory/db.py` — the plugin. Accepts `inventory.yml` /
  `ais.yml` file names.

## ⚠️ Gotchas
- `ansible-inventory` is not on PATH in agent shells; tests resolve it next to
  `sys.executable` (the venv). Run pytest with the venv's python.
- Subtree export keeps the ancestor chain (`all` → … → subtree root) so inherited vars
  still apply; it is not just "nodes under X".
- The spike stores `path` as text (works on SQLite and PostgreSQL). `ltree` is a later
  index optimisation, not a semantic change.
- `hash_behaviour=merge` must stay off on consumers; the server already folds.

## TODO (phases 1–4, see the plan)
- Phase 1: auth (local + OIDC), API tokens, per-node write endpoints with `If-Match`
  revision locking, Jinja render (`Templar`) with mock facts, `host_snapshot`, Alembic
  migrations, ltree on PostgreSQL, ETag/304 in the plugin.
- Phase 2: modules `node`, `variable`, `variable_definition`, `host`, `*_info`, `import`;
  audit log; ansible-test sanity/integration on the shared Molecule driver.
- Phase 3: web UI (tree, editor, render real/mock, diff, audit).
- Phase 4: RBAC roles, backups, CI, release; GitHub repo + public-repo baseline
  (visibility undecided; owner go needed before creating the repo).

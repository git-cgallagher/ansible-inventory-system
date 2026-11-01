# AGENTS.md — ansible-inventory-system

Read `~/Code/AGENTS.md` first (workspace rules). Design: `~/Code/ANSIBLE-INVENTORY-PLAN.md`.

## What this is
Authoritative database-backed Ansible inventory. Strict tree; Ansible merges natively;
the server only folds declared merge/append variables. **Phase 1 (core API) done
2026-11-01**: bearer-token auth with roles, locked write endpoints, Jinja render with mock
facts, host snapshots + diff, Alembic migrations, ltree on PostgreSQL, ETag/304 in the
plugin. No UI, no write modules, no audit log yet (phases 2–4).

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
- **Nodes and variables are temporal rows**; never UPDATE a value in place or hard-delete —
  close the row with `deleted_rev` and insert a new one under a new `Revision`. `DELETE
  /nodes/{id}` closes the whole subtree and its live variables. A pinned revision therefore
  sees exactly the tree of that moment. (`inventory_name`, `meta` and variable *definitions*
  are the documented exceptions: not temporal, changes apply to every revision's export.)
- **Every write needs `If-Match: "<revision>"`** (`ais.locking.begin_write`: lock → compare
  with `current_revision` → new `Revision`). 428 without, 409 when stale. Never add a write
  route that skips it.
- **Every `/api/v1` route needs a principal** (`Depends(require(Role.x))`); reads = viewer,
  writes = editor, users = admin. `/healthz` is the only open route. There is deliberately no
  "auth off" switch.
- **The schema is owned by `ais/migrations`**, not `create_all`. A model change needs a new
  migration (`alembic -c server/alembic.ini revision --autogenerate -m …`, then review it);
  the drift check is "migrated SQLite schema == `Base.metadata`" (columns + indexes).
- Never read `*.db` test artefacts into docs; never commit `examples/ais.db`.

## Repo and CI
Public repo `git-cgallagher/ansible-inventory-system` (created 2026-11-01; full new-public-repo
baseline from `~/Code/AGENTS.md`). CI `ci.yml`: job `test` (pytest incl. parity, Python 3.12 +
3.14, plus an `ansible-inventory --graph` smoke) and job `image` (docker build). Both are required
by the "main protection" ruleset; the "external contributor review" ruleset means your own PRs merge
with `gh pr merge --admin` once every check is green. Work on branches, never push to `main`.
**Several sessions may share this checkout**: never switch branches in the shared folder for side
work; use `git worktree add` in the scratchpad instead.

## Commands
```bash
python3 -m venv .venv && .venv/bin/pip install -e './server[test,postgres,oidc]'
cd server && ../.venv/bin/python -m pytest -q          # 41 tests, ~8 s (SQLite)
cd examples && export AIS_DATABASE_URL=sqlite:///./ais.db
../.venv/bin/ais import seed.yml                       # also runs the migrations
../.venv/bin/ais user create me@example.com --role admin   # prompts; --no-password for token-only
export AIS_TOKEN="$(../.venv/bin/ais token create --user me@example.com)"   # shown once
../.venv/bin/ais serve                                 # from examples/
../.venv/bin/ansible-inventory --graph                 # examples/ansible.cfg sets collections_path; token from AIS_TOKEN
docker compose -f deploy/compose.yml up --build        # PostgreSQL + API on :8000 (verified 2026-11-01, phase 1)
docker compose -f deploy/compose.yml exec api ais user create admin@example.com --role admin --no-password
docker compose -f deploy/compose.yml exec api ais token create --user admin@example.com
# whole suite against PostgreSQL (publish 5432 with an override file; schema is dropped per test):
AIS_TEST_DATABASE_URL=postgresql+psycopg://ais:ais@127.0.0.1:55432/ais_test ../.venv/bin/python -m pytest -q
```
The venv is Python 3.14 + ansible-core 2.21.5 (no system ansible on this Mac). CI also runs
3.12, so no 3.13+-only syntax.

## Structure
- `server/ais/models.py` — `Node` (kind, path, depth, temporal), `NodeVariable` (temporal),
  `VariableDefinition` (merge strategy), `Revision`, `Tag`/`TagMember`, `User`
  (`user_account`), `ApiToken`, `HostSnapshot`. `LEVEL_ORDER` is the level schema; `group`
  may nest; `TAGGED_KINDS` get auto tag groups; `NAME_RE` = ltree-safe node names.
- `server/ais/migrations/` — Alembic (`env.py` takes a connection from
  `ais.db.init_db`; `0001` = spike schema, `0002` = phase 1 incl. the PostgreSQL ltree
  branch). `server/alembic.ini` is for hand use only.
- `server/ais/resolver.py` — `fold`, `load_snapshot(revision, subtree)` (revision-aware for
  nodes and vars), `build_export`, `resolve(host|path, overrides)`.
- `server/ais/render.py` — `render_templates(resolved, facts, hostname, chain)` → Templar
  over trusted strings, `(rendered, errors)`.
- `server/ais/snapshots.py` — `materialize`, `get_snapshot` (on demand), `diff`.
- `server/ais/auth.py` (hashing, tokens, `Principal`, `require(role)`), `server/ais/oidc.py`
  (stub), `server/ais/locking.py` (`If-Match`, advisory lock, `begin_write`).
- `server/ais/api.py` (reads, export, render, snapshots, import), `api_write.py` (locked
  writes), `api_auth.py` (login, tokens, users), `cli.py`.
- `server/ais/importer.py` — YAML tree → nodes/vars under one revision; kind inferred
  from depth unless declared; rejects bad kind order and names.
- `collection/.../plugins/inventory/db.py` — the plugin (`fetch_export` does the ETag
  revalidation; `etag_store` option). Accepts `inventory.yml` / `ais.yml` file names.
- `server/tests/conftest.py` — `engine` (SQLite or `AIS_TEST_DATABASE_URL`), role tokens,
  `AuthClient` (sends the bearer, `if_match=` kwarg), live `server` + real-plugin helpers.

## ⚠️ Gotchas
- `ansible-inventory` is not on PATH in agent shells; tests resolve it next to
  `sys.executable` (the venv). Run pytest with the venv's python.
- Subtree export keeps the ancestor chain (`all` → … → subtree root) so inherited vars
  still apply; it is not just "nodes under X".
- `path` is text on both databases; on PostgreSQL `path_ltree` is a *generated* column
  (`text2ltree(path)`, GiST) that the resolver does not use yet. It is why node names are
  limited to `[A-Za-z0-9_-]` (ltree labels; hyphens need PostgreSQL 16+). It is migration-only,
  not in the ORM model, or SQLite would break.
- `hash_behaviour=merge` must stay off on consumers; the server already folds.
- **ansible-core 2.19+ only templates strings tagged trusted.** `Templar.template("{{ x }}")`
  on a plain `str` returns it unchanged, silently. `ais.render._trust` wraps every string with
  `trust_as_template` first; catch `AnsibleTemplateError` (covers undefined, syntax,
  recursion). Learned 2026-11-01 (first render returned raw templates with no error).
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
- The export `ETag` includes a digest of `subtree` / `group_separator` / `emit_tags`, so a
  tag is only valid for the same parameters; the plugin's store is keyed per URL.
- `~/.cache/ais/export-*.json` (the plugin's ETag store) holds a full inventory copy per
  source on the controller. `AIS_ETAG_STORE=""` disables it (tests use a tmp dir).
- The CI plugin smoke serves on :8000; running `docker compose up` at the same time makes two
  servers fight over the port and the smoke reads the wrong one. Run one at a time.

## TODO (phases 2–4, see the plan)
- Phase 1 leftovers: OIDC is a stub that has never run against a real provider (set the
  four `AIS_OIDC_*`/`AIS_SECRET_KEY` vars and test before relying on it); node rename/move
  (`POST /nodes/{id}/move`) is not implemented (PATCH only changes `inventory_name`/`meta`);
  tags have no write API yet (DB only); no rate limiting on login; the resolver does not
  use `path_ltree` yet.
- Phase 2: modules `node`, `variable`, `variable_definition`, `host`, `*_info`, `import`;
  audit log; ansible-test sanity/integration on the shared Molecule driver.
- Phase 3: web UI (tree, editor, render real/mock, diff, audit).
- Phase 4: RBAC roles, backups, release automation.

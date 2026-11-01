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
revision and audit browsers. Playwright smoke in CI (job `web`).

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
  writes = editor, users = admin, **audit = editor** (viewers must not see who changed what, or
  the failed-login e-mails). `/healthz` is the only open route. There is deliberately no
  "auth off" switch.
- **Every effective write and every auth event leaves exactly one `audit_log` row, in the same
  transaction** (`ais.audit.record`, after `check_export`). A refused write (409/422/428) therefore
  leaves none, and a no-op (`PUT` of an unchanged var or definition) leaves none either. The one
  deliberate exception to "raise to refuse": a failed login *returns* its 401 so the row commits.
  `before`/`after` never contain a secret (token plaintext, password hash). New write routes call
  `_audit` (or `audit.record`) or they are wrong.
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
  only `GET /` (index, `no-cache`) and `GET /assets/{path}` (hashed files, `immutable`,
  traversal-guarded). Routing is by hash (`#/tree?path=…`), so there is **no SPA catch-all**:
  unknown paths stay 404 and no API route can be shadowed (`server/tests/test_web.py`).
  A missing build makes `/` a 404 and nothing else (the `test` job runs without one).
- **UI writes carry `If-Match` = the revision the page shows** (from the render that loaded the
  values), never a revision fetched just before the write, which would defeat the lock. A stale
  409 reloads the values and keeps the user's text; the UI sends `X-AIS-Source: ui`.
- **Node detail reads through `POST /render {path, template: false}`**, also for hosts: a path
  render has no side effects, a host render materializes a snapshot row on every click. Own vars
  are the provenance entries whose `path` is the node's; ids go in no URL (a move changes them).

## Repo and CI
Public repo `git-cgallagher/ansible-inventory-system` (created 2026-11-01; full new-public-repo
baseline from `~/Code/AGENTS.md`). CI `ci.yml`: job `test` (pytest incl. parity, Python 3.12 +
3.14, plus an `ansible-inventory --graph` smoke), job `image` (docker build) and job `collection`
(`ansible-test sanity` + the six module integration targets against a live `ais serve` on SQLite,
Python 3.12; the step mints an editor token into the gitignored `integration_config.yml`) and job
`web` (`npm ci`, `npm run build` = `tsc` + Vite, then the Playwright smoke against `e2e/serve.sh`;
the report is uploaded on failure). The `image` job builds the UI too (Node stage in the
Dockerfile). All are required by the "main protection" ruleset (id `26318946`; `collection` and
`web` added 2026-11-01 after their first green runs); the "external contributor review" ruleset means your own PRs merge with
`gh pr merge --admin` once every check is green. Work on branches, never push to `main`.
**Several sessions may share this checkout**: never switch branches in the shared folder for side
work; use `git worktree add` in the scratchpad instead.

## Commands
```bash
python3 -m venv .venv && .venv/bin/pip install -e './server[test,postgres,oidc]'
cd server && ../.venv/bin/python -m pytest -q          # 51 tests, ~8 s (SQLite)
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
# schema drift check (should print only the two known spike-era nullable diffs, see TODO):
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
  branch, `0003` = `audit_log`). `server/alembic.ini` is for hand use only.
- `server/ais/audit.py` — `RequestIdMiddleware` (`X-Request-ID` in/out, `X-AIS-Source` →
  `source`, both as context variables), `record(session, …)`, `audit_out`, `as_utc` (query
  datetimes: naive = UTC, so `since`/`until` work on SQLite's naive strings and PostgreSQL's
  timestamptz alike).
- `server/ais/resolver.py` — `fold`, `load_snapshot(revision, subtree)` (revision-aware for
  nodes and vars), `build_export`, `resolve(host|path, overrides)`.
- `server/ais/render.py` — `render_templates(resolved, facts, hostname, chain)` → Templar
  over trusted strings, `(rendered, errors)`.
- `server/ais/snapshots.py` — `materialize`, `get_snapshot` (on demand), `diff`.
- `server/ais/auth.py` (hashing, tokens, `Principal`, `require(role)`), `server/ais/oidc.py`
  (stub), `server/ais/locking.py` (`If-Match`, advisory lock, `begin_write`).
- `server/ais/api.py` (reads incl. `GET /nodes/{id-or-path}`, export, render, snapshots,
  `GET /audit`, import), `api_write.py` (locked writes incl. `move`), `api_auth.py` (login,
  tokens, users; all audited), `cli.py` (`user`/`token` commands audit with `source=cli`).
- `server/ais/importer.py` — YAML tree → nodes/vars under one revision; kind inferred
  from depth unless declared; rejects bad kind order and names.
- `collection/.../plugins/inventory/db.py` — the plugin (`fetch_export` does the ETag
  revalidation; `etag_store` option). Accepts `inventory.yml` / `ais.yml` file names.
- `collection/.../plugins/module_utils/client.py` — `Client` (token/url from options or
  `AIS_TOKEN`/`AIS_URL`, `request`, `write` with `If-Match`, 409 stale vs other 409, 401/403),
  `ensure_node`/`remove_node` (shared by `node` and `host`). `doc_fragments/api.py` holds the
  `api` and `api.write` fragments; `meta/runtime.yml` declares the `inventory` action group
  (`module_defaults: group/git_cgallagher.inventory.inventory`).
- `collection/.../plugins/modules/` — `node`, `host`, `variable`, `variable_definition` (write,
  check + diff mode), `node_info`, `render_info`. `tests/integration/targets/<module>` = check,
  real, repeat, failure cases with an `always:` cleanup, all against one shared server (unique
  `it_*`/`it-*` names per target). `tests/sanity/ignore-2.*.txt` only waive the GPLv3-header
  check (the collection is MIT).
- `server/ais/api.py` (end) — `web_dir()` (`AIS_WEB_DIR` or `ais/static`), `GET /`, `GET /assets/…`;
  `/healthz` reports `oidc`. `ais/oidc.py` `ui_handoff()`: with `/login?ui=1` the callback answers
  with a page that puts the session into `sessionStorage['ais.session']` and goes to `/`.
- `web/` — Vite + React 19 + TypeScript, no component library (deps: `react`, `react-dom`,
  `yaml`). `src/api.ts` (fetch wrapper: bearer, `If-Match`, `X-AIS-Source: ui`, `ApiError.stale`
  for the 409 body `{error, expected, current}`, any 401 → login), `src/router.ts` (hash routes),
  `src/views/` (`Login`, `Tree` (lazy children, ARIA tree keys), `NodeDetail`, `VarEditor`,
  `Render`, `Diff`, `Revisions`, `Audit`), `src/styles.css` (tokens, dark via
  `prefers-color-scheme`). `vite.config.ts` sets `outDir: ../server/ais/static`.
  `e2e/smoke.spec.ts` + `e2e/serve.sh` + `playwright.config.ts`.
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
  asserts `ais/static/index.html` exists after `pip install`. A new asset subfolder needs a glob.
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

## TODO (phase 4 and leftovers, see the plan)
- Phase 1/2 leftovers: OIDC is a stub that has never run against a real provider (set the
  four `AIS_OIDC_*`/`AIS_SECRET_KEY` vars and test before relying on it); tags have no write
  API yet (DB only); no rate limiting on login (failed logins are at least audited now); the
  resolver does not use `path_ltree` yet; no `move` module and no `import` module/action (the
  endpoints exist: `POST /nodes/{id}/move`, `POST /import`); bad-token requests are not audited
  (only failed logins are); `json_schema` on definitions is stored, not enforced.
- Alembic autogenerate reports two spike-era nullability diffs (`node.meta`,
  `node_variable.value` are NOT NULL in the models, nullable in `0001`); harmless, fix in a
  `0004` when the next model change comes.
- Phase 3 leftovers: the OIDC button and `ui_handoff` are as unverified as the rest of OIDC; the
  UI cannot create, move or delete nodes, declare variables or manage tags (no tag write API yet)
  or API tokens (plan §7 lists token management; the endpoints exist); "history" links on a key
  filter the audit log by `<node id>/<key>`, so they miss rows from before a move (ids change);
  one ~350 kB bundle, no code splitting; tree children have no count, so every non-host node shows
  an expander until opened.
- Phase 4: RBAC roles, backups, release automation (Galaxy publish of the collection).

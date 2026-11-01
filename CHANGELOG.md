# Changelog

## Unreleased

### Added
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

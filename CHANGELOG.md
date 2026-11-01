# Changelog

## Unreleased

### Added
- Phase-0 spike: strict-tree data model with temporal variables and revisions;
  per-variable merge strategies (`replace`, `merge`, `append`, `unique_append`) folded
  server-side at contributing nodes; `/api/v1/export`, `/api/v1/render` (real or mock
  host, with provenance), `/api/v1/nodes`, `/api/v1/import`; `ais` CLI; YAML bulk
  importer; inventory plugin `git_cgallagher.inventory.db` (subtree, revision pin,
  cache plugin support, tag groups); compose deployment; tests incl. the
  `ansible-inventory --host` parity test.

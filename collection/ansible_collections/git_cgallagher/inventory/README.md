# git_cgallagher.inventory

Inventory plugin and modules for the Ansible Inventory System (database-backed strict-tree
inventory). Every plugin talks to the server's `/api/v1` with a bearer token (`AIS_TOKEN`,
or the `token` option).

## Inventory plugin `db`

```yaml
# inventory.yml
plugin: git_cgallagher.inventory.db
url: http://127.0.0.1:8000
# token: via AIS_TOKEN
# subtree: all.commercial.prod
# revision: 42
cache: true
cache_plugin: jsonfile
cache_connection: ~/.cache/ais
```

```bash
ansible-inventory -i inventory.yml --graph
ansible-inventory -i inventory.yml --host web01
```

## Modules

| Module | Does | Needs |
|---|---|---|
| `node` | create / converge (vars, meta, inventory_name, purge) / delete a node and its subtree | editor |
| `host` | create / converge / delete a host (`ansible_host`, `ansible_port`, `ansible_user`, vars) | editor |
| `variable` | set or remove one variable at a node | editor |
| `variable_definition` | declare a variable's merge strategy (`replace`, `merge`, `append`, `unique_append`) and `json_schema` (enforced on every write) | editor |
| `tag` | create / converge / delete a tag (a var-less targeting group): `paths` added, or exactly them with `exclusive: true`, `remove_paths` taken out; the group turns `-` into `_` (`gpu-canary` → `gpu_canary`); retries once on a stale 409 when the tag still differs | editor |
| `profile` | create / converge / delete a profile (a named bundle of variables applied at every node it is attached to, between the node's parent and the node): `vars` set (`purge_vars: true` = exactly them), `attach`/`detach` node paths (appended last in each node's list), `state: absent` (`detach_all: true` while attached); check mode lists every host a value change reaches | editor |
| `node_move` | move and/or rename a node with its subtree (`path` → `new_path`; ids change) | editor |
| `import` | bulk-import a tree (the `ais import` format) under one revision; additive; check mode is a server dry run | editor |
| `revert` | undo one revision (`revision`) or every revision of a change id (`revert_change`) as one revision; refuses on later conflicting writes unless `force`; check mode is a server dry run; already reverted = not changed | editor |
| `node_info` | a node with its variables and children (missing is not an error) | viewer |
| `render_info` | effective variables of a real host or a mock host at a path, with provenance and Jinja render | viewer |
| `tag_info` | one tag (`name`; missing is not an error) or every tag | viewer |
| `profile_info` | one profile (`name`, optional `revision`; values and where attached) or every profile | viewer |
| `variable_info` | where a key is set (nodes and their own values) and, with `hosts: true`, every host's resolved value with the node it comes from; `value` filters (JSON-typed: `"550.90"` is not `550.90`), `subtree`, `revision`, `limit` | viewer |

All writes are idempotent, support check mode and `--diff` (check mode is the server's dry run
of each write: the server's checks apply, nothing is written, and the result's `impact` lists
the hosts the change would reach; `--diff` prints a summary), and are optimistically locked:
the module reads the server's current revision (or takes `expect_revision`), sends it as
`If-Match`, and fails with both revisions if the inventory moved in between. Writes are audited
server-side with `source=module` and a per-task request id. `revision_message` is recorded on
the revision, and `change` (every module, or `AIS_CHANGE`) is sent as `X-AIS-Change` and recorded
as the revision's change id, so the revisions of one change can be listed
(`GET /api/v1/revisions?change=`) and undone together with `revert`.

```yaml
- hosts: localhost
  gather_facts: false
  module_defaults:
    group/git_cgallagher.inventory.inventory:
      url: http://127.0.0.1:8000
  tasks:
    - git_cgallagher.inventory.variable_definition:
        name: packages
        merge_strategy: unique_append
    - git_cgallagher.inventory.node:
        path: all.commercial.prod.dc1.cache
        vars: {redis_port: 6379, packages: [redis]}
    - git_cgallagher.inventory.host:
        name: cache01
        parent_path: all.commercial.prod.dc1.cache
        ansible_host: 10.1.3.11
    - git_cgallagher.inventory.variable:
        path: all.commercial.prod
        key: log_level
        value: error
    - git_cgallagher.inventory.render_info:
        host: cache01
      register: out
    - git_cgallagher.inventory.node_info:
        path: all.commercial.prod.dc1
      register: dc1
    - git_cgallagher.inventory.tag:
        name: cache-canary            # emitted as group cache_canary
        paths: [all.commercial.prod.dc1.cache.cache01]
```

## Testing

```bash
cd collection/ansible_collections/git_cgallagher/inventory
ansible-test sanity --requirements --python 3.14
cp tests/integration/integration_config.yml.template tests/integration/integration_config.yml   # url + editor token of a server holding examples/seed.yml
ansible-test integration --python 3.14            # one target per module: check, real, repeat, failure
```

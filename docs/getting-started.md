# Getting started

This guide takes you from nothing to a running inventory server that your playbooks use. It
covers the ideas behind the tree, a first local run, a PostgreSQL deployment, how to model an
estate, every way of writing to it, and how to run it day to day. The [README](../README.md)
is the short reference; this is the long walk.

Everything here was run against 0.3.0. Commands assume a checkout of this repository as the
current directory unless a step says otherwise.

- [1. The ideas in five minutes](#1-the-ideas-in-five-minutes)
- [2. Try it locally](#2-try-it-locally)
- [3. Run it for real (PostgreSQL)](#3-run-it-for-real-postgresql)
- [4. Design your tree](#4-design-your-tree)
- [5. Put data in](#5-put-data-in)
- [6. Declare variables](#6-declare-variables)
- [7. Check a change before it lands](#7-check-a-change-before-it-lands)
- [8. Use it from playbooks](#8-use-it-from-playbooks)
- [9. Secrets](#9-secrets)
- [10. Users, roles and tokens](#10-users-roles-and-tokens)
- [11. History, diffs and the audit log](#11-history-diffs-and-the-audit-log)
- [12. Operating it](#12-operating-it)
- [13. Limits and known gaps](#13-limits-and-known-gaps)
- [14. Troubleshooting](#14-troubleshooting)

## 1. The ideas in five minutes

### One tree, one path per host

Every node lives at exactly one place in a single tree, written as a dotted path:

```
all                                   depth 0  (kind: all)
└── commercial                        depth 1  (kind: business)
    └── prod                          depth 2  (kind: tier)
        └── dc1                       depth 3  (kind: datacenter)
            └── web                   depth 4+ (kind: group, may nest freely)
                ├── web01             leaf     (kind: host)
                └── web02
```

`web01`'s path is `all.commercial.prod.dc1.web.web01`. A node has one parent, never two, so a
variable at `all.commercial.prod` applies to commercial production only. There is no
`all.prod` that both businesses share. That is deliberate: in a strict tree, the order
variables apply in is simply "top to bottom", with no ties to break.

The first three levels below `all` are named by their **kind**: business, tier and
datacenter. When you import a tree the kind comes from the depth. Below a datacenter,
groups nest as deep as you need (`web.edge.eu`, …), and hosts are always leaves.

### Ansible does the merging

The inventory plugin hands Ansible the real group tree with each node's variables on its own
group. Ansible's normal rule, deeper groups override shallower ones, then yields exactly the
top-to-bottom order above. The server never re-implements Ansible's precedence. A test
(`server/tests/test_parity.py`) checks that the server's answer for every host equals
`ansible-inventory --host`. A second test checks that a playbook sees the same templated value
as the server's render.

Group names are the path joined with `_`, minus `all`: `all.commercial.prod.dc1.web` is the
group `commercial_prod_dc1_web`. That keeps `commercial/prod/dc1/web` and
`federal/prod/dc2/web` apart. The plugin also adds variable-free **tag groups** for targeting:

| Tag group | Contains |
|---|---|
| `business_commercial`, `business_federal` | every host under that business |
| `tier_prod`, `tier_lab`, … | every host in that tier, across businesses |
| `datacenter_dc1`, … | every host in that datacenter, across businesses and tiers |
| your own tags (section 5) | every host under the member paths you choose |

Tag groups never carry variables. They exist so that `--limit tier_prod` works. They don't
let a variable sneak in sideways.

### Some variables add up instead of replacing

Ansible replaces a variable wholesale when a deeper group sets it. That's right for
`http_port` but wrong for a package list that each level adds to. Here you declare, per
variable, how it combines down the path:

| Strategy | Combines | Example result for `web02` in `examples/seed.yml` |
|---|---|---|
| `replace` (default) | deepest value wins, as in Ansible | `dns_servers: [10.1.0.2, 10.1.0.3]` (set at `all`, replaced at `dc1`) |
| `merge` | dictionaries merge key by key, recursively | `sysctl: {net.ipv4.ip_forward: 0, vm.swappiness: 10}` (`all` set both, `commercial.prod` changed one) |
| `append` | lists concatenate, ancestors first | — |
| `unique_append` | lists concatenate, skipping items already present | `packages: [vim, curl, awscli, nginx, tcpdump]` (from `all`, `commercial`, `web`, and `web02` itself) |

The server works these out and sends the running total at every node that contributes. So
when Ansible does its usual replace, the deepest node already holds the combined value.
Ansible never needs to know merging happened.

### Every change is a revision

Every write creates an immutable, numbered **revision**. Nothing is overwritten in place, so
any revision can be exported, rendered or diffed exactly as it was. Each write must say which
revision it was based on (`If-Match: "<revision>"`). If someone else wrote first, the write is
refused with 409 rather than silently undoing their change. The modules and the web UI handle
this for you.

### The render answers "what would this host get?"

`POST /api/v1/render` (also the UI's Render page and the `render_info` module) shows a host's
resolved variables. For each one it shows which node it came from, and the result of
evaluating any Jinja in it. It works for real hosts and for **mock hosts**: "a new machine at
`all.federal.prod.dc2.web`", with your own overrides and facts. You can check a change before
any real host exists.

## 2. Try it locally

You need Python 3.12–3.14 (not 3.15: ansible-core 2.21 does not import there) and `git`. This
uses SQLite and needs no other services.

```bash
git clone https://github.com/git-cgallagher/ansible-inventory-system.git
cd ansible-inventory-system
python3 -m venv .venv && .venv/bin/pip install -e ./server
export PATH="$PWD/.venv/bin:$PATH"
export AIS_DATABASE_URL="sqlite:///$PWD/ais.db"
```

Load the example tree. Importing creates and migrates the database too:

```bash
ais import examples/seed.yml
# imported as revision 1
```

Create an admin who can sign in to the UI. You'll be prompted for the password twice:

```bash
ais user create admin@example.com --role admin
```

Then create a separate token-only account for Ansible to read with. A **viewer** can export
and render but not write. A token is printed once. Keep tokens outside the checkout, so they
can never be committed:

```bash
ais user create ansible@example.com --role viewer --no-password
mkdir -p ~/.config/ais && chmod 700 ~/.config/ais
ais token create --user ansible@example.com --name laptop > ~/.config/ais/viewer.token
ais token create --user admin@example.com --name cli --role editor > ~/.config/ais/editor.token
```

Start the server in a second terminal and leave it running. That terminal needs the same two
settings:

```bash
cd ansible-inventory-system
export PATH="$PWD/.venv/bin:$PATH" AIS_DATABASE_URL="sqlite:///$PWD/ais.db"
ais serve            # http://127.0.0.1:8000, API docs at /docs
```

Back in the first terminal:

Point Ansible at it. `examples/` has a ready `inventory.yml` and `ansible.cfg` that uses the
collection straight from this checkout:

```bash
cd examples
export AIS_TOKEN="$(cat ~/.config/ais/viewer.token)"
ansible-inventory --graph
ansible-inventory --host web02
```

`--host web02` shows the merged `packages` and `sysctl` from the table in section 1, plus
`inventory_revision: 1`. That variable is on every host, so every play run records the
revision it ran against.

Ask about a host that doesn't exist yet:

```bash
ais render --path all.federal.prod.dc2.web --facts '{"os_family": "RedHat"}'
```

**The web UI** is a separate build step when you run from a checkout (the container image in
section 3 includes it). You need Node 22 or newer:

```bash
cd web && npm ci && npm run build && cd ..
```

Restart `ais serve`, open `http://127.0.0.1:8000/` and sign in as `admin@example.com`.

## 3. Run it for real (PostgreSQL)

`deploy/compose.yml` runs PostgreSQL 16 and the API. The image builds the web UI in, and the
database schema is created or migrated every time the API starts. On its own, it uses the
development password `ais` and publishes port 8000 on every interface. That's fine on a
laptop and nowhere else.

`deploy/compose.prod.yml` layers the production settings on top:

- it reads the database password from `AIS_DB_PASSWORD`;
- it publishes the API on `127.0.0.1:8000` only, for a TLS proxy on the same host;
- it trusts that proxy's `X-Forwarded-For`;
- it restarts both containers after a reboot.

Set the password from your secret store when you start the stack, never in a file in git.
`POSTGRES_PASSWORD` only takes effect when the database volume is first created, so choose it
before the first `up`.

```bash
export AIS_DB_PASSWORD="$(your-secret-tool get ais-db-password)"
docker compose -f deploy/compose.yml -f deploy/compose.prod.yml up --build -d
docker compose -f deploy/compose.yml -f deploy/compose.prod.yml exec api ais user create admin@example.com --role admin
```

(`ports: !override` needs Docker Compose 2.24 or newer.)

The `ais` CLI inside the container talks to the same database. Every command in section 2
works there with `docker compose … exec api` in front of it. The `user create` prompt needs a
terminal, which `exec` provides.

### TLS

The server speaks plain HTTP. Put a reverse proxy in front of it to terminate TLS. Tokens and
passwords travel in every request, so never expose port 8000 directly. For example, with
[Caddy](https://caddyserver.com/) on the same host:

```
inventory.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

The API takes the client's address from `X-Forwarded-For` only when the request comes from
an address in `FORWARDED_ALLOW_IPS`. Otherwise the failed-login limiter sees every client as
the proxy, and 20 failed logins from anyone lock everyone out for five minutes.

`compose.prod.yml` sets it to `*`. That's safe only because the port is published on loopback,
so the only peer is your proxy. Inside the container, the proxy appears as Docker's bridge
gateway (for example `172.19.0.1`), not `127.0.0.1`. If you publish the port more widely, or
run the proxy on another machine, set `FORWARDED_ALLOW_IPS` to the proxy's address instead.

Then point the plugin at `https://inventory.example.com`. `validate_certs` is on by default;
keep it on.

### First thing to load

Either import your own tree (section 5) or start from the example:

```bash
docker compose -f deploy/compose.yml -f deploy/compose.prod.yml cp examples/seed.yml api:/tmp/seed.yml
docker compose -f deploy/compose.yml -f deploy/compose.prod.yml exec api ais import /tmp/seed.yml
```

## 4. Design your tree

**Rule of thumb: a variable lives at the highest node where it is true for everything
below.** If it differs between two children, push it down to them. If every child sets the
same value, lift it up.

**Order of the top levels.** Business → tier → datacenter is the fixed spine. It reflects the
usual order of "who differs from whom most": a federal estate's compliance settings differ
from commercial in every tier, and prod differs from lab in every datacenter. Put a value at
the level where it really varies:

| Where | Typical variables |
|---|---|
| `all` | NTP, base packages, default DNS, organisation-wide `sysctl` |
| business (`all.federal`) | compliance profile, logging destination, approved repositories |
| tier (`all.federal.prod`) | log level, monitoring thresholds, change windows, backup schedule |
| datacenter (`…prod.dc2`) | DNS servers, site code, NTP override, proxy, subnets |
| group (`…dc2.web`, `…web.edge`) | the role's ports, packages, tuning |
| host | its address (`ansible_host`), and anything truly one-off |

**Names.** Node and tag names follow one rule: letters, digits, `_` and `-` (up to 255
characters). Ansible group names can't hold `-`, so it becomes `_` in the group: the node
`all.commercial.cluster-a` is the group `commercial_cluster_a`, and the tag `cluster-a` is the
group `cluster_a`. Keep names short: the group name joins the whole path. A host name must be
unique across the tree, because Ansible addresses hosts by name.

**Depth and length limits.** A node can be at most 32 levels deep (`all` is 0;
`AIS_MAX_DEPTH` raises it) and its path at most 1,500 bytes. At 30-character names the path
limit is reached around 48 levels, long before any sane design needs it. Both are refused up
front with a 422 that names the limit.

**Hosts belong to one group.** If a machine is both "web" and "monitoring agent", that's a
variable or a tag, not a second parent. Use a **tag** (section 5) for cross-cutting
*targeting* ("all canary hosts"), and a variable for cross-cutting *configuration*.

**Precedence against the rest of Ansible.** Everything here is *inventory* variables, and
Ansible ranks them as such:

- **Group variables** (every node above a host) lose to a playbook's `group_vars/` files.
- **Host variables** (set on the host itself, like `ansible_host`) beat a playbook's
  `group_vars/` files but lose to its `host_vars/` files.
- Both lose to play `vars`, role vars, `set_fact` and `-e`.

Keep those for things that really belong to a play, or the inventory stops being the one
place to look.

## 5. Put data in

There are four ways in. They all go through the same checks (names, kinds, limits, schemas,
locking) and all leave an audit trail.

### YAML import (bulk, and the easiest start)

```yaml
variables:                      # optional: declarations (section 6)
  packages: {merge_strategy: unique_append, description: OS packages; each level adds its own}

all:
  vars: {ntp_server: time.example.com, packages: [vim, curl]}
  children:
    commercial:                 # depth 1 → business
      vars: {compliance_profile: cis-level1}
      children:
        prod:                   # depth 2 → tier
          children:
            dc1:                # depth 3 → datacenter
              vars: {dns_servers: [10.1.0.2, 10.1.0.3]}
              children:
                web:            # depth 4+ → group
                  vars: {http_port: 8080, packages: [nginx]}
                  hosts:
                    web01: {ansible_host: 10.1.1.11}
                    web02: {ansible_host: 10.1.1.12, packages: [tcpdump]}
```

```bash
ais import my-tree.yml --message "initial load"
```

An import is **additive**. It creates what's missing and sets the variables it names. It never
deletes, moves or changes a node's kind. Importing the same file twice is not a change: the
second run says `nothing to change` and writes no revision. To see what an import would do
without writing anything, use `ais import my-tree.yml --dry-run`,
`POST /api/v1/import {"tree": …, "dry_run": true}`, or the `import` module in check mode.

A node may say `kind: group` explicitly, for example a group straight under a business when
that business has no tiers. A child must always be of a deeper kind than its parent.

A file may also list tags (see "Tags" below). Members are node paths, and the import only
adds members, never removes one:

```yaml
tags:
  canary: {description: first to get changes, members: [all.commercial.lab]}
```

`ais export-tree` (or `GET /api/v1/tree`) writes this same format from the database: the
whole tree or one subtree, at the current revision or any earlier one. Importing its output
reproduces what it exported, so it is also how you copy a subtree (below), restore a deleted
one (section 11) and keep a logical backup or a Git copy of the tree (section 12).

### Copy a subtree (clone)

To build a new datacenter like an existing one, export the old one under its new path and
import the result:

```bash
ais export-tree --subtree all.commercial.prod.dc1 --as all.commercial.prod.dc2 --no-hosts > dc2.yml
ais import dc2.yml --dry-run && ais import dc2.yml --message "dc2 from dc1"
```

The API form is `GET /api/v1/tree?subtree=all.commercial.prod.dc1&as=all.commercial.prod.dc2&hosts=false&format=yaml`
(viewer role); import the file with `ais import`, or send the JSON answer as `tree` to
`POST /api/v1/import`.

- `--as` moves every node, value and tag membership of the copy under the new path. Parents
  that don't exist yet are created, with the kind their depth implies (business, tier,
  datacenter, then group). The new parents' own values are not touched.
- `--no-hosts` copies the structure and the group values only. Host names are unique across
  the whole tree, so a copy with its hosts is refused (`duplicate host name`) until you rename
  them in the file. To copy hosts, export with them and rename each one in the YAML.
- A node with an `inventory_name` keeps it in the copy, which then collides with the original
  group name (`group name collision`). Delete or change that line in the file first.
- The copy carries the variable declarations its values use; importing them changes nothing
  when they already match.

A subtree export keeps the chain from `all` down to the subtree, so the file still starts at
`all`, but those parents carry only their kind: importing it never overwrites the values of
the parents in the destination. Add `--ancestor-vars` (`ancestor_vars=true`) for a file that
stands on its own (the parents' own values included), for example to load one datacenter into
an empty test server.

### The web UI

Sign in as an editor or admin. From a node's page you can add a child, move or rename it
(with its subtree), delete a subtree (the dialog says how many nodes and hosts go, and
confirms exactly that many), and edit its variables as JSON or YAML. Saving a value, removing
one, a move and a delete first show how many hosts the change reaches, with the list on
demand, and the confirmation is refused if someone wrote in between ([Preview a
change](#preview-a-change)). The page shows
each inherited value greyed out, with the node it comes from. If someone saved first, the page
says so, reloads, and keeps your text for you to re-apply.

Wherever the UI asks for a host or a path (Render, Diff, the move dialog's new parent,
Search's subtree, the audit log's node and subtree), type part of a name or a path: a list of
up to 20 matching nodes opens, each with its path under the name. Exact names come first, then
names that start with what you typed, paths that start with it, names that contain it, and
last the nodes under a matching group (`dc2` lists `dc2` and then what is under it). Pick with
Down/Up and Enter or with the mouse; Escape closes the list. If more than 20 match, keep
typing. You can also type a name or path in full and press Enter. Links into these pages
(`#/render?host=web01`, `#/render?path=all.commercial.prod`, `#/diff?host=web01&from=3&to=7`)
open straight away. The pickers never download the whole tree: each pause in typing asks
`GET /api/v1/nodes?q=…&limit=21`, a few kilobytes at any size.

### Ansible modules (inventory as code)

The `git_cgallagher.inventory` collection manages the inventory from a playbook. Every module
supports check mode and `--diff`, changes nothing when nothing differs, and needs an **editor**
token. Check mode asks the server for a dry run of each write, so it refuses what the write
would refuse and returns `impact` (the hosts the change reaches; `--diff` prints a summary):

```yaml
- hosts: localhost
  gather_facts: false
  module_defaults:
    group/git_cgallagher.inventory.inventory:
      url: https://inventory.example.com      # or AIS_URL; token from AIS_TOKEN
  tasks:
    - git_cgallagher.inventory.node:
        path: all.commercial.prod.dc1.cache
        vars: {redis_port: 6379}
    - git_cgallagher.inventory.host:
        name: cache01
        parent_path: all.commercial.prod.dc1.cache
        ansible_host: 10.1.3.11
    - git_cgallagher.inventory.variable:
        path: all.commercial.prod
        key: log_level
        value: error
```

The modules are `node`, `host`, `variable`, `variable_definition`, `tag`, `node_move`, `import`,
`revert`, `node_info`, `render_info`, `variable_info` and `tag_info`. Each takes a `change` id that every revision it writes
records ([Undo a change](#undo-a-change)). `ansible-doc git_cgallagher.inventory.<name>` documents each.
Install the collection from the GitHub release (it is not on Ansible Galaxy):

```bash
ansible-galaxy collection install \
  https://github.com/git-cgallagher/ansible-inventory-system/releases/download/v0.3.0/git_cgallagher-inventory-0.3.0.tar.gz
```

### The REST API

Everything the UI and modules do is in the API, documented live at `/docs`. Reads need a
viewer token; writes need an editor token and `If-Match`:

```bash
H="Authorization: Bearer $(cat ~/.config/ais/editor.token)"
REV=$(curl -s -H "$H" 'http://127.0.0.1:8000/api/v1/revisions?limit=1' | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["id"])')
NODE=$(curl -s -H "$H" http://127.0.0.1:8000/api/v1/nodes/all.commercial.prod | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
curl -s -H "$H" -H "If-Match: \"$REV\"" -H 'Content-Type: application/json' \
  -X PUT "http://127.0.0.1:8000/api/v1/nodes/$NODE/vars/log_level" -d '{"value": "error"}'
```

A write answers with the new revision (also its `ETag`). Send that as the next `If-Match`.

To find nodes by name without listing all of them (what the UI's pickers do), add `q`:
`GET /api/v1/nodes?q=web0&kind=host&limit=20` returns at most 20 live hosts whose name or path
contains `web0`, ignoring case, best matches first (exact name, then prefixes, then
substrings). `kind` repeats for several kinds; `subtree` and `revision` work as on the plain
list. Without `q`, `kind` or `limit`, `GET /api/v1/nodes` still returns every node, which at
100,000 hosts is about 21 MB.

### Tags

Tags are named sets of paths for `--limit` and `hosts:` patterns. A member that's a group
brings in every host under it. Manage them with the `tag` module (or the UI's Tags page):

```yaml
- git_cgallagher.inventory.tag:
    name: canary
    description: first to get changes
    paths: [all.commercial.lab, all.commercial.prod.dc1.web.web01]
```

By default `paths` are added and the other members stay; `exclusive: true` makes the members
exactly `paths`, `remove_paths` takes members out, and `state: absent` deletes the tag. The
module reads the current revision for `If-Match` itself and, if someone wrote in between,
reads the tag again and retries once (only when the tag still differs, and never when you set
`expect_revision`). `tag_info` reads one tag or lists them all.

A tag name follows the node name rule (letters, digits, `_`, `-`), and its group turns `-`
into `_`: the tag `gpu-canary` is the group `gpu_canary`. Two names that would make the same
group are refused (422) when the tag is written or imported: `gpu-canary` next to
`gpu_canary`, and a tag whose group is a hierarchy group's name (`commercial-prod` while
`all.commercial.prod` exists). `all` and names whose group starts with `business_`, `tier_` or
`datacenter_` are reserved for the automatic tag groups.

The API underneath is `PUT /api/v1/tags/{name}` with the complete member set (declarative)
and `DELETE /api/v1/tags/{name}`, both with `If-Match`.

`ansible-playbook site.yml --limit canary` then runs on every lab host plus `web01`.

## 6. Declare variables

A variable doesn't need declaring to be used. Declare one when it should **merge or append**
(section 1), when you want a **description** shown in the UI, or when you want its values
**checked against a JSON Schema**:

```yaml
- git_cgallagher.inventory.variable_definition:
    name: http_port
    merge_strategy: replace
    description: Port the web role listens on.
    json_schema: {type: integer, minimum: 1, maximum: 65535}
```

The schema is checked on every write of that key, from every route: API, UI, import and
modules. A new schema is refused while any stored value already breaks it, so the tree never
holds a value its own schema rejects. Each node's own contribution is checked, not the merged
result. So for a `merge` dictionary, don't mark keys `required`: a deeper node normally sets
only some of them.

Change a strategy with care: it changes the value every host below sees. Render a few hosts
first (next section).

### Typos: "did you mean …?"

Because declaring is optional, a misspelled key is a valid new key: `log_levle` would be stored,
nothing would read it, and the real `log_level` would stay as it was. So every write that stores
values checks the undeclared keys it wrote against the keys the inventory already knows, and
when one is close it still writes it but says so:

```json
{"revision": 42, "changed": true, "key": "log_levle", "value": "debug",
 "warnings": [{"key": "log_levle", "path": "all.commercial.staging", "did_you_mean": "log_level",
               "declared": true, "message": "log_levle at all.commercial.staging is not declared; did you mean log_level (declared)?"}],
 "warnings_total": 1}
```

- **Where you see it.** In that `warnings` list on `PUT …/vars/{key}`, `POST /nodes` (its
  `vars`), `POST /import` (dry runs too), `POST /nodes/restore` and the revert routes; as an
  Ansible warning plus `key_warnings` in the result of the `variable`, `node`, `host`, `import`
  and `revert` modules; on stderr from `ais import`, `ais restore` and `ais revert`; and in the
  UI's notice after you save a variable. An import lists each key and path once, every key
  before any key's second path, at most 50, and `warnings_total` counts them all.
- **What counts as close.** Both keys are compared in lower case with `-` read as `_`. A key is
  close to a **declared** key within 2 edits (insert, delete, change or swap two neighbouring
  characters), and to an **undeclared key that at least `AIS_TYPO_MIN_NODES` nodes set** (default
  5, `0` turns this off) within 1 edit, and only when that key is set on more nodes than the
  new one. Short keys get less slack: 1 edit up to 6 characters, none (case and `-`/`_` only) up
  to 3. That is why `ansible_port` is not reported as a typo of `ansible_host` (2 edits, and
  undeclared), while `gpu_drvier` next to a `gpu_driver` that 20 nodes set is.
- **Silence it by declaring the key** (or fix the spelling and delete the typo). Declared keys
  never warn.
- **Clean up what is already there** with `ais lint` (exit status 1 when it finds something, so a
  pipeline can gate on it) or `GET /api/v1/vars/lint`: every live undeclared key that looks like
  a typo, with its suggestion, how many nodes set it and the first paths. Then `ais vars <key>`
  shows where it is set.

### Strict mode

Set `AIS_STRICT_KEYS=true` on the server to refuse **new** undeclared keys: a write whose key is
not declared and is set on no node yet gets a 422 that names the key, the likely intended one,
and how to declare it:

```json
{"detail": {"error": "undeclared key",
            "message": "strict mode (AIS_STRICT_KEYS) refuses keys that are not declared and not set anywhere yet: log_levle (did you mean log_level?). To add a new key, declare it first: PUT /api/v1/variables/log_levle, ... Nothing was written",
            "keys": [{"key": "log_levle", "paths": ["all.commercial.staging"], "nodes": 1, "did_you_mean": "log_level"}]}}
```

Keys already in the tree keep working: reads, renders, deletes, and new values for them, on any
node. So you can turn strict mode on over an existing inventory, then tidy up with `ais lint`.
Restores and reverts are never refused (they put back what was there before, and every change
must stay undoable); they still warn. Strict mode is one server-wide setting, not per business
or subtree: a per-subtree flag would be one more thing to store, revert and explain, for a
policy that is really about how the whole server is run.

## 7. Check a change before it lands

Render a real host, a mock host, a past revision, or "what if" overrides, without writing
anything:

```bash
# a real host now
ais render --host web02
# a host that doesn't exist yet, with facts the templates need
ais render --path all.federal.prod.dc2.web --facts '{"default_ipv4": {"address": "10.2.1.99"}}'
# the API form, with what-if overrides
curl -s -H "Authorization: Bearer $(cat ~/.config/ais/viewer.token)" -H 'Content-Type: application/json' \
  -X POST http://127.0.0.1:8000/api/v1/render \
  -d '{"path": "all.federal.prod.dc2.web", "mock_hostname": "fed-web99", "overrides": {"http_port": 8443}}'
```

The answer has:

- `chain`: the groups, top to bottom.
- `resolved`: the merged values, before Jinja.
- `provenance`: for each key, which node set it. For merge/append keys, every node that
  contributed.
- `rendered`: the values after Jinja, with mock or supplied facts and `inventory_hostname`,
  `group_names` and friends.
- `errors`: each key that failed to evaluate, and why.

**What the render will not do.** It runs on the server, for anyone with a viewer token, so it
never touches the server itself. It evaluates expressions and ordinary filters. It doesn't run
lookups (`lookup`, `query`). It refuses filters and tests that read files or the environment
(`expandvars`, `fileglob`, `is file`, …), as well as collection filters. Such a key shows up
in `errors` with "does not run in the server render". The value is still fine: a playbook
evaluates it normally on the controller. Facts you pass are data, never templates, as in
Ansible.

### Preview a change

The render answers "what would this host get?". The other question before a write is "which
hosts does this change reach?": a value set on `all.commercial` reaches every commercial host
that doesn't set it lower down. Every write route takes **`dry_run`**: the server runs the
write in a transaction with every check the real write runs (the schema, strict mode, the
delete guard, name collisions), measures what it would do to the hosts, and rolls it back.
Nothing is written, no revision is used up, no audit row is left.

```bash
TOKEN=$(cat ~/.config/ais/editor.token)
id=$(curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/api/v1/nodes/all.commercial | jq .id)
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -X PUT "http://127.0.0.1:8000/api/v1/nodes/$id/vars/compliance_profile?dry_run=true" \
  -d '{"value": "cis-level2"}' | jq .impact
```

```json
{
  "total": 4, "vars_changed": 4, "added": 0, "removed": 0, "regrouped": 0, "moved": 0,
  "hosts": [
    {"host": "lab-web01", "path": "all.commercial.lab.dc1.web.lab-web01", "status": "changed",
     "added": {}, "removed": {}, "changed": {"compliance_profile": {"from": "cis-level1", "to": "cis-level2"}}},
    "…"
  ],
  "groups": [], "groups_total": 0, "limit": 200, "truncated": false
}
```

- `dry_run=true` is a query parameter on `POST /nodes`, `PATCH /nodes/{id}`, `POST
  /nodes/{id}/move`, `DELETE /nodes/{id}`, `PUT`/`DELETE /nodes/{id}/vars/{key}`,
  `PUT`/`DELETE /variables/{name}` and `PUT`/`DELETE /tags/{name}`; `POST /import`, `POST
  /nodes/restore` and the two revert routes take `"dry_run": true` in the body.
- The answer is the body the real write would give (ids a create or move would allocate are
  `null`), with `dry_run: true`, `revision` = the revision it is based on, and **`impact`**.
  A refusal is the same status and message the write would give.
- `impact` has the same shape for every write. `total` is the number of distinct hosts
  affected; `vars_changed`, `added`, `removed` (a create, delete or restore), `regrouped` (a
  move, a rename, an `inventory_name` or a tag) and `moved` count them by kind. `hosts` lists
  them by path: the keys that appear (`added`), disappear (`removed`) or change (`changed`,
  `from`/`to`), the groups the host `joined` and `left`, and `moved_from`. `groups` counts the
  hosts that join or leave each group. The lists stop at `limit` (200) with `truncated: true`;
  the counts are always exact.
- A dry run needs no `If-Match`. If you send one it must be current (409 otherwise), which is
  how the UI makes sure the preview matches the page. Then send the real write with
  `If-Match` = the dry run's `revision`: if anything changed in between, it refuses with 409
  and you preview again.
- A schema-only or description-only change to a declaration changes no host (`total: 0`); a
  strategy change reaches every host whose value comes from more than one node.

The web UI does this for you: **Save** on a variable, **Remove**, **Move / rename** and
**Delete** on a node, and **Save** / **Undeclare** on the Variables page show "This changes N
hosts" (expand it for the list) before the confirm button writes. The modules use the dry run
in check mode, and `--check --diff` prints the summary:

```text
TASK [Lower the log level for commercial prod] ***
impact: 3 host(s) affected, 3 vars changed
  db01: changed (log_level: "warning" -> "error")
  web01: changed (log_level: "warning" -> "error")
  web02: changed (log_level: "warning" -> "error")
```

`ais import --dry-run`, `ais restore --dry-run` and `ais revert --dry-run` print the same
counts and the first hosts.

How much does it cost? The preview resolves only the hosts and keys the write can change (a
value at one node: the hosts under it, that key; a strategy change: the hosts under the nodes
that set it), so at 100,000 hosts it adds 0.06 s for a datacenter and about 1 s for a value at
`all` that reaches every host. The rest of a dry run costs what the write costs, mostly the
export check every structural or value write runs (about 3 s at that size). See
[docs/scale.md](scale.md).

## 8. Use it from playbooks

An inventory source is a YAML file whose name ends in `inventory.yml`, `inventory.yaml`,
`ais.yml` or `ais.yaml`. The plugin ignores any other name, and Ansible's only hint is a
generic "could not be verified by inventory plugin" warning:

```yaml
# inventory/ais.yml
plugin: git_cgallagher.inventory.db
url: https://inventory.example.com
# subtree: all.federal.prod      # only this part of the tree
# revision: 1234                 # pin to an exact revision (reproducible reruns)
```

```ini
# ansible.cfg
[defaults]
inventory = inventory/ais.yml

[inventory]
any_unparsed_is_failed = True
```

**Keep `any_unparsed_is_failed`.** By default, an inventory source that fails to load is only
a warning in Ansible. When the server is down, the token is wrong or a pinned `revision:`
doesn't exist, a playbook prints `skipping: no hosts matched` and **exits 0**, so a deploy job
goes green having touched nothing. With the setting, the same run stops with
`Completely failed to parse inventory source` and exits 1. It must be in the `[inventory]`
section: under `[defaults]` (as `any_unparsed_is_failed` or `inventory_any_unparsed_is_failed`)
Ansible ignores it without a word, and the run exits 0 again. The environment variable is
`ANSIBLE_INVENTORY_ANY_UNPARSED_IS_FAILED=True`. `ansible-config dump --only-changed` shows
whether it took effect (`INVENTORY_ANY_UNPARSED_IS_FAILED`).

Give it a **viewer** token through `AIS_TOKEN` (or the `token` option, but keep it out of
git). Then use the qualified groups and tag groups as usual:

```bash
export AIS_TOKEN="$(your-secret-tool get ais-ci-token)"
ansible-playbook site.yml --limit commercial_prod_dc1_web
ansible-playbook site.yml --limit 'tier_prod:&datacenter_dc1'
ansible-playbook patch.yml --limit canary
```

Points worth knowing:

- **Every host gets `inventory_revision`.** Log it, or put it in a change record. To rerun
  exactly what ran before, pin `revision:`.
- **`subtree`** exports only part of the tree. That's much faster on a big estate, and handy
  for giving a team only its own business.
- **Revalidation.** The plugin keeps the last export in `~/.cache/ais` (`etag_store`) and asks
  the server whether it changed. An unchanged inventory costs one tiny request. The store
  only revalidates: when the server can't be reached the inventory fails to load, it never
  falls back to the stored copy.
- **Leave Ansible's inventory cache (`cache: true`) off in deploy pipelines.** It is a
  different thing from the ETag store. Its key is the inventory file's path and nothing else,
  so while the cache is younger than `cache_timeout` (default 3600 seconds) the plugin doesn't
  ask the server at all. A change made in the meantime, a new `revision:` or `subtree:` in the
  file and a server that is down all go unnoticed: the play runs on the cached hosts, and
  `any_unparsed_is_failed` can't help, because the inventory did load. (Tested: API stopped,
  every cached host served, the playbook ran and exited 0.) What it saves is the one small
  revalidation request per run, which the ETag store already makes cheap. If you do turn it on
  for interactive use, keep `cache_timeout` short and use `--flush-cache` when it matters. The
  plugin prints a warning ("comes from Ansible's inventory cache") on every run it answers
  from that cache.
- **Templates are evaluated on the controller.** A stored `"{{ ansible_host }}:{{ http_port }}"`
  reaches a play as `10.1.1.11:8080`, exactly as the render showed. This includes lookups,
  which is how secrets work (next section). It also means that anyone who can write the
  inventory can run code on every controller that uses it, the same as anyone who can push to
  a YAML inventory in git. Hand out the editor role accordingly.
- Use **one** inventory server per inventory. Mixing another source in is allowed by Ansible,
  but its groups and variables then interleave with this one's precedence.

## 9. Secrets

**Don't store secret values in the inventory.** Every viewer can read every value: export and
render return them, and history keeps them forever. There is no Ansible Vault support:
vault-encrypted values do not survive the JSON export.

Instead, store a **lookup** that fetches the secret on the controller when the play runs:

```yaml
all:
  children:
    commercial:
      vars:
        db_password: "{{ lookup('community.hashi_vault.hashi_vault', 'secret/data/commercial/db:password') }}"
        # or AWS SSM, across accounts, with git_cgallagher.aws:
        # db_password: "{{ lookup('git_cgallagher.aws.ssm_parameter', '/commercial/db/password', account='123456789012', role_name='ParamReader', region='us-east-1') }}"
```

The inventory then holds only *where* the secret lives. A playbook resolves it on the
controller, with the controller's credentials. The render shows these keys under `errors`
("lookup() does not run in the server render"), which is expected. Put the lookup at the
level where the secret differs, like any other variable.

## 10. Users, roles and tokens

| Role | Can |
|---|---|
| `viewer` | export, render, read nodes, variables, revisions, snapshots, diffs, tags |
| `editor` | everything a viewer can, plus every write, and read the audit log |
| `admin` | everything an editor can, plus manage users |

Roles are global: there's no "editor of `all.federal` only" yet.

- **People** sign in to the UI with e-mail and password: `ais user create … --role editor`,
  or `POST /api/v1/users` as an admin. Disable someone with
  `PATCH /api/v1/users/{id} {"is_active": false}`; their tokens stop working too. A UI session lasts 12 hours
  (`AIS_SESSION_TTL_HOURS`).
- **Machines** use tokens. Create a token-only user (`--no-password`) per consumer, such as CI
  or AWX. Mint its token with a capped role and an expiry, and store it in that consumer's
  secret store:

  ```bash
  ais token create --user ci@example.com --name awx --role viewer --expires-days 90
  ais token list
  ais token revoke 7
  ```

  People can mint their own tokens on the UI's Tokens page. A token's secret is shown once
  and stored only as a hash.
- **Single sign-on (OIDC)** is implemented but has **never been tested against a real
  provider**. Treat it as unavailable until it has been. The settings are in section 12.
- Failed logins are limited per e-mail (5) and per client address (20) in a 5-minute window,
  with a 429 and `Retry-After` (`AIS_LOGIN_MAX_FAILURES`, `AIS_LOGIN_MAX_FAILURES_PER_IP`,
  `AIS_LOGIN_WINDOW_SECONDS`).

## 11. History, diffs and the audit log

- **Revisions:** `GET /api/v1/revisions` lists every revision with its actor, source (`api`,
  `ui`, `module`, `cli`), message and change id (`?change=SRE-1234` for one change's). In the UI,
  see the Revisions page.
- **Any revision, exactly:** `ais export --revision 12`, `ais render --host web01 --revision
  12`, or the plugin's `revision:` option.
- **A host's own history:** `GET /api/v1/hosts/web01/snapshots` lists the revisions where
  `web01`'s variables actually changed. `GET /api/v1/diff?host=web01&from=12&to=40` says what
  changed between two revisions. The UI's Diff page does both.
- **Who did what:** `GET /api/v1/audit` (editor role) has one row per change and per sign-in,
  token or user event, with before and after values. Filter by actor, action, node (following
  it across moves and renames), a variable's key on every node, subtree, revision or time. In the
  UI, a node's or variable's "history" link opens it. To find where a key is set and which hosts
  have which value, see [Investigate a variable](#investigate-a-variable) below.
- **Undo** is a new change, not a rewind: `revert` writes the inverse as a new revision
  ([Undo a change](#undo-a-change) below). History is never rewritten.
- **Deleting a group asks first.** `DELETE /api/v1/nodes/{id}` on a node with anything below
  it answers 409 with the counts ("has 44 node(s) below it; deleting it deletes 45 node(s) in
  all, 21 of them host(s)") unless the call confirms: `?recursive=true`, or `?expect_count=45`,
  which must equal the number of nodes deleted, the node included (a wrong count refuses even
  with `recursive`). A host or an empty group deletes without either. `?dry_run=true` lists
  what would go and its `impact` on the hosts, and writes nothing (no `If-Match` needed; see
  [Preview a change](#preview-a-change)). The module wants `recursive: true`; the UI's Delete
  shows the node and host counts and sends them as the confirmation.
- **Restore a deleted subtree** with one call. It comes back as it was at the last revision
  before its latest delete (or at the `revision` you give): every node, its own values, node
  `meta` and its tag memberships, under one new revision:

  ```bash
  curl -s -X POST -H "$H" -H 'Content-Type: application/json' -H 'If-Match: "57"' \
    -d '{"path": "all.commercial.prod.dc1"}' http://127.0.0.1:8000/api/v1/nodes/restore
  # {"revision": 58, "from_revision": 56, "nodes": 45, "hosts": 21, "tags": [...], "restored": [...]}
  ais restore all.commercial.prod.dc1 --dry-run     # the same from the server's shell
  ais restore all.commercial.prod.dc1 --revision 40 --message "dc1 as it was before the migration"
  ```

  `"dry_run": true` (or `--dry-run`) reports the counts and the `impact` (the hosts that
  come back) and writes nothing. Each host then
  resolves as it did at that revision, as long as nothing above the subtree changed since:
  the restore never rolls the parents back. It refuses, writing nothing, when something lives
  at the path now (it never overwrites: delete or move that first, or copy the old version
  elsewhere with `GET /api/v1/tree?…&as=`), when the parent is gone too (restore the highest
  deleted ancestor, which brings this subtree back with it), when one of its host names is in
  use elsewhere (host names are unique), and when a definition tightened since rejects an old
  value.

  **History continues.** The restored nodes are new rows with new ids (a node row is live over
  one span of revisions, so the deleted rows cannot be revived without making the revisions
  in between show them). The `node.restore` audit row maps every old id to its new one, the
  way a move's row does, so `GET /api/v1/audit?node=<path>` (and `&key=`) and the UI's history
  links show the changes from before the delete too, and the root's history shows the delete
  and the restore. Host snapshots are kept by host name: between the delete and the restore
  the host does not exist (`/hosts/{name}/snapshots/{rev}` and `/diff` answer 404 for those
  revisions), and `/diff` from before the delete to after the restore is `identical`; the
  background pass stores no new row for a host that came back unchanged. Tag memberships come
  back unless the tag itself was deleted since (`DELETE /tags/{name}` removes every
  membership; a `PUT /tags/{name}` only replaces the live members, so the deleted nodes' stay).
  The two-step path still works too, for a restore into another server or an edited copy:
  `ais export-tree --revision 56 --subtree … > dc1.yml`, then `ais import dc1.yml`; but an
  import does not link the old ids, so the audit trail of the old nodes stays with them.

### Investigate a variable

Three questions come up in every incident: where is `gpu_driver` set, which hosts end up with
which value, and who changed it. Each is one read. On the seed tree:

```bash
ais vars http_port                       # where is it set? (the nodes that set it themselves)
# http_port (replace, undeclared) at revision 1: set on 3 node(s)
# all.commercial.lab.dc1.web    8081
# all.commercial.prod.dc1.web   8080
# all.federal.prod.dc2.web      443
ais vars http_port --hosts --value 443   # which hosts get 443?
# http_port (replace, undeclared) at revision 1: 2 host(s) match, 5 have it, 1 do not
# values: 8080 x2, 443 x2, 8081 x1
# fed-edge01    443     from all.federal.prod.dc2.web
# fed-web01     443     from all.federal.prod.dc2.web
ais vars packages --hosts --subtree all.federal
# fed-edge01    ["vim", "curl", "aide", "haproxy"]      folded from all + all.federal + all.federal.prod.dc2.web.edge
```

The same through the API (viewer role), the `variable_info` module and the UI's **Search** page
(also reached from the "where set" link on every variable of the Variables page and of a node
page, and from the find box above the tree, which searches under the selected node):

```bash
curl -s -H "$H" 'http://127.0.0.1:8000/api/v1/vars?key=http_port'
curl -s -H "$H" -G 'http://127.0.0.1:8000/api/v1/vars/hosts' --data-urlencode 'key=gpu_driver' \
  --data-urlencode 'value="550.90"' --data-urlencode 'subtree=all.commercial.prod'
curl -s -H "$H" 'http://127.0.0.1:8000/api/v1/audit?key=gpu_driver'     # editor: who changed it, anywhere
```

```yaml
- git_cgallagher.inventory.variable_info:
    key: gpu_driver
    value: "550.90"
    subtree: all.commercial.prod
    hosts: true
  register: old_driver   # .nodes, .hosts (host, path, value, from), .value_counts, .with_key, .without_key
```

How to read the answers:

- **Where set** lists only the nodes that set the key **themselves**, sorted by path, with their
  own value. For a `merge`/`append`/`unique_append` key each node also shows `effective`: the
  cumulative fold it emits (what its hosts get unless a deeper node adds to it). With `subtree`,
  the ancestors of the subtree that set the key are listed too (`above_subtree`), because the
  subtree's hosts inherit from them.
- **Hosts** gives every host's **resolved** value, exactly what `ais render` and a playbook see,
  with `from`, the deepest node that sets it (for a fold, `sources`: every contributing node,
  root first). Hosts without the key are counted (`without_key`), not listed. `values` is the
  distribution over the whole scope ("20 hosts on 560.35, 10 on 550.90"), the 50 most common.
- **`value` is JSON, and the type counts.** `550.90` is the number, `"550.90"` the string, so
  quote a string that looks like a number (on the shell: `--value '"550.90"'`); text that is not
  valid JSON (`nvidia-550`) is taken as a plain string. Objects match whatever their key order.
  The UI's value box takes YAML (JSON is YAML) and sends JSON. A node matches on its **own**
  value; a host on its **whole resolved value**, which for a fold key is the folded result
  (`["vim", "curl", "aide"]`), not one node's contribution.
- **Revision and paging.** `revision` searches the tree as it was; the default is the current
  one. Both lists page by path (`limit` up to 1,000, `offset`); the counts always cover the
  whole scope. The host values are computed at that revision, not read from the stored host
  snapshots, so they are never behind a write. Every host's value takes about 1.5 s at 100,000
  hosts; pass `subtree` to make it a few hundredths of a second ([scale](scale.md)).
- **Audit by key** (`GET /api/v1/audit?key=`, the Audit page's key filter) returns that key's
  `variable.set`/`variable.unset` rows on every node, the rows its reverts wrote, and its
  definition's rows, newest first, across moves (the key is stored on the row). Writes that
  change many keys in one row are **not** found by key: an `import`, a `node.create` with
  variables, a delete or a restore of a subtree. Find those with `node=`, `action=` or
  `revision=`. `subtree=` keeps the rows whose recorded path (at the time of the write) is at or
  below it.

### Undo a change

**Name your changes.** Send a change id with every write of one change: a ticket or change
number such as `SRE-1234`. The API takes it as the `X-AIS-Change` header, every module as the
`change` option (set it once for a play with `module_defaults`, or export `AIS_CHANGE`), and `ais
import`/`ais restore`/`ais revert` as `--change`. The revision and its audit rows record it:

```bash
curl -s -H "$H" 'http://127.0.0.1:8000/api/v1/revisions?change=SRE-1234'   # the change's revisions
curl -s -H "$H" 'http://127.0.0.1:8000/api/v1/audit?change=SRE-1234'       # and what each did
```

The UI's Revisions and Audit pages show the id and filter by it (`#/revisions?change=SRE-1234`).
An id is 1 to 64 characters from letters, digits and `._:+@-`, starting with a letter or digit
(`SRE-1234`, `CHG0012345`, `deploy:2026-11-05.3`). Anything else is refused with a 400 that does
not repeat the value, and so is anything starting `ais_`: a token pasted into the wrong field
must not end up in the revision list. It names the change; it is not a place for free text (the
revision's `message` is).

**Revert.** One revision, or every revision of a change id, comes undone as one new revision:

```bash
# what would it do? (no If-Match needed; writes nothing)
curl -s -X POST -H "$H" -H 'Content-Type: application/json' -d '{"dry_run": true}' \
  http://127.0.0.1:8000/api/v1/changes/SRE-1234/revert
# {"revision": 61, "dry_run": true, "reverted": [57, 58, 60], "operations": [...],
#  "impact": {"total": 21, "vars_changed": 20, "removed": 1, ..., "hosts": [{"host": "web01", "status": "changed", ...}]}}
#  (hosts_changed and hosts, older names for impact.total and impact.hosts, are still there)
curl -s -X POST -H "$H" -H 'If-Match: "61"' -H 'X-AIS-Change: SRE-1234-rollback' \
  -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:8000/api/v1/changes/SRE-1234/revert
curl -s -X POST -H "$H" -H 'If-Match: "62"' -H 'Content-Type: application/json' -d '{}' \
  http://127.0.0.1:8000/api/v1/revisions/59/revert                          # one revision
ais revert --of-change SRE-1234 --dry-run      # the same from the server's shell
ais revert --revision 59 --message "back out the DNS change"
```

From a playbook, the `revert` module (`revert_change: SRE-1234` or `revision: 59`) does the
same; its check mode is the dry run, and a change that is already reverted is not a change.
In the UI, each revision has a Revert button, and a change filter adds "Revert change …"; both
show the dry run first and send the revision it was computed at as `If-Match`.

The server applies the inverse of every audited write, newest first, and records each as an
ordinary audit row (`variable.set`, `node.move`, `node.restore`, …) plus one `revert` row, so
the history of a node or key shows the revert, and **a revert can itself be reverted** (that
redoes the change). What each write turns into:

| The write | Its revert |
|---|---|
| set a variable | sets the old value back, or unsets a key that was new |
| unset a variable | sets the value again |
| create a node | deletes it (it must still have nothing below it) |
| patch a node (`inventory_name`, `meta`) | puts the old values back |
| move or rename | moves it back, with its values, `meta` and tags (node ids change, history is linked) |
| delete a subtree | restores it as it was at the revision before the delete (as `POST /nodes/restore` does) |
| restore | deletes the restored subtree (it must hold nothing else) |
| declare / change / undeclare a variable | declares it as it was, or undeclares it |
| set / delete a tag | sets it back to its description and members, or deletes it |
| import | deletes the nodes it created, puts back the values it replaced, unsets the ones it added, and puts its definitions and tags back as they were |

Every kind reverts exactly: afterwards the tree export and every host's resolved variables are
what they were before the write. Two limits, both about history from before this feature: an
import recorded by an older server that changed definitions or tags cannot be reverted (its
audit row does not say what they were; the revert says so and writes nothing), and a tag
deleted by an older server comes back with its live members but without the memberships of
nodes that were deleted at the time (they only matter to a later restore of those nodes).

**Conflicts.** If a later revision that is not being reverted changed the same node (or
anything above or below it, for a create, move, delete or restore), the same key on the same
node, the same definition or the same tag, the revert is refused with 409 and the list
(`conflicts`: which revision, what it did, who, and what overlapped). Revert those first,
newest first, or send `"force": true` (`--force`, `force: true`) to revert over them, which
overwrites what they did to those things. A change's own revisions never conflict with each
other. `force` never makes the revert guess: an inverse that cannot apply now (the node moved
away, the path is taken, a JSON Schema tightened since rejects the old value) is refused either
way, with what blocked it. Reverting something that was already reverted answers 409
`already reverted` with the revision that did it.

Per-host history is kept by a background task in the API process. It stores a host's state
only in the revisions where it changed. A revision it skipped is computed on demand, so every
revision still answers exactly. `ais snapshots status` shows how far it has got.

## 12. Operating it

### Settings

| Variable | Default | What |
|---|---|---|
| `AIS_DATABASE_URL` | — (required) | `postgresql+psycopg://user:pass@host:5432/db`, or `sqlite:///path.db` for trials |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1,::1` (`*` in `compose.prod.yml`) | peers whose `X-Forwarded-For` is trusted (uvicorn) |
| `AIS_MAX_DEPTH` | `32` | deepest allowed node (minimum 4) |
| `AIS_STRICT_KEYS` | off | `true` refuses keys that are neither declared nor set anywhere yet (section 6) |
| `AIS_TYPO_MIN_NODES` | `5` | an undeclared key set on this many nodes counts as known for "did you mean" warnings; `0` = only declared keys |
| `AIS_SESSION_TTL_HOURS` | `12` | lifetime of a UI sign-in |
| `AIS_LOGIN_MAX_FAILURES` / `_PER_IP` / `AIS_LOGIN_WINDOW_SECONDS` | `5` / `20` / `300` | failed-login limiter; `0` turns a limit off |
| `AIS_SNAPSHOTS` | on | `off` stops the per-host history task (history is then computed on demand) |
| `AIS_SNAPSHOT_POLL_SECONDS` | `10` | how often that task checks for missed writes |
| `AIS_WEB_DIR` | built-in | serve the UI from another directory |
| `AIS_OIDC_ISSUER`, `AIS_OIDC_CLIENT_ID`, `AIS_OIDC_CLIENT_SECRET`, `AIS_SECRET_KEY` | unset | OIDC sign-in (untested; all four enable it); also `AIS_OIDC_AUTO_PROVISION=1`, `AIS_OIDC_DEFAULT_ROLE`, `AIS_OIDC_REDIRECT_URL`, `AIS_COOKIE_SECURE` |

On the Ansible side: `AIS_URL`, `AIS_TOKEN` and `AIS_ETAG_STORE` feed the plugin and modules.

### Backups

There's no built-in backup. The whole state, history included, is the PostgreSQL database, so
back that up:

```bash
docker compose -f deploy/compose.yml -f deploy/compose.prod.yml exec -T db \
  pg_dump -U ais -Fc ais > "ais-$(date +%F).dump"
```

Restore into a stopped API, then start it again:

```bash
docker compose -f deploy/compose.yml -f deploy/compose.prod.yml stop api
docker compose -f deploy/compose.yml -f deploy/compose.prod.yml exec -T db \
  pg_restore -U ais -d ais --clean --if-exists < ais-2026-11-05.dump
docker compose -f deploy/compose.yml -f deploy/compose.prod.yml start api
```

A restore returns everything to the moment of the dump, users and tokens included. A token
minted after the dump stops working. Test a restore before you need one. A dump holds password
and token hashes, so store it as carefully as the database.

#### Tree exports (logical backups, GitOps)

`ais export-tree > tree.yml` (or `GET /api/v1/tree?format=yaml` with a viewer token) writes
the whole tree in the import format: every node with its kind and own values, every variable
declaration, and the tags. Loading it into an empty server with `ais import tree.yml` gives the
same inventory: every host resolves to the same values. The output is stable (keys and nodes in
a fixed order), so it diffs cleanly in Git, and a review of that diff is a review of the tree.
The revision it was taken at goes to stderr (`X-AIS-Revision` over HTTP), not into the file,
so an unchanged tree exports to the same bytes.

It complements `pg_dump`, it doesn't replace it. A tree export has no history (revisions,
snapshots, audit log), no users or tokens, and no node `meta`. Variable declarations and tags
are not versioned, so an export at an old revision carries today's declarations and today's
tag names. Importing is additive: an import never deletes a node or a value that the file
doesn't have.

At 100,000 hosts the whole-tree export takes about 3 s as JSON and 5 s as YAML (17 MB);
one datacenter about 0.1 s (`docs/scale.md`).

### Upgrades

Read `CHANGELOG.md`, back up, check out the new tag, and rebuild. Schema migrations run
automatically when the API starts:

```bash
git fetch --tags && git checkout v0.3.0
docker compose -f deploy/compose.yml -f deploy/compose.prod.yml up --build -d
curl -s https://inventory.example.com/healthz     # {"status":"ok","version":"0.3.0",…}
```

Upgrade the collection on your controllers to the same version (section 5).

The migrations create two PostgreSQL extensions: `ltree` (subtree queries) and, from the
release after 0.3.0, `pg_trgm` (the indexes behind the UI's node pickers). Both are trusted
extensions, so the database owner the API connects as may create them. On a managed
PostgreSQL that allow-lists extensions, allow both before upgrading.

### Health and size

- `GET /healthz` needs no token and returns `{"status": "ok", "version": …}`. Use it for
  monitoring.
- Run **one** API process. The login limiter and the history task live in that process, and
  `ais serve` starts exactly one.
- Measured at 100,000 hosts on PostgreSQL (`docs/scale.md`): a full export takes about 3.5
  seconds, a subtree export about 0.06 s, a render about 0.02 s, and each write about 3.3 s,
  because every write re-checks the whole tree.

## 13. Limits and known gaps

As of 0.3.0:

- **OIDC is untested** against a real provider. Use local accounts and tokens.
- **Roles are global.** No per-subtree permissions.
- **No secret storage.** Store lookups instead (section 9).
- **No published container image.** Build from a tagged checkout.
- **No `tag` module.** Use the API or UI.
- **Tree exports leave out node `meta`.** The import format has no place for it.
- **No user-management page.** Use the CLI or `/api/v1/users`.
- **Writes slow down with size.** About 3.3 s each at 100,000 hosts.
- **Render cost isn't capped.** A deliberately huge template (`{{ 'a' * 10**9 }}`) from any
  viewer can use that much memory in the API process. Only give tokens to people and systems
  you trust not to do that.
- **Inventory precedence is Ansible's.** Playbook `group_vars`, play vars and `-e` still
  override it (section 4).

## 14. Troubleshooting

| You see | Means | Do |
|---|---|---|
| a playbook prints `Could not match supplied host pattern` and `skipping: no hosts matched`, and exits 0 | the inventory didn't load, and Ansible treats that as a warning. Look above for `Failed to parse inventory`: `Connection refused` (server down), `404` with `revision=` in the URL (a pinned revision that doesn't exist), `rejected the token`, or `could not be verified by inventory plugin` (next row) | fix the cause, and set `any_unparsed_is_failed = True` under `[inventory]` so it fails the run next time (section 8) |
| `inventory source '…' could not be verified by inventory plugin 'git_cgallagher.inventory.db'` | the file's name doesn't end in `ais.yml`, `ais.yaml`, `inventory.yml` or `inventory.yaml` | rename it (e.g. `prod.ais.yml`) |
| `Completely failed to parse inventory source` | the inventory didn't load and `any_unparsed_is_failed` is on, as it should be | the warnings above it say why (the two rows above) |
| `[WARNING]: ais: inventory revision N comes from Ansible's inventory cache` | `cache: true` is set; the server wasn't asked, so the hosts can be up to `cache_timeout` old | `--flush-cache`, or remove `cache: true` (section 8) |
| `401` / plugin says "rejected the token" | missing, wrong, expired or revoked token | check `AIS_TOKEN`; `ais token list` |
| `403` | the token's role is too low | writes need editor; the audit log needs editor; users need admin |
| `428 Precondition Required` | a write without `If-Match` | send `If-Match: "<revision>"` |
| `409` on a write | someone wrote since your revision | re-read, re-apply, retry with the new revision |
| `422` naming depth or bytes | a node would exceed `AIS_MAX_DEPTH` or the 1,500-byte path limit | shorten names or flatten |
| `422 … does not match its json_schema` | the value breaks the variable's schema | fix the value, or the schema |
| `429` with `Retry-After` | too many failed logins | wait; check `FORWARDED_ALLOW_IPS` if it hits everyone |
| a render error "does not run in the server render" | the value uses a lookup or a file/env filter | expected; a playbook evaluates it (section 7) |
| a playbook prints `{{ … }}` literally | collection older than 0.3.0 with ansible-core 2.19+ | upgrade the collection |
| `the web UI is not built` at `/` | running from a checkout without building `web/` | `cd web && npm ci && npm run build` |
| export error about a group name collision | two paths produce the same `_`-joined name (e.g. `a.b_c` and `a_b.c`) | rename one node |

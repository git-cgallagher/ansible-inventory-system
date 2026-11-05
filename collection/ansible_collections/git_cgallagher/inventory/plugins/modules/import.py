#!/usr/bin/python
# -*- coding: utf-8 -*-
# Copyright (c) 2026 Chris Gallagher
# MIT License (see LICENSE)
from __future__ import annotations

DOCUMENTATION = r"""
module: import
short_description: Bulk-import a YAML tree of nodes, variables, definitions and tags
version_added: "0.2.0"
author: Chris Gallagher (@git-cgallagher)
description:
  - Sends a tree in the server's import format (the same as C(ais import)) to C(POST /api/v1/import). All of
    it lands under one revision.
  - Additive. Missing nodes are created, the listed variables and definitions are set, tag members are added, and
    nothing is deleted, moved or re-kinded (and no tag member removed).
    Use M(git_cgallagher.inventory.node) with O(git_cgallagher.inventory.node#module:purge_vars)
    or C(state=absent) to remove things.
  - Idempotent. When the tree already matches, the server writes nothing and the task is not changed. Check mode
    asks the server for a dry run, which runs the import and rolls it back, so it reports the same counts and writes nothing.
  - A tree the server refuses (a kind that does not fit the level schema, an invalid name, a value its variable's
    C(json_schema) rejects, a group-name collision) fails the task, and nothing is written.
options:
  tree:
    description:
      - The tree. Top-level C(all) is required. C(variables) optionally declares definitions
        (C(merge_strategy), C(description), C(json_schema)).
      - Each node takes C(vars), C(children), C(hosts) (host name to its variables), and optionally C(kind) and
        C(inventory_name).
      - C(tags) optionally maps a tag name to C(description) and C(members) (node paths, which must exist once the
        tree is imported).
      - The server's tree export (C(GET /api/v1/tree), C(ais export-tree)) writes this format, so a backup or an
        export at an earlier revision can be imported as it is.
      - Read a file on the controller with C(lookup('ansible.builtin.file', 'seed.yml') | from_yaml).
    type: dict
    required: true
extends_documentation_fragment:
  - git_cgallagher.inventory.api
  - git_cgallagher.inventory.api.write
"""

EXAMPLES = r"""
- name: Initial load from a file on the controller
  git_cgallagher.inventory.import:
    tree: "{{ lookup('ansible.builtin.file', 'seed.yml') | from_yaml }}"
    revision_message: initial load

- name: Add a group with a host and declare a variable
  git_cgallagher.inventory.import:
    tree:
      variables:
        packages: {merge_strategy: unique_append}
      all:
        children:
          commercial:
            children:
              prod:
                children:
                  dc1:
                    children:
                      cache:
                        vars: {packages: [redis]}
                        hosts:
                          cache01: {ansible_host: 10.1.3.11}
      tags:
        patch_wave_1:
          description: first patch wave
          members: [all.commercial.prod.dc1.cache]

- name: Restore a deleted datacenter from the revision before the delete
  git_cgallagher.inventory.import:
    tree: "{{ lookup('ansible.builtin.file', 'dc1-at-41.yml') | from_yaml }}"  # ais export-tree --revision 41 --subtree all.commercial.prod.dc1
    revision_message: restore dc1
"""

RETURN = r"""
nodes_created:
  description: Nodes the import created (or would create, in check mode).
  returned: always
  type: int
variables_set:
  description: Variable values the import set or changed.
  returned: always
  type: int
definitions:
  description: Variable definitions the import created or changed.
  returned: always
  type: list
  elements: str
tags:
  description: Tags the import created or changed (description or new members).
  returned: always
  type: list
  elements: str
key_warnings:
  description:
    - Undeclared keys this task wrote that look like typos of a declared key or of an undeclared key many nodes set,
      one per key and path, each with C(key), C(path), C(did_you_mean), C(declared) and C(message). Each is also an
      Ansible warning. The write still happened; declare the key (M(git_cgallagher.inventory.variable_definition)) to
      silence it, or fix the key.
  returned: always
  type: list
  elements: dict
impact:
  description:
    - In check mode, what the change would do to the hosts, from the server's dry run (C(dry_run=true)), which also
      runs every check the write would and refuses what the write would refuse.
    - C(total) distinct hosts affected; C(vars_changed), C(added), C(removed), C(regrouped) and C(moved) count them by
      kind; C(hosts) lists them sorted by path (at most C(limit), C(truncated) says when there were more) with
      C(status) (C(changed), C(added) or C(removed)), the C(added), C(removed) and C(changed) keys (C(from)/C(to)),
      the groups it C(joined) and C(left) and C(moved_from); C(groups) counts the hosts that join or leave each group.
      With C(--diff) a short summary is shown.
  returned: check mode, when changed
  type: dict
revision:
  description: Inventory revision after the task (the import's, or the current one when nothing changed).
  returned: always
  type: int
"""

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.git_cgallagher.inventory.plugins.module_utils.client import Client, with_impact, write_argument_spec


def main():
    spec = write_argument_spec()
    spec.update(tree=dict(type="dict", required=True))
    module = AnsibleModule(argument_spec=spec, supports_check_mode=True)
    client = Client(module)
    p = module.params
    body = {"tree": p["tree"]}
    if module.check_mode:
        data = client.preview("POST", "/import", body, in_body=True)
    else:
        data = client.write("POST", "/import", body)
    summary = dict((k, data.get(k)) for k in ("nodes_created", "variables_set", "definitions", "tags"))
    extra = {"impact": data["impact"]} if module.check_mode and data.get("changed") and data.get("impact") else {}
    diff = {"before": {}, "after": summary} if data.get("changed") else {"before": {}, "after": {}}
    module.exit_json(
        changed=bool(data.get("changed")),
        revision=data.get("revision"),
        diff=with_impact(diff, extra.get("impact")),
        key_warnings=client.key_warnings,
        **dict(summary, **extra)
    )


if __name__ == "__main__":
    main()

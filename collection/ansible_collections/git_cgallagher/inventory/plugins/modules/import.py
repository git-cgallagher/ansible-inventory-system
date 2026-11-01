#!/usr/bin/python
# -*- coding: utf-8 -*-
# Copyright (c) 2026 Chris Gallagher
# MIT License (see LICENSE)
from __future__ import annotations

DOCUMENTATION = r"""
module: import
short_description: Bulk-import a YAML tree of nodes, variables and definitions
version_added: "0.2.0"
author: Chris Gallagher (@git-cgallagher)
description:
  - Sends a tree in the server's import format (the same as C(ais import)) to C(POST /api/v1/import). All of
    it lands under one revision.
  - Additive. Missing nodes are created, the listed variables and definitions are set, and nothing is
    deleted, moved or re-kinded. Use M(git_cgallagher.inventory.node) with O(git_cgallagher.inventory.node#module:purge_vars)
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
revision:
  description: Inventory revision after the task (the import's, or the current one when nothing changed).
  returned: always
  type: int
"""

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.git_cgallagher.inventory.plugins.module_utils.client import Client, write_argument_spec


def main():
    spec = write_argument_spec()
    spec.update(tree=dict(type="dict", required=True))
    module = AnsibleModule(argument_spec=spec, supports_check_mode=True)
    client = Client(module)
    p = module.params
    body = {"tree": p["tree"]}
    if module.check_mode:
        _status, data = client.request("POST", "/import", body=dict(body, dry_run=True), allow=())
    else:
        data = client.write("POST", "/import", body)
    summary = dict((k, data.get(k)) for k in ("nodes_created", "variables_set", "definitions"))
    module.exit_json(
        changed=bool(data.get("changed")),
        revision=data.get("revision"),
        diff={"before": {}, "after": summary} if data.get("changed") else {"before": {}, "after": {}},
        **summary
    )


if __name__ == "__main__":
    main()

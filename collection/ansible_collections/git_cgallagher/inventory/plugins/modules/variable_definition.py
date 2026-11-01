#!/usr/bin/python
# -*- coding: utf-8 -*-
# Copyright (c) 2026 Chris Gallagher
# MIT License (see LICENSE)
from __future__ import annotations

DOCUMENTATION = r"""
module: variable_definition
short_description: Declare a variable's merge strategy
version_added: "0.2.0"
author: Chris Gallagher (@git-cgallagher)
description:
  - Declares how a variable combines down the tree (C(replace), C(merge), C(append), C(unique_append)),
    with an optional description and JSON schema, or removes the declaration (back to C(replace)).
  - Declarations are not temporal; a change applies to every revision's export.
options:
  name:
    description: Variable name.
    type: str
    required: true
  merge_strategy:
    description: How contributions down the path combine.
    type: str
    choices: [replace, merge, append, unique_append]
    default: replace
  description:
    description: Free text.
    type: str
  json_schema:
    description:
      - JSON Schema (Draft 2020-12 unless it names another C($schema)) that every stored value of the variable must
        match. The server enforces it on every write of the key and refuses a schema that is invalid or that a value
        already stored somewhere in the tree violates (the task then fails with the offending paths).
      - It checks each node's own value, not the folded result of C(merge)/C(append) variables.
    type: dict
  state:
    description: Whether the declaration must exist.
    type: str
    choices: [present, absent]
    default: present
extends_documentation_fragment:
  - git_cgallagher.inventory.api
  - git_cgallagher.inventory.api.write
"""

EXAMPLES = r"""
- name: packages accumulate down the tree
  git_cgallagher.inventory.variable_definition:
    name: packages
    merge_strategy: unique_append
    description: OS packages installed everywhere; each level adds its own.

- name: Undeclare (back to plain replace)
  git_cgallagher.inventory.variable_definition:
    name: packages
    state: absent
"""

RETURN = r"""
definition:
  description: The declaration as stored; empty when absent.
  returned: always
  type: dict
revision:
  description: Inventory revision after the task.
  returned: always
  type: int
"""

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.git_cgallagher.inventory.plugins.module_utils.client import Client, write_argument_spec


def main():
    spec = write_argument_spec()
    spec.update(
        name=dict(type="str", required=True),
        merge_strategy=dict(type="str", choices=["replace", "merge", "append", "unique_append"], default="replace"),
        description=dict(type="str"),
        json_schema=dict(type="dict"),
        state=dict(type="str", choices=["present", "absent"], default="present"),
    )
    module = AnsibleModule(argument_spec=spec, supports_check_mode=True)
    client = Client(module)
    p = module.params
    current = client.definition(p["name"])
    if p["state"] == "absent":
        if current is None:
            module.exit_json(changed=False, definition={}, revision=client.current_revision())
        diff = {"before": current, "after": {}}
        if module.check_mode:
            module.exit_json(changed=True, definition={}, diff=diff, revision=client.current_revision())
        client.write("DELETE", "/variables/%s" % p["name"])
        module.exit_json(changed=True, definition={}, diff=diff, revision=client.revision)
    wanted = {"name": p["name"], "merge_strategy": p["merge_strategy"], "description": p["description"], "json_schema": p["json_schema"]}
    if current == wanted:
        module.exit_json(changed=False, definition=current, revision=client.current_revision())
    diff = {"before": current or {}, "after": wanted}
    if module.check_mode:
        module.exit_json(changed=True, definition=wanted, diff=diff, revision=client.current_revision())
    body = {"merge_strategy": p["merge_strategy"], "description": p["description"], "json_schema": p["json_schema"]}
    data = client.write("PUT", "/variables/%s" % p["name"], body)
    module.exit_json(changed=True, definition=data["definition"], diff=diff, revision=client.revision)


if __name__ == "__main__":
    main()

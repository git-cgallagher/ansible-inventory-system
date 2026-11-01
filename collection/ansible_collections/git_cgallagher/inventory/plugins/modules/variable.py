#!/usr/bin/python
# -*- coding: utf-8 -*-
# Copyright (c) 2026 Chris Gallagher
# MIT License (see LICENSE)
from __future__ import annotations

DOCUMENTATION = r"""
module: variable
short_description: Set or remove one variable at a node
version_added: "0.2.0"
author: Chris Gallagher (@git-cgallagher)
description:
  - Sets O(key) to O(value) at the node O(path), or removes it. Values are compared as JSON, so an
    unchanged value is not a change and needs no revision.
options:
  path:
    description: Path of the node (a group or a host), for example C(all.commercial.prod).
    type: str
    required: true
  key:
    description: Variable name.
    type: str
    required: true
  value:
    description: Value (any JSON type). Required when O(state=present).
    type: raw
  state:
    description: Whether the variable must be set at this node.
    type: str
    choices: [present, absent]
    default: present
extends_documentation_fragment:
  - git_cgallagher.inventory.api
  - git_cgallagher.inventory.api.write
"""

EXAMPLES = r"""
- name: Lower the log level for commercial prod
  git_cgallagher.inventory.variable:
    path: all.commercial.prod
    key: log_level
    value: error

- name: Stop overriding http_port on web02
  git_cgallagher.inventory.variable:
    path: all.commercial.prod.dc1.web.web02
    key: http_port
    state: absent
"""

RETURN = r"""
value:
  description: The value now stored at the node (absent when removed).
  returned: when state=present
  type: raw
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
        path=dict(type="str", required=True),
        key=dict(type="str", required=True, no_log=False),
        value=dict(type="raw"),
        state=dict(type="str", choices=["present", "absent"], default="present"),
    )
    module = AnsibleModule(argument_spec=spec, supports_check_mode=True, required_if=[("state", "present", ["value"])])
    client = Client(module)
    p = module.params
    node = client.node(p["path"])
    if node is None:
        module.fail_json(msg="ais: node %s does not exist" % p["path"])
    current = node.get("vars") or {}
    key = p["key"]
    present = key in current
    if p["state"] == "absent":
        if not present:
            module.exit_json(changed=False, revision=client.current_revision())
        diff = {"before": {key: current[key]}, "after": {}}
        if module.check_mode:
            module.exit_json(changed=True, diff=diff, revision=client.current_revision())
        client.write("DELETE", "/nodes/%d/vars/%s" % (node["id"], key))
        module.exit_json(changed=True, diff=diff, revision=client.revision)
    if present and current[key] == p["value"]:
        module.exit_json(changed=False, value=current[key], revision=client.current_revision())
    diff = {"before": {key: current[key]} if present else {}, "after": {key: p["value"]}}
    if module.check_mode:
        module.exit_json(changed=True, value=p["value"], diff=diff, revision=client.current_revision())
    data = client.write("PUT", "/nodes/%d/vars/%s" % (node["id"], key), {"value": p["value"]})
    module.exit_json(changed=True, value=data["value"], diff=diff, revision=client.revision)


if __name__ == "__main__":
    main()

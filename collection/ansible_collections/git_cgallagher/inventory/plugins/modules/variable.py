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
  - An undeclared O(key) that looks like a typo of a declared key (or of an undeclared key many nodes set) is
    still written, with an Ansible warning ("did you mean ...?") and the details in RV(key_warnings).
  - When the server runs in strict mode (C(AIS_STRICT_KEYS=true)), a key that is not declared and not set on any
    node yet is refused; declare it first with M(git_cgallagher.inventory.variable_definition).
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
  description: Inventory revision after the task.
  returned: always
  type: int
"""

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.git_cgallagher.inventory.plugins.module_utils.client import Client, with_impact, write_argument_spec


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
            module.exit_json(changed=False, revision=client.current_revision(), key_warnings=[])
        diff = {"before": {key: current[key]}, "after": {}}
        if module.check_mode:
            data = client.preview("DELETE", "/nodes/%d/vars/%s" % (node["id"], key))
            module.exit_json(changed=True, diff=with_impact(diff, data.get("impact")), impact=data.get("impact"), revision=data["revision"], key_warnings=[])
        client.write("DELETE", "/nodes/%d/vars/%s" % (node["id"], key))
        module.exit_json(changed=True, diff=diff, revision=client.revision, key_warnings=[])
    if present and current[key] == p["value"]:
        module.exit_json(changed=False, value=current[key], revision=client.current_revision(), key_warnings=[])
    diff = {"before": {key: current[key]} if present else {}, "after": {key: p["value"]}}
    if module.check_mode:
        data = client.preview("PUT", "/nodes/%d/vars/%s" % (node["id"], key), {"value": p["value"]})
        module.exit_json(
            changed=True, value=p["value"], diff=with_impact(diff, data.get("impact")), impact=data.get("impact"), revision=data["revision"], key_warnings=client.key_warnings
        )
    data = client.write("PUT", "/nodes/%d/vars/%s" % (node["id"], key), {"value": p["value"]})
    module.exit_json(changed=True, value=data["value"], diff=diff, revision=client.revision, key_warnings=client.key_warnings)


if __name__ == "__main__":
    main()

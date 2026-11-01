#!/usr/bin/python
# -*- coding: utf-8 -*-
# Copyright (c) 2026 Chris Gallagher
# MIT License (see LICENSE)
from __future__ import annotations

DOCUMENTATION = r"""
module: node_info
short_description: Read a node, its variables and its children
version_added: "0.2.0"
author: Chris Gallagher (@git-cgallagher)
description:
  - Returns the node at O(path) (or with O(id)) with the variables set at that node, and its live
    children. A missing node is not an error (RV(exists) is false).
options:
  path:
    description: Node path, for example C(all.commercial.prod).
    type: str
  id:
    description: Node id (any row, including closed ones).
    type: int
extends_documentation_fragment:
  - git_cgallagher.inventory.api
"""

EXAMPLES = r"""
- git_cgallagher.inventory.node_info:
    path: all.commercial.prod.dc1
  register: dc1

- ansible.builtin.debug:
    msg: "{{ dc1.children | map(attribute='name') | list }}"
"""

RETURN = r"""
exists:
  description: Whether a live node was found.
  returned: always
  type: bool
node:
  description: The node with its variables (C(vars)); empty when not found.
  returned: always
  type: dict
children:
  description: Live children (without variables).
  returned: always
  type: list
  elements: dict
"""

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.git_cgallagher.inventory.plugins.module_utils.client import Client, client_argument_spec


def main():
    spec = client_argument_spec()
    spec.update(path=dict(type="str"), id=dict(type="int"))
    module = AnsibleModule(argument_spec=spec, supports_check_mode=True, required_one_of=[("path", "id")], mutually_exclusive=[("path", "id")])
    client = Client(module)
    ref = module.params["path"] if module.params["path"] else module.params["id"]
    status, node = client.get("/nodes/%s" % ref)
    if status == 404 or node is None:
        module.exit_json(changed=False, exists=False, node={}, children=[])
    children = []
    if node.get("deleted_rev") is None:
        _status, children = client.get("/nodes/%d/children" % node["id"], allow=())
    module.exit_json(changed=False, exists=node.get("deleted_rev") is None, node=node, children=children or [])


if __name__ == "__main__":
    main()

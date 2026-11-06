#!/usr/bin/python
# -*- coding: utf-8 -*-
# Copyright (c) 2026 Chris Gallagher
# MIT License (see LICENSE)
from __future__ import annotations

DOCUMENTATION = r"""
module: profile_info
short_description: Read one profile or list every profile
version_added: "0.4.0"
author: Chris Gallagher (@git-cgallagher)
description:
  - With O(name), returns that profile (description, values) and where it is attached, now or as of O(revision). A
    missing profile is not an error (RV(exists) is false).
  - Without O(name), returns every live profile with its keys and how many nodes it is attached to.
options:
  name:
    description: Profile name.
    type: str
  revision:
    description: Read the profile as it was at this inventory revision (with O(name) only).
    type: int
extends_documentation_fragment:
  - git_cgallagher.inventory.api
"""

EXAMPLES = r"""
- git_cgallagher.inventory.profile_info:
    name: gpu-a100
  register: gpu

- ansible.builtin.debug:
    msg: "{{ gpu.profile.vars.gpu_driver }} on {{ gpu.profile.attached | map(attribute='path') | list }}"

- git_cgallagher.inventory.profile_info:
  register: every_profile
"""

RETURN = r"""
exists:
  description: Whether the profile O(name) exists (at O(revision) when given).
  returned: when O(name) is given
  type: bool
profile:
  description:
    - The profile (C(name), C(description), C(vars), C(revision)) and C(attached), a list of C(path), C(kind) and
      C(position) (its place in that node's list, 0 = lowest precedence); empty when it does not exist.
  returned: when O(name) is given
  type: dict
profiles:
  description: Every live profile (C(name), C(description), C(keys), C(attached) = how many nodes), sorted by name.
  returned: when O(name) is not given
  type: list
  elements: dict
"""

import re

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.git_cgallagher.inventory.plugins.module_utils.client import Client, client_argument_spec


def main():
    spec = client_argument_spec()
    spec.update(name=dict(type="str"), revision=dict(type="int"))
    module = AnsibleModule(argument_spec=spec, supports_check_mode=True)
    client = Client(module)
    name = module.params["name"]
    if name is None:
        if module.params["revision"] is not None:
            module.fail_json(msg="ais: revision needs a name")
        _status, rows = client.get("/profiles", allow=())
        module.exit_json(changed=False, profiles=rows or [])
    if not re.match(r"^[A-Za-z0-9_-]{1,255}$", name):  # no such profile can exist; never put it in a URL
        module.exit_json(changed=False, exists=False, profile={})
    path = "/profiles/%s" % name
    if module.params["revision"] is not None:
        path += "?revision=%d" % module.params["revision"]
    status, data = client.get(path)
    if status == 404 or data is None:
        module.exit_json(changed=False, exists=False, profile={})
    module.exit_json(changed=False, exists=True, profile=data)


if __name__ == "__main__":
    main()

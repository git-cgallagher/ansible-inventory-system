#!/usr/bin/python
# -*- coding: utf-8 -*-
# Copyright (c) 2026 Chris Gallagher
# MIT License (see LICENSE)
from __future__ import annotations

DOCUMENTATION = r"""
module: tag_info
short_description: Read one tag or list every tag
version_added: "0.4.0"
author: Chris Gallagher (@git-cgallagher)
description:
  - With O(name), returns that tag (its description and member node paths). A missing tag is not an error
    (RV(exists) is false).
  - Without O(name), returns every tag, sorted by name.
  - Members are the live node paths; the inventory group each tag is emitted as is its name with C(-) turned into
    C(_) (RV(group)).
options:
  name:
    description: Tag name.
    type: str
extends_documentation_fragment:
  - git_cgallagher.inventory.api
"""

EXAMPLES = r"""
- git_cgallagher.inventory.tag_info:
    name: patch_wave_1
  register: wave

- ansible.builtin.debug:
    msg: "{{ wave.tag.members }}"

- git_cgallagher.inventory.tag_info:
  register: all_tags
"""

RETURN = r"""
exists:
  description: Whether the tag O(name) exists.
  returned: when O(name) is given
  type: bool
tag:
  description: The tag (C(name), C(description), C(members)); empty when it does not exist.
  returned: when O(name) is given
  type: dict
group:
  description: The inventory group the tag O(name) is emitted as.
  returned: when O(name) is given
  type: str
  sample: gpu_canary
tags:
  description: Every tag, sorted by name.
  returned: when O(name) is not given
  type: list
  elements: dict
"""

import re

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.git_cgallagher.inventory.plugins.module_utils.client import Client, client_argument_spec


def main():
    spec = client_argument_spec()
    spec.update(name=dict(type="str"))
    module = AnsibleModule(argument_spec=spec, supports_check_mode=True)
    client = Client(module)
    name = module.params["name"]
    if name is None:
        _status, tags = client.get("/tags", allow=())
        module.exit_json(changed=False, tags=tags or [])
    group = re.sub(r"[^A-Za-z0-9_]", "_", name)
    if not re.match(r"^[A-Za-z0-9_-]{1,255}$", name):  # no such tag can exist; never put it in a URL
        module.exit_json(changed=False, exists=False, tag={}, group=group)
    status, tag = client.get("/tags/%s" % name)
    if status == 404 or tag is None:
        module.exit_json(changed=False, exists=False, tag={}, group=group)
    module.exit_json(changed=False, exists=True, tag=tag, group=group)


if __name__ == "__main__":
    main()

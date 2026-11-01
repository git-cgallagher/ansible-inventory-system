#!/usr/bin/python
# -*- coding: utf-8 -*-
# Copyright (c) 2026 Chris Gallagher
# MIT License (see LICENSE)
from __future__ import annotations

DOCUMENTATION = r"""
module: render_info
short_description: Resolve (and template) the variables of a real or hypothetical host
version_added: "0.2.0"
author: Chris Gallagher (@git-cgallagher)
description:
  - Calls C(POST /api/v1/render). With O(host) the host's effective variables; with O(path) a mock host
    at that position. Returns the resolved variables with provenance, and (unless O(template=false))
    the Jinja-rendered values with per-key errors.
options:
  host:
    description: Inventory hostname of a real host.
    type: str
  path:
    description: Position of a mock host, for example C(all.commercial.prod.dc1.web).
    type: str
  overrides:
    description: Variables applied last, as if they were host vars.
    type: dict
  facts:
    description: Mock facts for templating (keys with or without the C(ansible_) prefix).
    type: dict
  revision:
    description: Render as of this inventory revision.
    type: int
  template:
    description: Also evaluate Jinja templates (RV(rendered), RV(errors)).
    type: bool
    default: true
extends_documentation_fragment:
  - git_cgallagher.inventory.api
"""

EXAMPLES = r"""
- git_cgallagher.inventory.render_info:
    host: web01
    facts:
      default_ipv4:
        address: 10.1.1.11
  register: web01

- git_cgallagher.inventory.render_info:
    path: all.commercial.lab.dc1.web
    overrides:
      http_port: 9000
  register: mock
"""

RETURN = r"""
resolved:
  description: Effective variables.
  returned: always
  type: dict
provenance:
  description: Per key, the nodes that contributed (path, group, strategy, value).
  returned: always
  type: dict
chain:
  description: Root-first group chain the host sits in.
  returned: always
  type: list
  elements: str
rendered:
  description: Resolved variables after Jinja evaluation.
  returned: when template=true
  type: dict
errors:
  description: Per-key template errors.
  returned: when template=true
  type: dict
revision:
  description: Revision rendered.
  returned: always
  type: int
"""

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.git_cgallagher.inventory.plugins.module_utils.client import Client, client_argument_spec


def main():
    spec = client_argument_spec()
    spec.update(
        host=dict(type="str"),
        path=dict(type="str"),
        overrides=dict(type="dict"),
        facts=dict(type="dict"),
        revision=dict(type="int"),
        template=dict(type="bool", default=True),
    )
    module = AnsibleModule(argument_spec=spec, supports_check_mode=True, required_one_of=[("host", "path")])
    client = Client(module)
    p = module.params
    body = {"template": p["template"]}
    for key in ("host", "path", "overrides", "facts", "revision"):
        if p[key] is not None:
            body[key] = p[key]
    _status, data = client.request("POST", "/render", body=body, allow=())
    out = {k: data.get(k) for k in ("resolved", "provenance", "chain", "revision", "path", "host")}
    if p["template"]:
        out["rendered"] = data.get("rendered")
        out["errors"] = data.get("errors")
    module.exit_json(changed=False, **out)


if __name__ == "__main__":
    main()

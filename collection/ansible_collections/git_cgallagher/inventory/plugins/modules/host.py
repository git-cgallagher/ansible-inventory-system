#!/usr/bin/python
# -*- coding: utf-8 -*-
# Copyright (c) 2026 Chris Gallagher
# MIT License (see LICENSE)
from __future__ import annotations

DOCUMENTATION = r"""
module: host
short_description: Create, converge or delete a host
version_added: "0.2.0"
author: Chris Gallagher (@git-cgallagher)
description:
  - Convenience wrapper around M(git_cgallagher.inventory.node) for hosts. Creates the host node
    under O(parent_path) and sets C(ansible_host), C(ansible_port), C(ansible_user) and any O(vars).
  - Host names are unique across the whole inventory. If the host already exists under a different
    parent the module fails (move it on the server first); if it exists under O(parent_path) its
    variables are converged.
options:
  name:
    description: Host name (inventory_hostname).
    type: str
    required: true
  parent_path:
    description: Path of the group the host lives in. Required to create a host; optional to converge or delete one.
    type: str
  state:
    description: Whether the host must exist.
    type: str
    choices: [present, absent]
    default: present
  ansible_host:
    description: Sets the C(ansible_host) variable.
    type: str
  ansible_port:
    description: Sets the C(ansible_port) variable.
    type: int
  ansible_user:
    description: Sets the C(ansible_user) variable.
    type: str
  vars:
    description: Further host variables (JSON values).
    type: dict
    default: {}
  purge_vars:
    description: Remove host variables not given by this task (the C(ansible_*) options count as given).
    type: bool
    default: false
extends_documentation_fragment:
  - git_cgallagher.inventory.api
  - git_cgallagher.inventory.api.write
"""

EXAMPLES = r"""
- name: web03 in the dc1 web group
  git_cgallagher.inventory.host:
    name: web03
    parent_path: all.commercial.prod.dc1.web
    ansible_host: 10.1.1.13
    vars:
      packages: [tcpdump]

- name: Retire it
  git_cgallagher.inventory.host:
    name: web03
    state: absent
"""

RETURN = r"""
host:
  description: The host node as the server returns it (with its live variables); empty when absent.
  returned: always
  type: dict
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
    - A task that makes several writes (node patch plus variables) previews each against the current inventory and
      merges the answers by host; the counts are exact unless a preview was truncated, then they are lower bounds.
  returned: check mode, when changed
  type: dict
revision:
  description: Inventory revision after the task.
  returned: always
  type: int
"""

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.git_cgallagher.inventory.plugins.module_utils.client import (
    Client,
    ensure_node,
    merge_impacts,
    remove_node,
    with_impact,
    write_argument_spec,
)


def find_host(client, name):
    """The live host node called name anywhere in the tree, or None."""
    _status, nodes = client.get("/nodes", allow=())
    for n in nodes or []:
        if n["kind"] == "host" and n["name"] == name:
            return client.node(n["id"])
    return None


def main():
    spec = write_argument_spec()
    spec.update(
        name=dict(type="str", required=True),
        parent_path=dict(type="str"),
        state=dict(type="str", choices=["present", "absent"], default="present"),
        ansible_host=dict(type="str"),
        ansible_port=dict(type="int"),
        ansible_user=dict(type="str"),
        vars=dict(type="dict", default={}),
        purge_vars=dict(type="bool", default=False),
    )
    module = AnsibleModule(argument_spec=spec, supports_check_mode=True)
    client = Client(module)
    p = module.params
    existing = client.node("%s.%s" % (p["parent_path"], p["name"])) if p["parent_path"] else None
    if existing is None:
        existing = find_host(client, p["name"])
    if existing is not None and existing["kind"] != "host":
        module.fail_json(msg="ais: %s exists but is a %s, not a host" % (existing["path"], existing["kind"]))

    if p["state"] == "absent":
        if existing is None:
            module.exit_json(changed=False, host={}, revision=client.current_revision(), key_warnings=[])
        changed, _deleted = remove_node(client, module, existing["path"])
        impact = merge_impacts(client.impacts)
        module.exit_json(changed=changed, host={}, key_warnings=[], **({"impact": impact} if impact else {}),
                         revision=client.revision if client.revision is not None else client.current_revision(),
                         diff=with_impact({"before": {"path": existing["path"]}, "after": {}}, impact))

    if existing is not None and p["parent_path"] and existing["path"] != "%s.%s" % (p["parent_path"], p["name"]):
        module.fail_json(msg="ais: host %s already exists at %s, not under %s (move it on the server)" % (p["name"], existing["path"], p["parent_path"]))
    if existing is None and not p["parent_path"]:
        module.fail_json(msg="ais: host %s does not exist; parent_path is required to create it" % p["name"])
    path = existing["path"] if existing is not None else "%s.%s" % (p["parent_path"], p["name"])
    variables = dict(p["vars"])
    for key in ("ansible_host", "ansible_port", "ansible_user"):
        if p[key] is not None:
            variables[key] = p[key]
    changed, node, diff = ensure_node(client, module, path, kind="host", variables=variables, purge_vars=p["purge_vars"])
    revision = client.revision if client.revision is not None else client.current_revision()
    impact = merge_impacts(client.impacts)  # check mode only
    module.exit_json(changed=changed, host=node, diff=diff, revision=revision, key_warnings=client.key_warnings, **({"impact": impact} if impact else {}))


if __name__ == "__main__":
    main()

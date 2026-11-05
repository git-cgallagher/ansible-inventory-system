#!/usr/bin/python
# -*- coding: utf-8 -*-
# Copyright (c) 2026 Chris Gallagher
# MIT License (see LICENSE)
from __future__ import annotations

DOCUMENTATION = r"""
module: node
short_description: Create, converge or delete a node of the inventory tree
version_added: "0.2.0"
author: Chris Gallagher (@git-cgallagher)
description:
  - Ensures a node (business, tier, datacenter, group or host) exists at O(path) with the given
    metadata and variables, or that it does not exist.
  - Idempotent, with check and diff mode. Variables listed in O(vars) are set; others are left alone
    unless O(purge_vars) is true. Creating a node needs its parent to exist already.
  - A node's kind cannot change in place and the root C(all) can be neither created nor deleted;
    renames and moves are M(git_cgallagher.inventory.node_move).
options:
  path:
    description: Full path of the node, for example C(all.commercial.prod.dc1.cache).
    type: str
    required: true
  state:
    description:
      - Whether the node must exist. C(absent) deletes the node with its whole subtree.
      - A node with descendants is only deleted with O(recursive=true); otherwise the task fails
        and names how many nodes and hosts the delete would take.
    type: str
    choices: [present, absent]
    default: present
  recursive:
    description:
      - With O(state=absent), confirm deleting a node that has descendants (the whole subtree goes).
      - Not needed for a host or an empty group. Check and diff mode list every path that would be deleted.
      - A deleted subtree can be brought back with C(POST /api/v1/nodes/restore) or C(ais restore).
    type: bool
    default: false
    version_added: "0.4.0"
  kind:
    description:
      - Kind of a node being created. Inferred from the depth when omitted (depth 1 business,
        2 tier, 3 datacenter, deeper group); hosts must say C(host).
      - On an existing node a different kind is an error.
    type: str
    choices: [business, tier, datacenter, group, host]
  inventory_name:
    description: Override for the emitted Ansible group name (not for hosts). An empty string clears it.
    type: str
  meta:
    description: Free-form metadata stored on the node. Replaces the stored metadata when given.
    type: dict
  vars:
    description: Variables to set at this node (JSON values).
    type: dict
    default: {}
  purge_vars:
    description: Remove variables set at the node that are not in O(vars).
    type: bool
    default: false
extends_documentation_fragment:
  - git_cgallagher.inventory.api
  - git_cgallagher.inventory.api.write
"""

EXAMPLES = r"""
- name: A cache group under dc1 with two vars
  git_cgallagher.inventory.node:
    path: all.commercial.prod.dc1.cache
    vars:
      redis_port: 6379
      packages: [redis]

- name: A host (kind is required for hosts)
  git_cgallagher.inventory.node:
    path: all.commercial.prod.dc1.cache.cache01
    kind: host
    vars:
      ansible_host: 10.1.3.11

- name: Remove the group and everything under it (refused without recursive when it has children)
  git_cgallagher.inventory.node:
    path: all.commercial.prod.dc1.cache
    state: absent
    recursive: true
"""

RETURN = r"""
node:
  description: The node as the server returns it (with its live variables); empty when absent.
  returned: always
  type: dict
deleted:
  description: Paths closed by a delete (the node and its subtree); in check mode, the paths that would be.
  returned: when state=absent and changed
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
    - A task that makes several writes (node patch plus variables) previews each against the current inventory and
      merges the answers by host; the counts are exact unless a preview was truncated, then they are lower bounds.
  returned: check mode, when changed
  type: dict
revision:
  description: Inventory revision after the task (the last write's, or the current one when nothing changed).
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


def main():
    spec = write_argument_spec()
    spec.update(
        path=dict(type="str", required=True),
        state=dict(type="str", choices=["present", "absent"], default="present"),
        kind=dict(type="str", choices=["business", "tier", "datacenter", "group", "host"]),
        inventory_name=dict(type="str"),
        meta=dict(type="dict"),
        vars=dict(type="dict", default={}),
        purge_vars=dict(type="bool", default=False),
        recursive=dict(type="bool", default=False),
    )
    module = AnsibleModule(argument_spec=spec, supports_check_mode=True)
    client = Client(module)
    p = module.params
    if p["state"] == "absent":
        changed, deleted = remove_node(client, module, p["path"], recursive=p["recursive"])
        impact = merge_impacts(client.impacts)
        module.exit_json(changed=changed, node={}, deleted=deleted, key_warnings=[], impact=impact,
                         revision=client.revision if client.revision is not None else client.current_revision(),
                         diff=with_impact({"before": {"path": p["path"], "deleted": deleted} if changed else {}, "after": {}}, impact))
    changed, node, diff = ensure_node(
        client, module, p["path"], kind=p["kind"], inventory_name=p["inventory_name"], meta=p["meta"], variables=p["vars"], purge_vars=p["purge_vars"]
    )
    revision = client.revision if client.revision is not None else client.current_revision()
    module.exit_json(changed=changed, node=node, diff=diff, revision=revision, key_warnings=client.key_warnings, impact=merge_impacts(client.impacts))


if __name__ == "__main__":
    main()

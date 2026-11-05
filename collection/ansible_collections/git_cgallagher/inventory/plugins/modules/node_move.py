#!/usr/bin/python
# -*- coding: utf-8 -*-
# Copyright (c) 2026 Chris Gallagher
# MIT License (see LICENSE)
from __future__ import annotations

DOCUMENTATION = r"""
module: node_move
short_description: Move or rename a node of the inventory tree with its subtree
version_added: "0.2.0"
author: Chris Gallagher (@git-cgallagher)
description:
  - Moves the node at O(path) so that it ends up at O(new_path), through C(POST /api/v1/nodes/{id}/move). The new
    parent must exist; a change of the last path element renames the node. Its whole subtree, variables and tag
    memberships move with it.
  - The server closes the old rows and opens new ones, so B(node ids change) (the result's RV(moved) maps old to
    new ids) and pinned revisions still see the old tree.
  - Idempotent. When nothing lives at O(path) and a node lives at O(new_path), the move is taken as done and the task
    is not changed. When neither exists the task fails, as it does when both do.
  - Refused by the server, and failing the task, are moving the root, moving a node under its own subtree, a kind
    the new parent cannot have, and anything the exporter would reject afterwards (a group-name or tag collision, a
    duplicate host name).
options:
  path:
    description: Current full path of the node, for example C(all.commercial.prod.dc1.cache).
    type: str
    required: true
  new_path:
    description: Full path the node must end up at, for example C(all.commercial.prod.dc2.cache).
    type: str
    required: true
extends_documentation_fragment:
  - git_cgallagher.inventory.api
  - git_cgallagher.inventory.api.write
"""

EXAMPLES = r"""
- name: Move the cache group from dc1 to dc2
  git_cgallagher.inventory.node_move:
    path: all.commercial.prod.dc1.cache
    new_path: all.commercial.prod.dc2.cache

- name: Rename a host
  git_cgallagher.inventory.node_move:
    path: all.commercial.prod.dc1.web.web01
    new_path: all.commercial.prod.dc1.web.web01-old
"""

RETURN = r"""
node:
  description: The node at O(new_path) as the server returns it (with its live variables).
  returned: always
  type: dict
moved:
  description: One entry per node of the moved subtree with C(old_id), C(id), C(from) and C(to); empty when not changed.
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
  description: Inventory revision after the task (the move's, or the current one when nothing changed).
  returned: always
  type: int
"""

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.git_cgallagher.inventory.plugins.module_utils.client import Client, split_path, with_impact, write_argument_spec


def main():
    spec = write_argument_spec()
    spec.update(path=dict(type="str", required=True), new_path=dict(type="str", required=True))
    module = AnsibleModule(argument_spec=spec, supports_check_mode=True)
    client = Client(module)
    path, new_path = module.params["path"], module.params["new_path"]
    if path == new_path:
        module.fail_json(msg="ais: path and new_path are the same")
    new_parent, new_name = split_path(new_path)
    if new_parent is None:
        module.fail_json(msg="ais: new_path %r must be below the root" % new_path)

    source = client.node(path)
    target = client.node(new_path)
    if source is None and target is not None:
        module.exit_json(changed=False, node=target, moved=[], revision=client.current_revision(), diff={"before": {}, "after": {}})
    if source is None:
        module.fail_json(msg="ais: neither %s nor %s exists" % (path, new_path))
    if target is not None:
        module.fail_json(msg="ais: both %s and %s exist; refusing to move onto a live node" % (path, new_path))

    diff = {"before": {"path": path}, "after": {"path": new_path}}
    body = {"parent_path": new_parent, "name": new_name}
    if module.check_mode:
        if client.node(new_parent) is None:
            module.fail_json(msg="ais: new parent %s does not exist" % new_parent)
        data = client.preview("POST", "/nodes/%d/move" % source["id"], body)  # the server refuses what the move would
        module.exit_json(
            changed=True,
            node=dict(source, path=new_path, name=new_name),
            moved=[],
            revision=data["revision"],
            diff=with_impact(diff, data.get("impact")),
            impact=data.get("impact"),
        )
    data = client.write("POST", "/nodes/%d/move" % source["id"], body)
    module.exit_json(changed=True, node=data["node"], moved=data.get("moved", []), revision=data["revision"], diff=diff)


if __name__ == "__main__":
    main()

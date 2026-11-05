#!/usr/bin/python
# -*- coding: utf-8 -*-
# Copyright (c) 2026 Chris Gallagher
# MIT License (see LICENSE)
from __future__ import annotations

DOCUMENTATION = r"""
module: revert
short_description: Undo an inventory revision, or every revision of a change id
version_added: "0.4.0"
author: Chris Gallagher (@git-cgallagher)
description:
  - Undoes one revision (O(revision)) or every revision that carries a change id (O(revert_change)) as one new
    revision, through C(POST /api/v1/revisions/{id}/revert) or C(POST /api/v1/changes/{change}/revert). The server
    applies the inverse of each audited write, newest first, and records each as an ordinary audit row.
  - Tag the writes of a change with the O(change) option (on every module, or once with C(module_defaults)); this
    module then undoes them together, for example in the C(rescue) of a failed rollout.
  - Idempotent. When the revision or change was already reverted, the task is not changed.
  - Refused, failing the task with the server's message, are a later revision (not part of what is reverted) that
    changed the same node, key, definition or tag, unless O(force); an inverse that cannot apply now (a node that is
    gone, a path that is taken, a value its JSON Schema now rejects), even with O(force); and revisions with nothing
    recorded to invert.
  - Check mode asks the server for a dry run, which applies the same rules and lists the operations and the hosts
    whose resolved variables would change.
options:
  revision:
    description: The revision to undo. Mutually exclusive with O(revert_change).
    type: int
  revert_change:
    description:
      - Undo every revision that carries this change id (the O(change) option or the C(X-AIS-Change) header of the
        writes). Mutually exclusive with O(revision).
      - The O(change) option, by contrast, is the change id recorded on the revert's own revision.
    type: str
  force:
    description:
      - Revert even though later revisions changed the same things. Their changes to those things are overwritten.
    type: bool
    default: false
extends_documentation_fragment:
  - git_cgallagher.inventory.api
  - git_cgallagher.inventory.api.write
"""

EXAMPLES = r"""
- name: A rollout whose writes all carry one change id
  module_defaults:
    group/git_cgallagher.inventory.inventory:
      change: SRE-1234
  block:
    - name: Move the cache group
      git_cgallagher.inventory.node_move:
        path: all.commercial.prod.dc1.cache
        new_path: all.commercial.prod.dc2.cache
    - name: Point it at the new DNS
      git_cgallagher.inventory.variable:
        path: all.commercial.prod.dc2.cache
        key: dns_servers
        value: [10.2.0.2]
  rescue:
    - name: Undo everything SRE-1234 changed, as one revision
      git_cgallagher.inventory.revert:
        revert_change: SRE-1234
        change: SRE-1234-rollback

- name: Undo one revision
  git_cgallagher.inventory.revert:
    revision: 42
"""

RETURN = r"""
reverted:
  description: The revisions undone.
  returned: changed
  type: list
  elements: int
operations:
  description:
    - The inverse operations, newest reverted revision first, each with C(reverts) (the revision it undoes),
      C(action), C(target_type), C(target_id), C(summary), C(before) and C(after) (the audit row it records).
  returned: changed
  type: list
  elements: dict
hosts:
  description: In check mode, the hosts whose resolved variables would change, with C(status) and the keys.
  returned: check mode
  type: list
  elements: dict
key_warnings:
  description:
    - Undeclared keys this task wrote that look like typos of a declared key or of an undeclared key many nodes set,
      one per key and path, each with C(key), C(path), C(did_you_mean), C(declared) and C(message). Each is also an
      Ansible warning. The write still happened; declare the key (M(git_cgallagher.inventory.variable_definition)) to
      silence it, or fix the key.
  returned: always
  type: list
  elements: dict
revision:
  description: Inventory revision after the task (the revert's, or the current one when nothing changed).
  returned: always
  type: int
reverted_by:
  description: When the task is not changed, the revision(s) that already reverted it.
  returned: when already reverted
  type: list
  elements: int
"""

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.git_cgallagher.inventory.plugins.module_utils.client import Client, write_argument_spec


def main():
    spec = write_argument_spec()
    spec.update(revision=dict(type="int"), revert_change=dict(type="str"), force=dict(type="bool", default=False))
    module = AnsibleModule(
        argument_spec=spec,
        supports_check_mode=True,
        mutually_exclusive=[("revision", "revert_change")],
        required_one_of=[("revision", "revert_change")],
    )
    client = Client(module)
    params = module.params
    if params["revision"] is not None:
        path = "/revisions/%d/revert" % params["revision"]
    else:
        path = "/changes/%s/revert" % params["revert_change"]  # a valid change id is one path segment as is
    body = {"force": params["force"]}

    status, data = client.request("POST", path, body=dict(body, dry_run=True), allow=(409,), keep_body=True)
    if status == 409:
        if isinstance(data, dict) and data.get("error") == "already reverted":
            module.exit_json(changed=False, reverted_by=data.get("reverted_by", []), revision=client.current_revision(), key_warnings=[])
        client.fail(status, data, path)
    if module.check_mode:
        client.collect(data)
        module.exit_json(
            changed=True,
            reverted=data.get("reverted", []),
            operations=data.get("operations", []),
            hosts=data.get("hosts", []),
            revision=data["revision"],
            key_warnings=client.key_warnings,
        )
    if client.revision is None:
        client.revision = data["revision"]  # the dry run's: refuses if anything changed since
    result = client.write("POST", path, body)
    module.exit_json(
        changed=True,
        reverted=result.get("reverted", []),
        operations=result.get("operations", []),
        revision=result["revision"],
        key_warnings=client.key_warnings,
    )


if __name__ == "__main__":
    main()

#!/usr/bin/python
# -*- coding: utf-8 -*-
# Copyright (c) 2026 Chris Gallagher
# MIT License (see LICENSE)
from __future__ import annotations

DOCUMENTATION = r"""
module: profile
short_description: Manage a profile (a named bundle of variables) and where it is attached
version_added: "0.4.0"
author: Chris Gallagher (@git-cgallagher)
description:
  - Creates, converges or deletes a profile, and attaches it to or detaches it from nodes. A profile is a named
    bundle of variables (for example C(gpu-a100) with C(gpu_driver) and C(node_labels)). At every node it is attached
    to, its values apply as a level between the node's parent and the node, below the node's own variables, with
    the same merge strategies. The inventory plugin never sees the profile; the values are emitted on the node.
  - Profiles attach to groups (any non-host node). A node's profiles apply in its list order (first = lowest
    precedence); O(attach) appends this profile to a node's list (the highest precedence among its profiles) when it
    is not there yet. To reorder, use C(PUT /api/v1/nodes/{id}/profiles) or an import.
  - By default O(vars) are set and every other value of the profile stays; O(purge_vars=true) makes the values
    exactly O(vars).
  - Reads first and changes nothing when everything matches. Each change is one locked write (C(If-Match)), audited
    and revertible (M(git_cgallagher.inventory.revert)). A value change is one write however many nodes the profile is
    attached to; in check mode its RV(impact) lists every host it would change.
options:
  name:
    description: Profile name (letters, digits, C(_) and C(-)), for example C(gpu-a100).
    type: str
    required: true
  description:
    description: Free text. When omitted, an existing profile keeps its description.
    type: str
  vars:
    description: Values to set in the profile (any JSON types).
    type: dict
  purge_vars:
    description: When true, the profile's values become exactly O(vars); other keys are removed.
    type: bool
    default: false
  attach:
    description:
      - Paths of nodes (groups) to attach the profile to, for example C(all.commercial.prod.use1.gpu.a100). A node
        that already has it keeps its order. A missing node, or a host, fails the task.
    type: list
    elements: str
  detach:
    description: Paths of nodes to detach the profile from. A node that does not have it is ignored.
    type: list
    elements: str
  detach_all:
    description:
      - With O(state=absent), detach the profile from every node in the same revision. Without it the server refuses
        to delete a profile that is still attached, and the task fails listing the nodes.
    type: bool
    default: false
  state:
    description: V(present) creates or converges the profile; V(absent) deletes it.
    type: str
    choices: [present, absent]
    default: present
extends_documentation_fragment:
  - git_cgallagher.inventory.api
  - git_cgallagher.inventory.api.write
"""

EXAMPLES = r"""
- name: One profile for the A100 workers, attached in every branch
  git_cgallagher.inventory.profile:
    name: gpu-a100
    description: A100 GPU workers
    vars:
      gpu_driver: "560.35"
      node_labels: {gpu: a100}
    attach:
      - all.commercial.prod.use1.platform.k8s.cluster-a.workers.gpu.a100
      - all.commercial.prod.usw2.platform.k8s.cluster-a.workers.gpu.a100

- name: Bump the driver everywhere it is attached (one write)
  git_cgallagher.inventory.profile:
    name: gpu-a100
    vars:
      gpu_driver: "570.86"
    change: CHG0012345

- name: Detach it from one branch
  git_cgallagher.inventory.profile:
    name: gpu-a100
    detach: [all.commercial.prod.usw2.platform.k8s.cluster-a.workers.gpu.a100]

- name: Delete it, detaching it everywhere
  git_cgallagher.inventory.profile:
    name: gpu-a100
    state: absent
    detach_all: true
"""

RETURN = r"""
profile:
  description: The profile as stored (C(name), C(description), C(vars)); empty when absent.
  returned: always
  type: dict
attached:
  description: Paths of the nodes the profile is attached to after the task, sorted.
  returned: always
  type: list
  elements: str
attached_now:
  description: Paths the task attached the profile to (or would, in check mode).
  returned: always
  type: list
  elements: str
detached:
  description: Paths the task detached the profile from (or would); every attachment when deleted with O(detach_all).
  returned: always
  type: list
  elements: str
key_warnings:
  description: Undeclared keys that look like typos (the server's C(warnings)), each with C(key), C(path), C(did_you_mean).
  returned: when the server warned
  type: list
  elements: dict
impact:
  description:
    - In check mode, what the change would do to the hosts, from the server's dry run of each write (merged by host).
      The server also runs every check the write would (schemas, strict keys, a host as the node) and refuses what
      the write would refuse.
  returned: check mode, when changed
  type: dict
revision:
  description: Inventory revision after the task.
  returned: always
  type: int
"""

import re

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.git_cgallagher.inventory.plugins.module_utils.client import Client, merge_impacts, with_impact, write_argument_spec

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,255}$")  # the server's rule; it decides


def read_profile(client, name):
    status, data = client.get("/profiles/%s" % name)
    return None if status == 404 else data


def plain(doc):
    return {"name": doc["name"], "description": doc.get("description"), "vars": doc.get("vars") or {}}


def main():
    spec = write_argument_spec()
    spec.update(
        name=dict(type="str", required=True),
        description=dict(type="str"),
        vars=dict(type="dict"),
        purge_vars=dict(type="bool", default=False),
        attach=dict(type="list", elements="str"),
        detach=dict(type="list", elements="str"),
        detach_all=dict(type="bool", default=False),
        state=dict(type="str", choices=["present", "absent"], default="present"),
    )
    module = AnsibleModule(argument_spec=spec, supports_check_mode=True)
    p = module.params
    name = p["name"]
    if not NAME_RE.match(name):
        module.fail_json(msg="ais: invalid profile name %r: letters, digits, '_' and '-' only (max 255)" % name)
    both = sorted(set(p["attach"] or []) & set(p["detach"] or []))
    if both:
        module.fail_json(msg="ais: %s is in both attach and detach" % ", ".join(both))
    if p["state"] == "absent" and (p["vars"] or p["attach"] or p["detach"] or p["purge_vars"]):
        module.fail_json(msg="ais: state=absent deletes the profile; vars, purge_vars, attach and detach do not apply")
    client = Client(module)
    current = read_profile(client, name)

    if p["state"] == "absent":
        if current is None:
            module.exit_json(changed=False, profile={}, attached=[], attached_now=[], detached=[], revision=client.current_revision())
        path = "/profiles/%s%s" % (name, "?detach=true" if p["detach_all"] else "")
        gone = [a["path"] for a in current["attached"]]
        diff = {"before": plain(current), "after": {}}
        if module.check_mode:
            data = client.preview("DELETE", path)
            module.exit_json(
                changed=True, profile={}, attached=[], attached_now=[], detached=gone,
                diff=with_impact(diff, data.get("impact")), impact=data.get("impact"), revision=data["revision"],
            )
        client.write("DELETE", path)
        module.exit_json(changed=True, profile={}, attached=[], attached_now=[], detached=gone, diff=diff, revision=client.revision)

    values = dict(p["vars"] or {})
    if current is None:
        wanted = {"name": name, "description": p["description"], "vars": values}
    else:
        merged = values if p["purge_vars"] else dict(current["vars"], **values)
        description = p["description"] if p["description"] is not None else current.get("description")
        wanted = {"name": name, "description": description, "vars": merged}
    profile_changes = current is None or plain(current) != wanted

    attached = set(a["path"] for a in (current or {}).get("attached", []))
    to_attach = []
    for path in p["attach"] or []:
        if path in attached or path in to_attach:
            continue
        node = client.node(path)
        if node is None:
            module.fail_json(msg="ais: node %s does not exist" % path)
        to_attach.append((path, node))
    to_detach = []
    for path in p["detach"] or []:
        if path in attached:
            node = client.node(path)
            if node is not None:
                to_detach.append((path, node))
    after_attached = sorted((attached | set(x for x, _n in to_attach)) - set(x for x, _n in to_detach))
    changed = bool(profile_changes or to_attach or to_detach)
    result = dict(
        profile=wanted if changed else plain(current),
        attached=after_attached,
        attached_now=[x for x, _n in to_attach],
        detached=[x for x, _n in to_detach],
    )
    if not changed:
        module.exit_json(changed=False, revision=client.current_revision(), **result)

    body = {"description": wanted["description"], "vars": wanted["vars"]}
    diff = {"before": dict(plain(current), attached=sorted(attached)) if current else {}, "after": dict(wanted, attached=after_attached)}
    if module.check_mode:
        if profile_changes:
            client.preview("PUT", "/profiles/%s" % name, body)
        if current is not None:  # a profile the task would create cannot be previewed attached
            for _path, node in to_attach:
                client.preview("PUT", "/nodes/%d/profiles/%s" % (node["id"], name), {})
            for _path, node in to_detach:
                client.preview("DELETE", "/nodes/%d/profiles/%s" % (node["id"], name))
        impact = merge_impacts(client.impacts)
        module.exit_json(
            changed=True, diff=with_impact(diff, impact), impact=impact, revision=client.current_revision(), key_warnings=client.key_warnings, **result
        )

    if profile_changes:
        data = client.write("PUT", "/profiles/%s" % name, body)
        result["profile"] = plain(data["profile"])
    for _path, node in to_attach:
        client.write("PUT", "/nodes/%d/profiles/%s" % (node["id"], name), {})
    for _path, node in to_detach:
        client.write("DELETE", "/nodes/%d/profiles/%s" % (node["id"], name))
    module.exit_json(changed=True, diff=diff, revision=client.revision, key_warnings=client.key_warnings, **result)


if __name__ == "__main__":
    main()

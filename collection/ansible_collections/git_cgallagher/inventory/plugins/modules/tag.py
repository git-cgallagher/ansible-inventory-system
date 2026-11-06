#!/usr/bin/python
# -*- coding: utf-8 -*-
# Copyright (c) 2026 Chris Gallagher
# MIT License (see LICENSE)
from __future__ import annotations

DOCUMENTATION = r"""
module: tag
short_description: Manage a tag (a var-less targeting group) and its members
version_added: "0.4.0"
author: Chris Gallagher (@git-cgallagher)
description:
  - Creates, converges or deletes a tag. A tag is a named set of node paths that the inventory plugin emits as a
    group directly under C(all) holding every host under those paths (a host member is just that host), for
    C(--limit) and C(hosts:) patterns. Tags never carry variables.
  - Tag names follow the node name rule (letters, digits, C(_) and C(-)), and the group is named the way a node's group
    is, with C(-) turned into C(_) (C(gpu-canary) is group C(gpu_canary)). The server refuses (422) a name whose group
    is already another tag's (C(gpu-canary) and C(gpu_canary)) or a hierarchy group's, and the reserved names
    (C(all), and the C(business_), C(tier_) and C(datacenter_) prefixes of the automatic tag groups).
  - By default O(paths) are added to the members and every other member stays; O(exclusive=true) makes the members
    exactly O(paths); O(remove_paths) takes members out.
  - Reads the tag first and changes nothing when it already matches. A change is one C(PUT /api/v1/tags/{name})
    (or C(DELETE)) with C(If-Match). When the inventory moved between the read and the write (409 stale) and
    O(expect_revision) is not set, the module reads the tag again and retries once, only if the tag still differs
    from what the task wants.
  - Tags are not temporal; a change applies to every revision's export. The write still opens a revision and is
    audited and revertible (M(git_cgallagher.inventory.revert)).
options:
  name:
    description: Tag name, for example C(patch_wave_1) or C(gpu-canary).
    type: str
    required: true
  description:
    description:
      - Free text. When omitted, an existing tag keeps its description (a new tag gets none).
    type: str
  paths:
    description:
      - Member node paths, for example C(all.commercial.prod.dc1.web). A group member brings every host under it.
      - Each must name a live node; a missing one fails the task (404) and changes nothing.
      - With O(exclusive=false) (the default) they are added to the current members.
    type: list
    elements: str
  exclusive:
    description:
      - When true, the members become exactly O(paths) (an empty or omitted O(paths) removes every member). When
        false, O(paths) are added and the other members stay.
    type: bool
    default: false
  remove_paths:
    description:
      - Member paths to take out of the tag. A path that is not a member is ignored.
      - Not with O(exclusive=true) (O(paths) then says everything), and not a path that is also in O(paths).
    type: list
    elements: str
  state:
    description:
      - V(present) creates or converges the tag; V(absent) deletes it with all its memberships (O(paths),
        O(remove_paths) and O(exclusive) do not apply).
    type: str
    choices: [present, absent]
    default: present
extends_documentation_fragment:
  - git_cgallagher.inventory.api
  - git_cgallagher.inventory.api.write
"""

EXAMPLES = r"""
- name: Patch the canary hosts first
  git_cgallagher.inventory.tag:
    name: patch_wave_1
    description: first patch wave
    paths:
      - all.commercial.prod.dc1.web
      - all.ops-tools.ops-bastion01

- name: Add one more member (the others stay)
  git_cgallagher.inventory.tag:
    name: patch_wave_1
    paths: [all.commercial.prod.dc2.web]

- name: Exactly these members
  git_cgallagher.inventory.tag:
    name: patch_wave_1
    paths: [all.commercial.prod.dc1.web.web01]
    exclusive: true

- name: Take a member out
  git_cgallagher.inventory.tag:
    name: patch_wave_1
    remove_paths: [all.ops-tools.ops-bastion01]

- name: A tag named after the node it tracks (emitted as group cluster_a)
  git_cgallagher.inventory.tag:
    name: cluster-a
    paths: [all.commercial.prod.dc1.cluster-a]

- name: Delete the tag
  git_cgallagher.inventory.tag:
    name: patch_wave_1
    state: absent
"""

RETURN = r"""
tag:
  description: The tag as stored (C(name), C(description), C(members) sorted); empty when absent.
  returned: always
  type: dict
group:
  description: The inventory group the tag is emitted as (C(-) turned into C(_)).
  returned: always
  type: str
  sample: gpu_canary
added:
  description: Member paths the task added (or would add, in check mode).
  returned: always
  type: list
  elements: str
removed:
  description: Member paths the task removed (or would remove); every member when the tag is deleted.
  returned: always
  type: list
  elements: str
impact:
  description:
    - In check mode, what the change would do to the hosts, from the server's dry run (C(dry_run=true)), which also
      runs every check the write would and refuses what the write would refuse.
    - C(total) distinct hosts affected; C(regrouped) counts the hosts that join or leave the tag's group; C(hosts) lists
      them sorted by path (at most C(limit), C(truncated) says when there were more) with the groups each C(joined)
      and C(left); C(groups) counts the hosts that join or leave each group. With C(--diff) a short summary is shown.
  returned: check mode, when changed
  type: dict
revision:
  description: Inventory revision after the task.
  returned: always
  type: int
"""

import re

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.git_cgallagher.inventory.plugins.module_utils.client import Client, with_impact, write_argument_spec

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,255}$")  # the server's rule (ais.models.NAME_RE); it decides


def read_tag(client, name):
    status, data = client.get("/tags/%s" % name)
    return None if status == 404 else data


def wanted_tag(p, current):
    """The tag the task wants, given the tag as it is now (None: missing)."""
    members = set(current["members"]) if current else set()
    if p["exclusive"]:
        members = set(p["paths"] or [])
    else:
        members |= set(p["paths"] or [])
    members -= set(p["remove_paths"] or [])
    description = p["description"] if p["description"] is not None else (current or {}).get("description")
    return {"name": p["name"], "description": description, "members": sorted(members)}


def main():
    spec = write_argument_spec()
    spec.update(
        name=dict(type="str", required=True),
        description=dict(type="str"),
        paths=dict(type="list", elements="str"),
        exclusive=dict(type="bool", default=False),
        remove_paths=dict(type="list", elements="str"),
        state=dict(type="str", choices=["present", "absent"], default="present"),
    )
    module = AnsibleModule(argument_spec=spec, supports_check_mode=True)
    p = module.params
    name = p["name"]
    if not NAME_RE.match(name):
        module.fail_json(msg="ais: invalid tag name %r: letters, digits, '_' and '-' only (max 255)" % name)
    if p["state"] == "absent" and (p["paths"] or p["remove_paths"] or p["exclusive"]):
        module.fail_json(msg="ais: state=absent deletes the whole tag; paths, remove_paths and exclusive do not apply")
    if p["exclusive"] and p["remove_paths"]:
        module.fail_json(msg="ais: remove_paths does not go with exclusive=true (paths is then the whole member set)")
    both = sorted(set(p["paths"] or []) & set(p["remove_paths"] or []))
    if both:
        module.fail_json(msg="ais: %s is in both paths and remove_paths" % ", ".join(both))
    client = Client(module)
    group = re.sub(r"[^A-Za-z0-9_]", "_", name)

    if p["state"] == "absent":
        for attempt in (1, 2):
            current = read_tag(client, name)
            if current is None:
                module.exit_json(changed=False, tag={}, group=group, added=[], removed=[], revision=client.current_revision())
            diff = {"before": current, "after": {}}
            if module.check_mode:
                data = client.preview("DELETE", "/tags/%s" % name)
                module.exit_json(
                    changed=True, tag={}, group=group, added=[], removed=current["members"],
                    diff=with_impact(diff, data.get("impact")), impact=data.get("impact"), revision=data["revision"],
                )
            done = client.write_or_stale("DELETE", "/tags/%s" % name) if attempt == 1 else client.write("DELETE", "/tags/%s" % name)
            if done is not None:
                module.exit_json(changed=True, tag={}, group=group, added=[], removed=current["members"], diff=diff, revision=client.revision)

    for attempt in (1, 2):
        current = read_tag(client, name)
        wanted = wanted_tag(p, current)
        before_members = set(current["members"]) if current else set()
        added = sorted(set(wanted["members"]) - before_members)
        removed = sorted(before_members - set(wanted["members"]))
        if current == wanted:
            module.exit_json(changed=False, tag=current, group=group, added=[], removed=[], revision=client.current_revision())
        diff = {"before": current or {}, "after": wanted}
        body = {"description": wanted["description"], "members": wanted["members"]}
        if module.check_mode:
            data = client.preview("PUT", "/tags/%s" % name, body)
            module.exit_json(
                changed=True, tag=wanted, group=group, added=added, removed=removed,
                diff=with_impact(diff, data.get("impact")), impact=data.get("impact"), revision=data["revision"],
            )
        data = client.write_or_stale("PUT", "/tags/%s" % name, body) if attempt == 1 else client.write("PUT", "/tags/%s" % name, body)
        if data is not None:
            if not data.get("changed", True):  # the server found nothing to do (someone else just did it)
                module.exit_json(changed=False, tag=data["tag"], group=group, added=[], removed=[], revision=data["revision"])
            module.exit_json(changed=True, tag=data["tag"], group=group, added=added, removed=removed, diff=diff, revision=client.revision)


if __name__ == "__main__":
    main()

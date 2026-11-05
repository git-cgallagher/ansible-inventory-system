#!/usr/bin/python
# -*- coding: utf-8 -*-
# Copyright (c) 2026 Chris Gallagher
# MIT License (see LICENSE)
from __future__ import annotations

DOCUMENTATION = r"""
module: variable_info
short_description: Find where a variable is set and which hosts have which value
version_added: "0.4.0"
author: Chris Gallagher (@git-cgallagher)
description:
  - Calls C(GET /api/v1/vars) for the nodes that set O(key) in their own variables, and with O(hosts=true)
    also C(GET /api/v1/vars/hosts) for every host's resolved value of it (what a playbook sees), with the
    node it comes from.
  - With O(value), only matching nodes and hosts. A node matches on its own value; a host on its whole
    resolved value, which for a C(merge), C(append) or C(unique_append) variable is the folded result of
    every contribution down its path, not one node's part.
  - Read-only; the API's viewer role is enough.
options:
  key:
    description: The variable name, for example C(gpu_driver).
    type: str
    required: true
  value:
    description:
      - Only this value, compared as JSON with its type, so the string C("550.90") and the number C(550.90)
        are different values. Omit it for every value.
    type: raw
  subtree:
    description:
      - Only this node and what is below it, for example C(all.commercial.prod). Ancestors of it that set
        the key are listed too (with C(above_subtree) in RV(nodes)), since the subtree's hosts inherit from them.
    type: str
  revision:
    description: As of this inventory revision (default the current one).
    type: int
  hosts:
    description: Also list the hosts with their resolved value (RV(hosts)).
    type: bool
    default: false
  limit:
    description:
      - At most this many nodes, and at most this many hosts, are returned (paged by path). The counts
        always cover the whole scope; RV(truncated) says whether anything was left out. Narrow a large
        search with O(subtree) rather than raising this.
    type: int
    default: 1000
extends_documentation_fragment:
  - git_cgallagher.inventory.api
"""

EXAMPLES = r"""
- name: Where is gpu_driver set?
  git_cgallagher.inventory.variable_info:
    key: gpu_driver
  register: where

- name: Which hosts under prod still have driver 550.90?
  git_cgallagher.inventory.variable_info:
    key: gpu_driver
    value: "550.90"
    subtree: all.commercial.prod
    hosts: true
  register: old_driver

- ansible.builtin.debug:
    msg: "{{ old_driver.hosts | map(attribute='host') | list }} ({{ old_driver.value_counts }})"
"""

RETURN = r"""
revision:
  description: The revision searched.
  returned: always
  type: int
strategy:
  description: The key's merge strategy (C(replace) when it is not declared).
  returned: always
  type: str
declared:
  description: Whether the key has a definition (C(/variables)).
  returned: always
  type: bool
nodes:
  description:
    - The nodes that set the key themselves, by path. Each has C(id), C(path), C(kind), C(value) (its own
      value), C(effective) for a fold strategy (the cumulative value it emits) and, with O(subtree),
      C(above_subtree).
  returned: always
  type: list
  elements: dict
nodes_total:
  description: How many nodes match, before O(limit).
  returned: always
  type: int
hosts:
  description:
    - Hosts whose resolved value matches (every host that has the key without O(value)), by path. Each has
      C(host), C(path), C(value), C(from) (the deepest node that sets it) and, for a fold strategy,
      C(sources) (every contributing node, root first).
  returned: when O(hosts=true)
  type: list
  elements: dict
hosts_total:
  description: How many hosts match, before O(limit).
  returned: when O(hosts=true)
  type: int
with_key:
  description: Hosts in scope that have the key at all.
  returned: when O(hosts=true)
  type: int
without_key:
  description: Hosts in scope that do not have the key.
  returned: when O(hosts=true)
  type: int
value_counts:
  description:
    - The distribution of resolved values among the hosts that have the key, most common first
      (C(value), C(hosts)); the 50 most common. The API calls it C(values), a name Jinja would resolve to
      the dict method.
  returned: when O(hosts=true)
  type: list
  elements: dict
truncated:
  description: Whether O(limit) left nodes or hosts out.
  returned: always
  type: bool
"""

import json
from urllib.parse import urlencode

from ansible.module_utils.basic import AnsibleModule

from ansible_collections.git_cgallagher.inventory.plugins.module_utils.client import Client, client_argument_spec

PAGE = 1000  # the server's maximum page


def fetch(client, route, params, items, limit):
    """Page through ``route`` until ``limit`` items or the end; returns (first page doc, items)."""
    out, first, offset = [], None, 0
    while True:
        query = dict(params, limit=min(PAGE, limit - len(out)), offset=offset)
        _status, doc = client.request("GET", "%s?%s" % (route, urlencode(query)), allow=())
        first = first or doc
        page = doc[items]
        out.extend(page)
        offset += len(page)
        if not page or len(out) >= limit or offset >= doc["total"]:
            return first, out


def main():
    spec = client_argument_spec()
    spec.update(
        key=dict(type="str", required=True, no_log=False),
        value=dict(type="raw"),
        subtree=dict(type="str"),
        revision=dict(type="int"),
        hosts=dict(type="bool", default=False),
        limit=dict(type="int", default=1000),
    )
    module = AnsibleModule(argument_spec=spec, supports_check_mode=True)
    p = module.params
    if p["limit"] < 1:
        module.fail_json(msg="limit must be at least 1")
    client = Client(module)
    params = {"key": p["key"]}
    if p["value"] is not None:
        params["value"] = json.dumps(p["value"])
    if p["subtree"]:
        params["subtree"] = p["subtree"]
    if p["revision"] is not None:
        params["revision"] = p["revision"]

    doc, nodes = fetch(client, "/vars", params, "nodes", p["limit"])
    result = dict(
        changed=False,
        revision=doc["revision"],
        strategy=doc["strategy"],
        declared=doc["declared"],
        nodes=nodes,
        nodes_total=doc["total"],
        truncated=len(nodes) < doc["total"],
    )
    if p["hosts"]:
        # Pin the hosts to the revision the nodes were read at, so both lists agree.
        hdoc, hosts = fetch(client, "/vars/hosts", dict(params, revision=doc["revision"]), "hosts", p["limit"])
        result.update(
            hosts=hosts,
            hosts_total=hdoc["total"],
            with_key=hdoc["with_key"],
            without_key=hdoc["without_key"],
            value_counts=hdoc["values"],
            truncated=result["truncated"] or len(hosts) < hdoc["total"],
        )
    module.exit_json(**result)


if __name__ == "__main__":
    main()

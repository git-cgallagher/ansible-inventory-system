"""Generate a large synthetic inventory tree straight into the database.

Shape (defaults, depth 8 counting ``all``): 4 businesses × 3 tiers × 5 datacenters, then
three nested ``group`` levels of 4 × 5 × 7 per datacenter, then hosts in the deepest groups:
60 datacenters, 9,840 groups, 9,916 non-host nodes and ~100k hosts (``--hosts``). Every
non-host node carries 3–4 variables, every host 2 (a tenth of them 3), and the fold
variables ``packages`` (unique_append) and ``sysctl`` (merge) contribute at several levels,
so exports exercise the fold path. One revision, written with bulk inserts and explicit ids
(the ORM importer would spend minutes on 100k hosts; this is a load generator, not an import
test). The database must hold no nodes yet; the schema is created or upgraded first.

``--profiles`` (issue 22) also creates two profiles: ``role-common`` attached to every
``roleN`` group (1,200 attachments over every host) and ``pool-tuning`` attached to every
``pool3`` group (a narrow one), with values of every strategy, some shadowed by the groups'
own values.

    AIS_DATABASE_URL=postgresql+psycopg://ais:ais@127.0.0.1:55434/ais python bench/generate.py --hosts 100000
"""

from __future__ import annotations

import argparse
import itertools
import os
import sys
import time

from sqlalchemy import func, insert, select, text

from ais.db import init_db, make_engine
from ais.models import MergeStrategy, Node, NodeKind, NodeVariable, Profile, ProfileAttachment, ProfileVariable, Revision, VariableDefinition

BUSINESSES, TIERS, DATACENTERS = 4, 3, 5
GROUPS = (4, 5, 7)  # nested group fan-out below each datacenter
BATCH = 5000


def build(hosts_wanted: int):
    """Yield (nodes, variables) as plain dicts with explicit ids, parents before children."""
    ids = itertools.count(1)
    nodes: list[dict] = []
    variables: list[dict] = []

    def node(parent: dict | None, name: str, kind: NodeKind) -> dict:
        n = {
            "id": next(ids),
            "parent_id": parent["id"] if parent else None,
            "kind": kind.value,
            "name": name,
            "inventory_name": None,
            "path": f"{parent['path']}.{name}" if parent else name,
            "depth": parent["depth"] + 1 if parent else 0,
            "meta": {},
        }
        nodes.append(n)
        return n

    def var(n: dict, key: str, value) -> None:
        variables.append({"node_id": n["id"], "key": key, "value": value})

    root = node(None, "all", NodeKind.all)
    var(root, "ntp_server", "time.example.com")
    var(root, "packages", ["vim", "curl"])
    var(root, "sysctl", {"vm.swappiness": 60})
    leaves: list[dict] = []
    for b in range(BUSINESSES):
        biz = node(root, f"b{b}", NodeKind.business)
        var(biz, "owner_team", f"team-{b}")
        var(biz, "cost_center", 1000 + b)
        var(biz, "packages", [f"agent-b{b}"])
        for t, tier_name in enumerate(["prod", "stage", "dev"][:TIERS]):
            tier = node(biz, tier_name, NodeKind.tier)
            var(tier, "log_level", ["warning", "info", "debug"][t])
            var(tier, "sysctl", {"vm.swappiness": 10 * (t + 1)})
            var(tier, "packages", [f"{tier_name}-tools"])
            for d in range(DATACENTERS):
                dc = node(tier, f"dc{d}", NodeKind.datacenter)
                var(dc, "dns_servers", [f"10.{b}.{d}.2", f"10.{b}.{d}.3"])
                var(dc, "site_code", f"B{b}{tier_name[0].upper()}D{d}")
                var(dc, "ntp_server", f"ntp.dc{d}.example.com")
                for g1 in range(GROUPS[0]):
                    grp1 = node(dc, f"svc{g1}", NodeKind.group)
                    var(grp1, "service", f"svc{g1}")
                    var(grp1, "packages", [f"svc{g1}-runtime"])
                    var(grp1, "app_port", 8000 + g1)
                    for g2 in range(GROUPS[1]):
                        grp2 = node(grp1, f"role{g2}", NodeKind.group)
                        var(grp2, "role_name", f"role{g2}")
                        var(grp2, "sysctl", {f"net.core.somaxconn{g2}": 1024})
                        var(grp2, "app_port", 9000 + g2)
                        for g3 in range(GROUPS[2]):
                            grp3 = node(grp2, f"pool{g3}", NodeKind.group)
                            var(grp3, "pool", g3)
                            var(grp3, "packages", [f"pool{g3}-extras"])
                            var(grp3, "max_conns", 100 * (g3 + 1))
                            leaves.append(grp3)
    per_leaf, extra = divmod(hosts_wanted, len(leaves))
    h = 0
    for i, leaf in enumerate(leaves):
        for _ in range(per_leaf + (1 if i < extra else 0)):
            host = node(leaf, f"h{h:06d}", NodeKind.host)
            var(host, "ansible_host", f"10.{(h >> 16) & 255}.{(h >> 8) & 255}.{h & 255}")
            var(host, "host_id", h)
            if h % 10 == 0:
                var(host, "packages", ["tcpdump"])
            h += 1
    return nodes, variables


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--hosts", type=int, default=100_000)
    p.add_argument("--database-url", default=os.environ.get("AIS_DATABASE_URL"))
    p.add_argument("--profiles", action="store_true", help="also attach profiles widely (issue 22)")
    args = p.parse_args(argv)
    if not args.database_url:
        p.error("set AIS_DATABASE_URL or --database-url")

    engine = make_engine(args.database_url)
    init_db(engine)
    t0 = time.perf_counter()
    nodes, variables = build(args.hosts)
    with engine.begin() as conn:
        if conn.scalar(select(func.count()).select_from(Node)):
            print("the database already holds nodes; generate into an empty one", file=sys.stderr)
            return 1
        rev = conn.execute(insert(Revision).values(actor="bench", source="import", message=f"synthetic tree, {args.hosts} hosts")).inserted_primary_key[0]
        conn.execute(
            insert(VariableDefinition),
            [
                {"name": "packages", "merge_strategy": MergeStrategy.unique_append, "description": "accumulates down the tree", "json_schema": None},
                {"name": "sysctl", "merge_strategy": MergeStrategy.merge, "description": "merged key by key", "json_schema": None},
            ],
        )
        for i in range(0, len(nodes), BATCH):
            conn.execute(insert(Node), [dict(n, created_rev=rev) for n in nodes[i : i + BATCH]])
        for i in range(0, len(variables), BATCH):
            conn.execute(insert(NodeVariable), [dict(v, created_rev=rev) for v in variables[i : i + BATCH]])
        attached = 0
        if args.profiles:
            profiles = {
                "role-common": {"kernel": "6.8.0", "sysctl": {"kernel.pid_max": 4194304}, "packages": ["role-agent"], "app_port": 9100, "monitoring": {"scrape": True}},
                "pool-tuning": {"max_conns": 5000, "sysctl": {"net.ipv4.tcp_tw_reuse": 1}, "tuned": True},
            }
            for name, values in profiles.items():
                conn.execute(insert(Profile).values(name=name, description=f"bench profile {name}", created_rev=rev))
                conn.execute(insert(ProfileVariable), [{"profile": name, "key": k, "value": v, "created_rev": rev} for k, v in values.items()])
            rows = [{"node_id": n["id"], "profile": "role-common", "position": 0, "created_rev": rev} for n in nodes if n["name"].startswith("role")]
            rows += [{"node_id": n["id"], "profile": "pool-tuning", "position": 0, "created_rev": rev} for n in nodes if n["name"] == "pool3"]
            for i in range(0, len(rows), BATCH):
                conn.execute(insert(ProfileAttachment), rows[i : i + BATCH])
            attached = len(rows)
        if conn.dialect.name == "postgresql":  # explicit ids: move the sequence past them
            conn.execute(text("SELECT setval(pg_get_serial_sequence('node', 'id'), (SELECT max(id) FROM node))"))
            conn.execute(text("ANALYZE"))
    hosts = sum(1 for n in nodes if n["kind"] == "host")
    print(
        f"revision {rev}: {len(nodes)} nodes ({hosts} hosts, {len(nodes) - hosts} non-host, depth {max(n['depth'] for n in nodes) + 1}), "
        f"{len(variables)} variables, {attached} profile attachments in {time.perf_counter() - t0:.1f} s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Revert one revision, or every revision of a change id, as one new revision
(``POST /api/v1/revisions/{id}/revert``, ``POST /api/v1/changes/{change}/revert``,
``ais revert``).

A revision's audit rows say what it did; ``revert`` applies the inverse of each row, newest
first, under one new revision, and records one ordinary audit row per inverse operation
(``variable.set``, ``node.move``, ``node.restore``, …, the same shapes the write routes
record) plus one ``revert`` summary row. Because the rows are ordinary, history by node and
key (``audit.node_lineage``) follows the revert, and reverting a revert is just another
revert (the summary row inverts to nothing).

What each kind's row records, and its inverse (all exact unless noted):

* ``variable.set`` (``before``/``after`` = ``{path, key, value}``, ``before`` None for a new
  key) → set the old value back, or unset the key.
* ``variable.unset`` (``before`` = the value) → set it again.
* ``node.create`` (``after`` = the node) → delete it (it must still be a leaf).
* ``node.update`` (``before`` = the node with ``inventory_name`` and ``meta``) → put them back.
* ``node.move`` (``before`` = the old root, ``after`` = the new one) → move it back (values,
  ``meta`` and tag memberships travel with it; new ids, mapped in the new ``node.move`` row).
* ``node.delete`` (``before.path``) → restore the subtree as it was at the newest revision
  before the delete (``ais.restore``; new ids, mapped in the ``node.restore`` row).
* ``node.restore`` (``after.restored``) → delete the restored subtree (it must hold nothing
  else). The restore only added memberships of the restored nodes and never changes
  definitions or tag descriptions (it exports today's), so nothing else needs undoing.
* ``definition.set`` / ``definition.delete`` (``before``/``after`` = the declaration) →
  declare it as before, or undeclare it.
* ``tag.set`` / ``tag.delete`` (``before``/``after`` = name, description, live member
  paths) → converge it back, or delete it. A ``tag.delete`` row also records the memberships
  of deleted nodes (``deleted_members``) since this release; an older row cannot bring those
  back (they never affect the current tree, only a later restore), and the operation says so.
* ``profile.create`` / ``profile.update`` / ``profile.delete`` (``before``/``after`` = name,
  description, vars; a delete's ``before`` also has ``attached``, every node's list before) →
  converge the profile back (and those lists), or delete it (it must not be attached then).
* ``profile.attach`` / ``profile.detach`` / ``node.profiles`` (``before``/``after`` = the
  node's path and ordered profile list) → converge the list back.
* ``import`` → nodes and values are temporal rows: close the nodes the import created, put
  back the values it replaced (rows closed under its revision) and unset the ones it added.
  Profiles and attachments are temporal too: profiles go back to what they were just before
  the import (or away), node lists likewise. Definitions and tags are not temporal: since
  this release the row's ``before`` records them as they were; an older import row that changed any is refused (422), never guessed.
* ``revert`` (the summary row) → nothing.

**Conflicts.** Each row has a footprint (node paths, ``path``/``key`` pairs, definition and
tag names). If a later revision that is not being reverted touched any of it, the revert is
refused with 409 and the list, unless ``force``; reverting a set newest first means its own
revisions never conflict with each other. ``force`` skips that check only: an inverse that
cannot apply (the node is gone, the path is taken, a value its schema now rejects) is
refused either way, with what blocked it.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ais import audit, impact, schema
from ais import profiles as profile_lib
from ais.audit import current_change
from ais.importer import ImportError_, live_node_by_path
from ais.models import PATH_SEP, AuditLog, Node, NodeKind, NodeVariable, Profile, ProfileAttachment, ProfileVariable, Revision, Tag
from ais.restore import RestoreError, restore_subtree

SUMMARY_ACTION = "revert"


class RevertError(Exception):
    """A revert that cannot run. ``status`` is the HTTP status; ``detail`` the body's detail
    (a dict with ``error`` and ``message``)."""

    def __init__(self, status: int, detail: dict[str, Any]):
        super().__init__(detail.get("message", ""))
        self.status = status
        self.detail = detail


# ------------------------------------------------------------------ footprints

# A footprint entry: ("var", path, key) | ("node", path) | ("tree", path) | ("member", path)
# | ("profiles", path) (a node's profile list) | ("definition", name) | ("tag", name) |
# ("profile", name). "tree" covers the node at path and everything below.
Touch = tuple


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root + PATH_SEP)


def _overlap(a: Touch, b: Touch) -> bool:
    if a[0] in ("definition", "tag", "profile") or b[0] in ("definition", "tag", "profile"):
        return a == b
    if a[0] == "tree" and b[0] == "tree":
        return _within(a[1], b[1]) or _within(b[1], a[1])
    if a[0] == "tree":
        return _within(b[1], a[1])
    if b[0] == "tree":
        return _within(a[1], b[1])
    if a[0] == "var" and b[0] == "var":
        return a == b
    if a[0] == "node" and b[0] == "node":
        return a[1] == b[1]
    if a[0] == "profiles" and b[0] == "profiles":
        return a[1] == b[1]
    return False


def _specific(a: Touch, b: Touch) -> Touch:
    """Of two overlapping touches, the narrower one (what the message should name)."""
    if a[0] == "tree" and b[0] != "tree":
        return b
    if b[0] == "tree" and a[0] != "tree":
        return a
    if a[0] == "tree" and b[0] == "tree":
        return a if len(a[1]) >= len(b[1]) else b
    return a


def _describe_touch(t: Touch) -> str:
    if t[0] == "var":
        return f"{t[2]} on {t[1]}"
    if t[0] == "tree":
        return f"{t[1]} and below"
    if t[0] == "member":
        return f"tag member {t[1]}"
    if t[0] == "profiles":
        return f"the profiles of {t[1]}"
    return f"{t[0]} {t[1]}"


@dataclass
class _ImportFacts:
    """What an ``import`` revision did to temporal rows, read from them."""

    created: dict[int, str] = field(default_factory=dict)  # node id -> path
    roots: list[str] = field(default_factory=list)  # created subtrees' roots, shallowest first
    replaced: dict[tuple[str, str], Any] = field(default_factory=dict)  # (path, key) -> old value (rows closed by the import)
    added: set[tuple[str, str]] = field(default_factory=set)  # (path, key) set on a node that existed before
    profiles: set[str] = field(default_factory=set)  # profiles created or changed
    lists: dict[str, list[str]] = field(default_factory=dict)  # path -> profile list before, nodes that existed before


def _import_facts(session: Session, revision: int) -> _ImportFacts:
    facts = _ImportFacts()
    for n in session.scalars(select(Node).where(Node.created_rev == revision)):
        facts.created[n.id] = n.path
    parents = {n.id: n.parent_id for n in session.scalars(select(Node).where(Node.created_rev == revision))}
    facts.roots = sorted((p for i, p in facts.created.items() if parents.get(i) not in facts.created), key=lambda p: (p.count(PATH_SEP), p))
    rows = session.execute(
        select(NodeVariable.key, NodeVariable.value, NodeVariable.created_rev, NodeVariable.deleted_rev, Node.path, Node.id)
        .join(Node, Node.id == NodeVariable.node_id)
        .where((NodeVariable.created_rev == revision) | (NodeVariable.deleted_rev == revision))
    )
    for key, value, created, deleted, path, node_id in rows:
        if node_id in facts.created:
            continue  # goes with the node
        if deleted == revision:
            facts.replaced[(path, key)] = value
        if created == revision:
            facts.added.add((path, key))
    changed = (ProfileVariable.created_rev == revision) | (ProfileVariable.deleted_rev == revision)
    facts.profiles.update(session.scalars(select(ProfileVariable.profile).where(changed).distinct()))
    facts.profiles.update(session.scalars(select(Profile.name).where((Profile.created_rev == revision) | (Profile.deleted_rev == revision)).distinct()))
    att = session.execute(
        select(Node.path, Node.id)
        .join(ProfileAttachment, ProfileAttachment.node_id == Node.id)
        .where((ProfileAttachment.created_rev == revision) | (ProfileAttachment.deleted_rev == revision))
        .distinct()
    )
    for path, node_id in att:
        if node_id not in facts.created:
            facts.lists[path] = profile_lib.node_list(session, node_id, revision - 1)
    return facts


def footprint(session: Session, row: AuditLog, cache: dict[int, _ImportFacts] | None = None) -> list[Touch]:
    """What ``row`` touched, for the conflict check."""
    b, a = row.before or {}, row.after or {}
    act = row.action
    if act in ("variable.set", "variable.unset"):
        doc = a or b
        return [("var", doc.get("path"), doc.get("key"))]
    if act == "node.create":
        return [("tree", a.get("path"))]
    if act == "node.update":
        return [("node", b.get("path"))]
    if act == "node.move":
        return [("tree", b.get("path")), ("tree", a.get("path"))]
    if act in ("node.delete", "node.restore"):
        return [("tree", b.get("path"))]
    if act in ("definition.set", "definition.delete"):
        return [("definition", row.target_id)]
    if act in ("tag.set", "tag.delete"):
        members = set((b or {}).get("members") or []) | set((a or {}).get("members") or [])
        return [("tag", row.target_id)] + [("member", m) for m in sorted(members)]
    if act == "import":
        if cache is not None and row.revision in cache:
            facts = cache[row.revision]
        else:
            facts = _import_facts(session, row.revision)  # type: ignore[arg-type]
            if cache is not None:
                cache[row.revision] = facts  # type: ignore[index]
        out: list[Touch] = [("tree", p) for p in facts.roots]
        out += [("var", p, k) for p, k in sorted(set(facts.replaced) | facts.added)]
        out += [("definition", n) for n in a.get("definitions") or []]
        out += [("tag", n) for n in a.get("tags") or []]
        out += [("profile", n) for n in sorted(facts.profiles)]
        out += [("profiles", p) for p in sorted(facts.lists)]
        return out
    if act in ("profile.create", "profile.update"):
        return [("profile", row.target_id)]
    if act == "profile.delete":
        return [("profile", row.target_id)] + [("profiles", x.get("path")) for x in (b or {}).get("attached") or []]
    if act in LIST_ACTIONS:
        return [("profiles", (a or b).get("path"))]
    if act == SUMMARY_ACTION:
        return []
    return [("unknown", row.action)]


# ----------------------------------------------------------------------- plan


def _rows_of(session: Session, revisions: list[int]) -> list[AuditLog]:
    return list(session.scalars(select(AuditLog).where(AuditLog.revision.in_(revisions)).order_by(AuditLog.revision.desc(), AuditLog.id.desc())))


LIST_ACTIONS = ("profile.attach", "profile.detach", "node.profiles")

INVERTIBLE = {
    "profile.create",
    "profile.update",
    "profile.delete",
    *LIST_ACTIONS,
    "variable.set",
    "variable.unset",
    "node.create",
    "node.update",
    "node.move",
    "node.delete",
    "node.restore",
    "definition.set",
    "definition.delete",
    "tag.set",
    "tag.delete",
    "import",
    SUMMARY_ACTION,
}


def _check_invertible(session: Session, revisions: list[int], rows: list[AuditLog]) -> None:
    by_rev: dict[int, list[AuditLog]] = {}
    for r in rows:
        by_rev.setdefault(r.revision, []).append(r)  # type: ignore[arg-type]
    problems: list[str] = []
    for rid in revisions:
        mine = by_rev.get(rid, [])
        if not mine:
            problems.append(
                f"revision {rid} has no audit rows, so there is nothing recorded to invert "
                "(it was written outside the API and the CLI, such as by a script calling the importer directly)"
            )
        for r in mine:
            if r.action not in INVERTIBLE:
                problems.append(f"revision {rid}: cannot revert a {r.action} row")
            elif r.action == "import" and r.before is None and ((r.after or {}).get("definitions") or (r.after or {}).get("tags")):
                changed = ", ".join(
                    f"{kind} {', '.join((r.after or {}).get(kind) or [])}" for kind in ("definitions", "tags") if (r.after or {}).get(kind)
                )
                problems.append(
                    f"revision {rid} is an import recorded before revert existed: it changed {changed}, and its audit row "
                    "does not say what they were before (definitions and tags are not temporal), so it cannot be reverted "
                    "exactly. Put those back by hand (PUT /variables, PUT /tags), or revert its other changes with an import"
                )
    if problems:
        raise RevertError(422, {"error": "cannot revert", "message": "; ".join(problems), "problems": problems})


def find_conflicts(session: Session, revisions: list[int], rows: list[AuditLog]) -> list[dict[str, Any]]:
    """Rows of later revisions (not in ``revisions``) whose footprint overlaps a reverted
    row's. A later revision that is a revert of one of ``revisions`` is flagged
    ``already_reverted``."""
    chosen = set(revisions)
    cache: dict[int, _ImportFacts] = {}
    later = [
        r
        for r in session.scalars(select(AuditLog).where(AuditLog.revision > min(revisions)).order_by(AuditLog.revision, AuditLog.id))
        if r.revision not in chosen
    ]
    reverted_by: dict[int, set[int]] = {}
    for r in later:
        if r.action == SUMMARY_ACTION:
            for rid in (r.after or {}).get("reverted") or []:
                reverted_by.setdefault(int(rid), set()).add(r.revision)  # type: ignore[arg-type]
    mine = [(r, footprint(session, r, cache)) for r in rows if r.action != SUMMARY_ACTION]
    out: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for lr in later:
        if lr.action == SUMMARY_ACTION:
            continue
        lt = footprint(session, lr, cache)
        for r, rt in mine:
            if lr.revision <= r.revision or (lr.id, r.revision) in seen:  # type: ignore[operator]
                continue
            hit = next(((a, b) for a in rt for b in lt if _overlap(a, b)), None)
            if hit is None:
                continue
            seen.add((lr.id, r.revision))  # type: ignore[arg-type]
            out.append(
                {
                    "revision": lr.revision,
                    "action": lr.action,
                    "target": f"{lr.target_type} {lr.target_id}",
                    "actor": lr.actor,
                    "ts": lr.ts.isoformat() if lr.ts else None,
                    "change": lr.change,
                    "reverting": r.revision,
                    "reverting_action": r.action,
                    "on": _describe_touch(_specific(*hit)),
                    "already_reverted": lr.revision in reverted_by.get(r.revision, set()),  # type: ignore[arg-type]
                }
            )
    return out


def _conflict_error(revisions: list[int], conflicts: list[dict[str, Any]]) -> RevertError:
    if all(c["already_reverted"] for c in conflicts):
        by = sorted({c["revision"] for c in conflicts})
        return RevertError(
            409,
            {
                "error": "already reverted",
                "message": f"{_label(revisions)} was already reverted by revision {', '.join(map(str, by))}",
                "reverted_by": by,
                "conflicts": conflicts,
            },
        )
    shown = "; ".join(
        f"revision {c['revision']} ({c['action']} {c['target']} by {c['actor']}) changed {c['on']}, which revision {c['reverting']} changed"
        for c in conflicts[:10]
    )
    more = f" and {len(conflicts) - 10} more" if len(conflicts) > 10 else ""
    return RevertError(
        409,
        {
            "error": "conflict",
            "message": (
                f"cannot revert {_label(revisions)}: later revisions changed the same things: {shown}{more}. "
                "Revert those first (newest first), or send force=true to revert over them"
            ),
            "conflicts": conflicts,
        },
    )


def _label(revisions: list[int]) -> str:
    return f"revision {revisions[0]}" if len(revisions) == 1 else f"revisions {', '.join(map(str, revisions))}"


# ---------------------------------------------------------------------- apply


@dataclass
class _Ctx:
    session: Session
    rev: Revision
    actor: str
    source: str
    operations: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    keys: set[str] = field(default_factory=set)
    reverting: int = 0

    def emit(self, action: str, target_type: str, target_id: Any, before: Any, after: Any, summary: str, keys: list[str] | None = None) -> None:
        row = audit.record(
            self.session,
            actor=self.actor,
            action=action,
            target_type=target_type,
            target_id=target_id,
            before=before,
            after=after,
            source=self.source,
            revision=self.rev.id,
            keys=keys,
        )
        self.operations.append(
            {"reverts": self.reverting, "action": action, "target_type": target_type, "target_id": row.target_id, "summary": summary, "before": row.before, "after": row.after}
        )


def _blocked(ctx: _Ctx, row: AuditLog, message: str, status: int = 409) -> RevertError:
    return RevertError(
        status,
        {
            "error": "cannot apply",
            "message": f"cannot revert revision {row.revision} ({row.action} {row.target_type} {row.target_id}): {message}",
            "revision": row.revision,
        },
    )


def _live(ctx: _Ctx, row: AuditLog, path: str) -> Node:
    node = live_node_by_path(ctx.session, path)
    if node is None:
        raise _blocked(ctx, row, f"{path} does not exist now")
    return node


def _http(ctx: _Ctx, row: AuditLog, exc: HTTPException) -> RevertError:
    detail = exc.detail
    text = detail.get("message", str(detail)) if isinstance(detail, dict) else str(detail)
    return _blocked(ctx, row, text, 409 if exc.status_code == 404 else exc.status_code)


def _set_var(ctx: _Ctx, row: AuditLog, path: str, key: str, value: Any, absent: bool = False) -> None:
    from ais.api_write import _set_var as set_row, _var_target

    node = _live(ctx, row, path)
    current = ctx.session.scalar(
        select(NodeVariable).where(NodeVariable.node_id == node.id, NodeVariable.key == key, NodeVariable.deleted_rev.is_(None))
    )
    ctx.keys.add(key)
    if absent:
        if current is None:
            return
        current.deleted_rev = ctx.rev.id
        ctx.session.flush()
        ctx.emit("variable.unset", "node_variable", _var_target(node, key), {"path": path, "key": key, "value": current.value}, None, f"unset {key} on {path}")
        return
    if current is not None and current.value == value:
        return
    before = {"path": path, "key": key, "value": current.value} if current is not None else None
    set_row(ctx.session, node, key, copy.deepcopy(value), ctx.rev)
    ctx.session.flush()
    ctx.emit("variable.set", "node_variable", _var_target(node, key), before, {"path": path, "key": key, "value": value}, f"set {key} on {path} to {_short(value)}")


def _short(value: Any) -> str:
    text = json.dumps(value, default=str)
    return text if len(text) <= 60 else text[:57] + "..."


def _delete_exactly(ctx: _Ctx, row: AuditLog, path: str, expected_ids: set[int] | None, why: str) -> None:
    """Delete the live subtree at ``path``, which must hold only ``expected_ids`` (when given)
    or only its root."""
    from ais.api_write import delete_subtree, live_subtree

    node = _live(ctx, row, path)
    if node.kind is NodeKind.all:
        raise _blocked(ctx, row, "that would delete the root", 422)
    subtree = live_subtree(ctx.session, node)
    extra = [n.path for n in subtree if (n.id not in expected_ids if expected_ids is not None else n.id != node.id)]
    if extra:
        raise _blocked(
            ctx,
            row,
            f"{path} now holds {len(extra)} node(s) that {why} did not create ({', '.join(extra[:5])}{' …' if len(extra) > 5 else ''}); "
            "delete or move them first",
        )
    before = delete_subtree(ctx.session, ctx.rev, node, subtree)
    ctx.emit("node.delete", "node", node.id, before, None, f"delete {path}" + (f" ({len(subtree)} nodes)" if len(subtree) > 1 else ""))


def _restore(ctx: _Ctx, row: AuditLog, path: str) -> None:
    """Undo a delete: restore ``path`` as it was at the newest revision before ``row``'s."""
    from ais.api_write import live_vars, node_out

    session = ctx.session
    as_of = session.scalar(select(Revision.id).where(Revision.id < row.revision).order_by(Revision.id.desc()).limit(1))
    try:
        result = restore_subtree(session, path, as_of, actor=ctx.actor, source=ctx.source, rev=ctx.rev)
    except RestoreError as exc:
        raise _blocked(ctx, row, str(exc), exc.status) from exc
    except ImportError_ as exc:
        raise _blocked(ctx, row, str(exc), 422) from exc
    root = session.get(Node, result.restored[0]["id"])
    out = node_out(root, live_vars(session, root.id))  # type: ignore[arg-type]
    ctx.emit(
        "node.restore",
        "node",
        root.id,  # type: ignore[union-attr]
        {"path": result.path, "id": result.restored[0]["old_id"], "revision": result.from_revision},
        {**out, **result.summary(), "restored": result.restored},
        f"restore {path} as of revision {result.from_revision} ({len(result.restored)} nodes)",
    )


def _set_definition(ctx: _Ctx, row: AuditLog, name: str, wanted: dict[str, Any] | None) -> None:
    from ais.api_write import definition_out, set_definition
    from ais.models import VariableDefinition

    current = ctx.session.get(VariableDefinition, name)
    before = definition_out(current) if current is not None else None
    if before == wanted:
        return
    try:
        out = set_definition(ctx.session, name, wanted)
    except HTTPException as exc:
        raise _http(ctx, row, exc) from exc
    ctx.keys.add(name)
    if out is None:
        ctx.emit("definition.delete", "variable_definition", name, before, None, f"undeclare {name}")
    else:
        ctx.emit("definition.set", "variable_definition", name, before, out, f"declare {name} ({out['merge_strategy']})")


def _set_tag(ctx: _Ctx, row: AuditLog, name: str, wanted: dict[str, Any] | None) -> None:
    from ais.api_tags import converge_tag, remove_tag, tag_out

    session = ctx.session
    tag = session.scalar(select(Tag).where(Tag.name == name))
    before = tag_out(session, tag) if tag is not None else None
    if wanted is None:
        if tag is None:
            return
        gone = remove_tag(session, tag)
        ctx.emit("tag.delete", "tag", name, gone, None, f"delete tag {name}")
        return
    restore_ids = [int(m["id"]) for m in wanted.get("deleted_members") or []]
    plain = {"name": name, "description": wanted.get("description"), "members": sorted(wanted.get("members") or [])}
    if before == plain and not restore_ids:
        return
    try:
        out = converge_tag(session, name, plain["description"], plain["members"], restore_ids=restore_ids)
    except HTTPException as exc:
        raise _http(ctx, row, exc) from exc
    ctx.emit("tag.set", "tag", name, before, out, f"set tag {name} ({len(out['members'])} members)")


def _set_profile(ctx: _Ctx, row: AuditLog, name: str, wanted: dict[str, Any] | None) -> None:
    """Converge profile ``name`` to ``wanted`` (a doc) or delete it (None)."""
    before = profile_lib.doc(ctx.session, name)
    target = None if wanted is None else {"name": name, "description": wanted.get("description"), "vars": wanted.get("vars") or {}}
    if before == target or (before is not None and target is not None and before["description"] == target["description"] and not profile_lib.changed_keys(before, target)):
        return
    try:
        out = profile_lib.set_profile(ctx.session, ctx.rev, name, target, check=False)  # schema: check_live at the end
    except profile_lib.ProfileError as exc:
        raise _blocked(ctx, row, str(exc), 409 if exc.status == 404 else exc.status) from exc
    keys = profile_lib.changed_keys(before, out)
    ctx.keys.update(keys)
    if out is None:
        ctx.emit("profile.delete", "profile", name, {**before, "attached": []}, None, f"delete profile {name}", keys=keys)  # type: ignore[dict-item]
    else:
        ctx.emit("profile.create" if before is None else "profile.update", "profile", name, before, out, f"set profile {name}", keys=keys)


def _set_list(ctx: _Ctx, row: AuditLog, path: str, names: list[str]) -> None:
    """Converge the node at ``path``'s profile list to ``names``."""
    node = _live(ctx, row, path)
    try:
        before, after = profile_lib.set_node_profiles(ctx.session, ctx.rev, node, list(names))
    except profile_lib.ProfileError as exc:
        raise _blocked(ctx, row, str(exc), 409 if exc.status == 404 else exc.status) from exc
    if before == after:
        return
    ctx.emit(
        profile_lib.attach_action(before, after),
        "node",
        node.id,
        {"path": path, "profiles": before},
        {"path": path, "profiles": after},
        f"set the profiles of {path} to {', '.join(after) or '(none)'}",
    )


def _invert(ctx: _Ctx, row: AuditLog) -> None:
    from ais.api_write import live_vars, move_subtree, node_out

    session = ctx.session
    b, a = row.before, row.after
    act = row.action
    ctx.reverting = row.revision  # type: ignore[assignment]
    if act == "variable.set":
        if b is None:
            _set_var(ctx, row, a["path"], a["key"], None, absent=True)
        else:
            _set_var(ctx, row, b["path"], b["key"], b["value"])
    elif act == "variable.unset":
        _set_var(ctx, row, b["path"], b["key"], b["value"])
    elif act == "node.create":
        _delete_exactly(ctx, row, a["path"], None, f"revision {row.revision}")
    elif act == "node.update":
        node = _live(ctx, row, b["path"])
        was = node_out(node)
        node.inventory_name = b.get("inventory_name")
        node.meta = copy.deepcopy(b.get("meta") or {})
        session.flush()
        now = node_out(node)
        if now != was:
            ctx.emit("node.update", "node", node.id, was, now, f"put back inventory_name and meta of {b['path']}")
    elif act == "node.move":
        node = _live(ctx, row, a["path"])
        parent_path, _sep, name = b["path"].rpartition(PATH_SEP)
        parent = _live(ctx, row, parent_path)
        try:
            before, out, moved = move_subtree(session, ctx.rev, node, parent, name)
        except HTTPException as exc:
            raise _http(ctx, row, exc) from exc
        ctx.emit("node.move", "node", node.id, before, {**out, "moved": moved}, f"move {a['path']} back to {b['path']}")
    elif act == "node.delete":
        _restore(ctx, row, b["path"])
    elif act == "node.restore":
        ids = {int(m["id"]) for m in (a or {}).get("restored") or []}
        _delete_exactly(ctx, row, b["path"], ids, f"the restore in revision {row.revision}")
    elif act == "definition.set":
        _set_definition(ctx, row, row.target_id, b)
    elif act == "definition.delete":
        _set_definition(ctx, row, row.target_id, b)
    elif act == "tag.set":
        _set_tag(ctx, row, row.target_id, b)
    elif act == "tag.delete":
        if "deleted_members" not in b:
            ctx.notes.append(
                f"revision {row.revision} deleted tag {row.target_id} before deletes recorded the memberships of deleted nodes: "
                "if it had any, they stay lost (the current tree is exact; a later restore of such a node comes back without the tag)"
            )
        _set_tag(ctx, row, row.target_id, b)
    elif act in ("profile.create", "profile.update"):
        _set_profile(ctx, row, row.target_id, b)
    elif act == "profile.delete":
        _set_profile(ctx, row, row.target_id, b)
        for entry in b.get("attached") or []:
            _set_list(ctx, row, entry["path"], entry["profiles"])
    elif act in LIST_ACTIONS:
        _set_list(ctx, row, b["path"], b["profiles"])
    elif act == "import":
        facts = _import_facts(session, row.revision)  # type: ignore[arg-type]
        before = b or {}
        for name, was in sorted((before.get("tags") or {}).items()):
            _set_tag(ctx, row, name, was)
        for path, key in sorted(set(facts.replaced) | facts.added):
            if (path, key) in facts.replaced:
                _set_var(ctx, row, path, key, facts.replaced[(path, key)])
            else:
                _set_var(ctx, row, path, key, None, absent=True)
        for path, names in sorted(facts.lists.items()):
            _set_list(ctx, row, path, names)
        created = set(facts.created)
        for path in reversed(facts.roots):
            _delete_exactly(ctx, row, path, created, f"the import in revision {row.revision}")
        for name in sorted(facts.profiles):
            _set_profile(ctx, row, name, profile_lib.doc(session, name, row.revision - 1))  # type: ignore[operator]
        for name, was in sorted((before.get("definitions") or {}).items()):
            _set_definition(ctx, row, name, was)
    elif act == SUMMARY_ACTION:
        return
    else:  # pragma: no cover - _check_invertible refuses these first
        raise _blocked(ctx, row, f"cannot revert a {act} row", 422)


# -------------------------------------------------------------------- entry


@dataclass
class RevertResult:
    revision: Revision
    reverted: list[int]
    operations: list[dict[str, Any]]
    conflicts: list[dict[str, Any]]
    notes: list[str]
    impact: dict[str, Any] | None = None  # the dry run's preview (``ais.impact``)

    @property
    def hosts(self) -> list[dict[str, Any]] | None:
        return None if self.impact is None else self.impact["hosts"]

    def body(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "reverted": self.reverted,
            "operations": self.operations,
            "forced_over": self.conflicts,
            "notes": self.notes,
        }
        if self.impact is not None:
            # ``hosts_changed``/``hosts`` predate ``impact`` (kept for older clients): its
            # ``total`` and its capped host list.
            out["hosts_changed"] = self.impact["total"]
            out["hosts"] = self.impact["hosts"]
            out["impact"] = self.impact
        return out


def revisions_of_change(session: Session, change: str) -> list[int]:
    return list(session.scalars(select(Revision.id).where(Revision.change == change).order_by(Revision.id)))


def revert(
    session: Session,
    revisions: list[int],
    *,
    actor: str,
    source: str,
    message: str | None = None,
    force: bool = False,
    hosts: bool = False,
    change: str | None = None,
) -> RevertResult:
    """Revert ``revisions`` as one new revision (``change``: they are that change id's). The
    caller holds the write lock (or runs a dry run it rolls back), runs ``check_export`` and
    commits. Raises ``RevertError``. ``hosts`` also measures what the revert does to the hosts
    (``ais.impact``, the dry run's preview); the caller then rolls back."""
    revisions = sorted(set(revisions))
    for rid in revisions:
        if session.get(Revision, rid) is None:
            raise RevertError(404, {"error": "not found", "message": f"revision {rid} does not exist"})
    rows = _rows_of(session, revisions)
    _check_invertible(session, revisions, rows)
    conflicts = find_conflicts(session, revisions, rows)
    if conflicts and not force:
        raise _conflict_error(revisions, conflicts)

    preview = impact.Preview(session) if hosts else None
    label = f"change {change} ({_label(revisions)})" if change is not None else _label(revisions)
    if message is None:
        first = session.get(Revision, revisions[0])
        message = f"revert {label}" + (f": {first.message}" if change is None and first is not None and first.message else "")
    if preview is not None:
        rev = preview.revision(actor=actor, source=source, message=message)
    else:
        rev = Revision(actor=actor, source=source, message=message, change=current_change())
        session.add(rev)
        session.flush()
    ctx = _Ctx(session=session, rev=rev, actor=actor, source=source)
    for row in rows:
        _invert(ctx, row)
    session.flush()
    try:
        schema.check_live(session, keys=ctx.keys, created_rev=rev.id)
    except schema.SchemaViolation as exc:
        raise RevertError(422, {"error": "schema", "message": f"cannot revert {label}: {exc}"}) from exc
    if not ctx.operations:
        raise RevertError(409, {"error": "nothing to revert", "message": f"{label}: everything it changed is already as it was before"})
    result = RevertResult(revision=rev, reverted=revisions, operations=ctx.operations, conflicts=conflicts if force else [], notes=ctx.notes)
    if preview is not None:
        result.impact = preview.impact(rev.id)
    audit.record(
        session,
        actor=actor,
        action=SUMMARY_ACTION,
        target_type="change" if change is not None else "revision",
        target_id=change if change is not None else revisions[0],
        after={"reverted": revisions, "change": change, "operations": len(ctx.operations), "forced_over": len(conflicts) if force else 0},
        source=source,
        revision=rev.id,
    )
    return result

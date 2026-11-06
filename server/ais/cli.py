"""``ais`` command line: init-db, import, export, export-tree, restore, revert, render, vars, serve, user, token."""

from __future__ import annotations

import argparse
import getpass
import json
import sys

from ais.db import init_db, session_scope
from ais.importer import ImportError_, import_tree_report
from ais.resolver import build_export, load_snapshot, resolve


def _read_password(args: argparse.Namespace) -> str | None:
    if getattr(args, "no_password", False):
        return None
    if getattr(args, "password_stdin", False):
        return sys.stdin.readline().rstrip("\n")
    if not sys.stdin.isatty():
        raise SystemExit("no TTY: pass --password-stdin (and pipe the password) or --no-password")
    pw = getpass.getpass("Password: ")
    if pw != getpass.getpass("Again: "):
        raise SystemExit("passwords differ")
    return pw


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ais")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db", help="create or upgrade the schema (Alembic; AIS_DATABASE_URL)")
    sub.add_parser("migrate", help="alias of init-db")
    imp = sub.add_parser("import", help="import a YAML tree")
    imp.add_argument("file")
    imp.add_argument("--message")
    imp.add_argument("--dry-run", action="store_true", help="report what the import would change, write nothing")
    imp.add_argument("--change", help="change id to record on the revision (like X-AIS-Change), e.g. SRE-1234")
    exp = sub.add_parser("export", help="print the plugin export as JSON")
    exp.add_argument("--revision", type=int)
    exp.add_argument("--subtree")
    ext = sub.add_parser("export-tree", help="print the tree (or a subtree) in the import format, as YAML")
    ext.add_argument("--revision", type=int, help="as of this revision (default: current); restores a deleted subtree")
    ext.add_argument("--subtree", help="path of the subtree, e.g. all.commercial.prod.dc1 (default: the whole tree)")
    ext.add_argument("--as", dest="as_path", metavar="PATH", help="re-root the subtree at PATH (a clone)")
    ext.add_argument("--ancestor-vars", action="store_true", help="also export the ancestors' own variables (self-contained)")
    ext.add_argument("--no-hosts", action="store_true", help="leave the hosts out (a clone inside one inventory)")
    ext.add_argument("--format", choices=["yaml", "json"], default="yaml")
    rst = sub.add_parser("restore", help="bring a deleted subtree back as it was at a revision (POST /api/v1/nodes/restore)")
    rst.add_argument("path", help="path of the deleted subtree, e.g. all.commercial.prod.dc1")
    rst.add_argument("--revision", type=int, help="as it was at this revision (default: the last one before its latest delete)")
    rst.add_argument("--message")
    rst.add_argument("--dry-run", action="store_true", help="report what the restore would bring back, write nothing")
    rst.add_argument("--change", help="change id to record on the revision (like X-AIS-Change)")
    rvt = sub.add_parser("revert", help="undo a revision, or every revision of a change id, as one new revision")
    which = rvt.add_mutually_exclusive_group(required=True)
    which.add_argument("--revision", type=int, help="the revision to undo")
    which.add_argument("--of-change", metavar="CHANGE", help="undo every revision carrying this change id")
    rvt.add_argument("--force", action="store_true", help="revert even though later revisions changed the same things")
    rvt.add_argument("--dry-run", action="store_true", help="list the inverse operations and the hosts that would change, write nothing")
    rvt.add_argument("--message")
    rvt.add_argument("--change", help="change id to record on the revert's own revision")
    ren = sub.add_parser("render", help="resolve a real or mock host")
    ren.add_argument("--host")
    ren.add_argument("--path")
    ren.add_argument("--revision", type=int)
    ren.add_argument("--facts", help="JSON object of mock facts")
    ren.add_argument("--no-template", action="store_true")
    vrs = sub.add_parser("vars", help="where a variable is set, or which hosts have which value (GET /api/v1/vars)")
    vrs.add_argument("key", help="the variable name, e.g. gpu_driver")
    vrs.add_argument("--value", help="only this value: JSON (550.90 a number, '\"550.90\"' a string), else a plain string")
    vrs.add_argument("--hosts", action="store_true", help="list hosts and their resolved value instead of the nodes that set it")
    vrs.add_argument("--subtree", help="only this node and below, e.g. all.commercial.prod")
    vrs.add_argument("--revision", type=int, help="as of this revision (default: current)")
    vrs.add_argument("--limit", type=int, help="at most this many rows (default: all)")
    vrs.add_argument("--offset", type=int, default=0)
    vrs.add_argument("--json", action="store_true", help="print the API's JSON document instead of a table")
    lnt = sub.add_parser("lint", help="list undeclared keys that look like typos of known ones (GET /api/v1/vars/lint); exit 1 when any")
    lnt.add_argument("--json", action="store_true", help="print the API's JSON document instead of lines")
    snp = sub.add_parser("snapshots", help="per-host snapshot history").add_subparsers(dest="snap_cmd", required=True)
    snp.add_parser("run", help="run background passes now until the newest revision is passed")
    snp.add_parser("status", help="newest revision, newest passed revision, last pass")
    srv = sub.add_parser("serve", help="run the API with uvicorn")
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8000)

    user = sub.add_parser("user", help="manage local users").add_subparsers(dest="user_cmd", required=True)
    uc = user.add_parser("create", help="create a user (password from a prompt, --password-stdin, or none)")
    uc.add_argument("email")
    uc.add_argument("--role", choices=["viewer", "editor", "admin"], default="viewer")
    uc.add_argument("--display-name")
    uc.add_argument("--password-stdin", action="store_true", help="read the password from the first line of stdin")
    uc.add_argument("--no-password", action="store_true", help="token-only user (cannot log in with a password)")
    user.add_parser("list")

    token = sub.add_parser("token", help="manage API tokens").add_subparsers(dest="token_cmd", required=True)
    tc = token.add_parser("create", help="mint a token; prints it once")
    tc.add_argument("--user", required=True, help="user email")
    tc.add_argument("--name", default="cli")
    tc.add_argument("--role", choices=["viewer", "editor", "admin"], help="cap below the user's role")
    tc.add_argument("--expires-days", type=int)
    tr = token.add_parser("revoke")
    tr.add_argument("id", type=int)
    token.add_parser("list")

    args = p.parse_args(argv)
    if getattr(args, "change", None) is not None:
        from ais import audit

        try:
            audit.change_var.set(audit.check_change(args.change))
        except ValueError as exc:
            raise SystemExit(str(exc))

    if args.cmd in ("init-db", "migrate"):
        init_db()
        return 0
    if args.cmd == "import":
        return _import(args)
    if args.cmd == "export":
        with session_scope() as s:
            json.dump(build_export(load_snapshot(s, args.revision, args.subtree)), sys.stdout, indent=2)
        print()
        return 0
    if args.cmd == "export-tree":
        return _export_tree(args)
    if args.cmd == "restore":
        return _restore(args)
    if args.cmd == "revert":
        return _revert(args)
    if args.cmd == "render":
        with session_scope() as s:
            out = resolve(load_snapshot(s, args.revision), host=args.host, path=args.path)
            if not args.no_template:
                from ais.render import MOCK_HOSTNAME, render_templates

                facts = json.loads(args.facts) if args.facts else None
                out["rendered"], out["errors"] = render_templates(out["resolved"], facts=facts, hostname=args.host or MOCK_HOSTNAME, chain=out["chain"])
            json.dump(out, sys.stdout, indent=2, default=str)
        print()
        return 0
    if args.cmd == "vars":
        return _vars(args)
    if args.cmd == "lint":
        return _lint(args)
    if args.cmd == "snapshots":
        from ais import snapshots

        init_db()
        if args.snap_cmd == "run":
            for r in snapshots.catch_up():
                print(f"revision {r.revision}: {r.hosts_checked} hosts checked, {r.rows_written} changed, {r.seconds:.2f}s")
        with session_scope() as s:
            print(json.dumps(snapshots.status(s), indent=2, default=str))
        return 0
    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("ais.api:app", host=args.host, port=args.port)
        return 0
    if args.cmd == "user":
        return _user(args)
    if args.cmd == "token":
        return _token(args)
    return 1


def _import(args: argparse.Namespace) -> int:
    """The CLI twin of ``POST /api/v1/import``: takes the write lock, refuses a tree that
    no longer exports, writes nothing when nothing changed (or ``--dry-run``), and
    leaves one ``import`` audit row otherwise."""
    import yaml

    from ais import audit, impact, keycheck
    from ais.locking import lock_for_write
    from ais.resolver import current_revision

    init_db()
    with open(args.file) as fh:
        tree = yaml.safe_load(fh)
    with session_scope() as s:
        lock_for_write(s)
        preview = impact.Preview(s) if args.dry_run else None
        try:
            rev = preview.revision(actor="cli", source="import", message=args.message or "bulk import") if preview else None
            result = import_tree_report(s, tree, actor="cli", message=args.message, rev=rev)
            build_export(load_snapshot(s))
            keys = keycheck.review(s, result.revision.id, strict=keycheck.strict_keys())
        except (ImportError_, ValueError) as exc:  # keycheck.UndeclaredKeys is a ValueError
            s.rollback()
            raise SystemExit(f"import refused: {exc}")
        _print_warnings(keys)
        summary = result.summary()
        counts = (
            f"{result.nodes_created} nodes created, {result.variables_set} variables set, "
            f"{len(result.definitions_changed)} definitions changed, {len(result.tags_changed)} tags changed"
        )
        if args.dry_run or not result.changed:
            found = preview.impact(result.revision.id) if preview is not None and result.changed else None
            s.rollback()
            if args.dry_run:
                print(f"dry run: would change {counts}" if result.changed else "dry run: nothing to change")
                if found is not None:
                    _print_impact(found)
            else:
                print(f"nothing to change (revision {current_revision(s)})")
            return 0
        rev = result.revision
        audit.record(
            s,
            actor="cli",
            action="import",
            target_type="tree",
            target_id="all",
            before=result.before(),
            after={"revision": rev.id, **summary},
            source="cli",
            revision=rev.id,
        )
        print(f"imported as revision {rev.id}: {counts}")
    return 0


def _restore(args: argparse.Namespace) -> int:
    """The CLI twin of ``POST /api/v1/nodes/restore``: takes the write lock, refuses what the
    API refuses (with its message), checks the export, and leaves one ``node.restore`` audit
    row whose ``restored`` map keeps the subtree's history (``ais.restore``)."""
    from ais import audit, impact, keycheck
    from ais.api_write import live_vars, node_out
    from ais.locking import lock_for_write
    from ais.models import Node
    from ais.restore import RestoreError, restore_subtree

    init_db()
    with session_scope() as s:
        lock_for_write(s)
        preview = impact.Preview(s) if args.dry_run else None
        try:
            rev = preview.revision(actor="cli", source="cli", message=args.message or f"restore {args.path}") if preview else None
            result = restore_subtree(s, args.path, args.revision, actor="cli", source="cli", message=args.message, rev=rev)
            build_export(load_snapshot(s))
        except (RestoreError, ImportError_, ValueError) as exc:
            s.rollback()
            raise SystemExit(f"restore refused: {exc}")
        _print_warnings(keycheck.review(s, result.revision.id, strict=False))
        summary = result.summary()
        counts = f"{summary['nodes']} nodes ({summary['hosts']} hosts), {summary['variables_set']} variables, {len(summary['tags'])} tags"
        if args.dry_run:
            found = preview.impact(result.revision.id)  # type: ignore[union-attr]
            s.rollback()
            print(f"dry run: would restore {result.path} as of revision {result.from_revision}: {counts}")
            _print_impact(found)
            return 0
        rev = result.revision
        root = s.get(Node, result.restored[0]["id"])
        out = node_out(root, live_vars(s, root.id))
        audit.record(
            s,
            actor="cli",
            action="node.restore",
            target_type="node",
            target_id=root.id,
            before={"path": result.path, "id": result.restored[0]["old_id"], "revision": result.from_revision},
            after={**out, **summary, "restored": result.restored},
            source="cli",
            revision=rev.id,
        )
        print(f"restored {result.path} as of revision {result.from_revision} as revision {rev.id}: {counts}")
    return 0


def _revert(args: argparse.Namespace) -> int:
    """The CLI twin of ``POST /api/v1/revisions/{id}/revert`` and ``POST /changes/{id}/revert``:
    takes the write lock, refuses what the API refuses (conflicts unless ``--force``), checks
    the export, and leaves the same audit rows (``source=cli``)."""
    from ais import keycheck
    from ais.locking import lock_for_write
    from ais.revert import RevertError, revert, revisions_of_change

    init_db()
    with session_scope() as s:
        lock_for_write(s)
        if args.of_change is not None:
            revisions = revisions_of_change(s, args.of_change)
            if not revisions:
                raise SystemExit(f"no revision carries change id {args.of_change!r}")
        else:
            revisions = [args.revision]
        try:
            result = revert(
                s, revisions, actor="cli", source="cli", message=args.message, force=args.force, hosts=args.dry_run, change=args.of_change
            )
            build_export(load_snapshot(s))
        except (RevertError, ValueError) as exc:
            s.rollback()
            raise SystemExit(f"revert refused: {exc}")
        _print_warnings(keycheck.review(s, result.revision.id, strict=False))
        for op in result.operations:
            print(f"  revision {op['reverts']}: {op['summary']}")
        for c in result.conflicts:
            print(f"  forced over revision {c['revision']} ({c['action']} {c['target']}): {c['on']}")
        for note in result.notes:
            print(f"  note: {note}")
        label = f"change {args.of_change}" if args.of_change else f"revision {args.revision}"
        if args.dry_run:
            s.rollback()
            print(f"dry run: reverting {label} would apply {len(result.operations)} operation(s)")
            _print_impact(result.impact)  # type: ignore[arg-type]
            return 0
        print(f"reverted {label} as revision {result.revision.id}: {len(result.operations)} operation(s)")
    return 0


def _print_impact(found: dict, shown: int = 50) -> None:
    """A dry run's ``impact`` (``ais.impact``): the counts, then the first hosts."""
    from ais import impact

    print(f"impact: {impact.summary(found)}")
    for h in found["hosts"][:shown]:
        keys = sorted(set(h.get("added", {})) | set(h.get("removed", {})) | set(h.get("changed", {})))
        extra = [", ".join(keys)] if keys else []
        if h.get("joined") or h.get("left"):
            extra.append("groups " + " ".join([f"+{g}" for g in h.get("joined", [])] + [f"-{g}" for g in h.get("left", [])]))
        if h.get("moved_from"):
            extra.append(f"from {h['moved_from']}")
        print(f"  {h['host']}: {h['status']} [{h['path']}]" + (f" ({'; '.join(extra)})" if extra else ""))
    if found["total"] > shown:
        print(f"  ... and {found['total'] - shown} more")


def _print_warnings(review) -> None:
    """Near-miss key warnings (``ais.keycheck``) on stderr, so stdout stays the summary."""
    for line in review.lines():
        print(line, file=sys.stderr)


def _lint(args: argparse.Namespace) -> int:
    """The CLI twin of ``GET /api/v1/vars/lint``: undeclared keys that look like typos. Exits
    1 when it finds any, so a pipeline can gate on it."""
    from ais import keycheck
    from ais.resolver import current_revision

    with session_scope() as s:
        keys = keycheck.lint(s)
        rev = current_revision(s)
    if args.json:
        json.dump({"revision": rev, "total": len(keys), "keys": keys, "strict": keycheck.strict_keys()}, sys.stdout, indent=2)
        print()
    else:
        for k in keys:
            known = "declared" if k["declared"] else "widely used, not declared"
            more = f" (+{k['nodes'] - len(k['paths'])} more)" if k["nodes"] > len(k["paths"]) else ""
            print(f"{k['key']}: did you mean {k['did_you_mean']} ({known})? set on {k['nodes']} node(s): {', '.join(k['paths'])}{more}")
        print(f"{len(keys)} likely typo(s) at revision {rev}", file=sys.stderr)
    return 1 if keys else 0


def _export_tree(args: argparse.Namespace) -> int:
    """The CLI twin of ``GET /api/v1/tree``. Writes the document to stdout and the revision
    it was taken at to stderr, so ``ais export-tree > tree.yml`` is a clean file."""
    from ais.models import Revision
    from ais.resolver import current_revision
    from ais.tree import TreeExportError, dump_yaml, export_tree

    with session_scope() as s:
        if args.revision is not None and s.get(Revision, args.revision) is None:
            raise SystemExit(f"revision {args.revision} does not exist")
        rev = args.revision if args.revision is not None else current_revision(s)
        try:
            data = export_tree(s, rev, args.subtree, as_path=args.as_path, ancestor_vars=args.ancestor_vars, hosts=not args.no_hosts)
        except KeyError as exc:
            raise SystemExit(f"{exc.args[0]} at revision {rev}")
        except TreeExportError as exc:
            raise SystemExit(str(exc))
    if args.format == "json":
        json.dump(data, sys.stdout, indent=2)
        print()
    else:
        sys.stdout.write(dump_yaml(data))
    print(f"exported {args.subtree or 'all'} at revision {rev}", file=sys.stderr)
    return 0


def _vars(args: argparse.Namespace) -> int:
    """The CLI twin of ``GET /api/v1/vars`` (``--hosts``: ``/vars/hosts``), read in-process."""
    from ais import varsearch
    from ais.models import Revision

    def show(v: object) -> str:
        return json.dumps(v, sort_keys=True)

    with session_scope() as s:
        if args.revision is not None and s.get(Revision, args.revision) is None:
            raise SystemExit(f"revision {args.revision} does not exist")
        search = varsearch.host_values if args.hosts else varsearch.where_set
        try:
            out = search(
                s,
                args.key,
                args.revision,
                subtree=args.subtree,
                wanted=varsearch.parse_value(args.value),
                limit=args.limit if args.limit is not None else sys.maxsize,
                offset=args.offset,
            )
        except KeyError as exc:
            raise SystemExit(exc.args[0])
    if args.json:
        json.dump(out, sys.stdout, indent=2, default=str)
        print()
        return 0
    rows = out["hosts"] if args.hosts else out["nodes"]
    what = f"{args.key} ({out['strategy']}{'' if out['declared'] else ', undeclared'}) at revision {out['revision']}"
    if args.hosts:
        print(f"{what}: {out['total']} host(s) match, {out['with_key']} have it, {out['without_key']} do not")
        print("values: " + ", ".join(f"{show(v['value'])} x{v['hosts']}" for v in out["values"]) + (" ..." if out["values_truncated"] else ""))
        for r in rows:
            origin = f"folded from {' + '.join(r['sources'])}" if "sources" in r else f"from {r.get('source', r['from'])}"
            print(f"{r['host']}\t{show(r['value'])}\t{origin}")
    else:
        print(f"{what}: set on {out['total']} node(s)")
        for r in rows:
            extra = f"\t(emits {show(r['effective'])})" if "effective" in r else ""
            above = "\t(above the subtree)" if r.get("above_subtree") else ""
            print(f"{r.get('source', r['path'])}\t{show(r['value'])}{extra}{above}")
        for p in out.get("profiles") or []:
            print(f"profile:{p['name']}\t{show(p['value'])}\t(the profile itself)")
    if out["total"] > out["offset"] + len(rows):
        print(f"... {out['total'] - out['offset'] - len(rows)} more (--offset {out['offset'] + len(rows)})", file=sys.stderr)
    return 0


def _user(args: argparse.Namespace) -> int:
    import datetime as dt

    from sqlalchemy import select

    from ais import audit, auth
    from ais.models import Role, User

    init_db()
    with session_scope() as s:
        if args.user_cmd == "create":
            password = _read_password(args)
            try:
                u = auth.create_user(s, args.email, role=Role(args.role), password=password, display_name=args.display_name)
            except ValueError as exc:
                raise SystemExit(str(exc))
            audit.record(s, actor="cli", action="user.create", target_type="user", target_id=u.email, after={"email": u.email, "role": u.role.value, "password": password is not None}, source="cli")
            print(f"created user {u.email} ({u.role.value}, {'password' if password else 'token-only'})", file=sys.stderr)
            return 0
        if args.user_cmd == "list":
            for u in s.scalars(select(User).order_by(User.id)):
                print(f"{u.id}\t{u.email}\t{u.role.value}\t{u.auth_source}\t{'active' if u.is_active else 'disabled'}")
            return 0
    return 1


def _token(args: argparse.Namespace) -> int:
    import datetime as dt

    from sqlalchemy import select

    from ais import audit, auth
    from ais.models import ApiToken, Role, User

    init_db()
    with session_scope() as s:
        if args.token_cmd == "create":
            user = s.scalar(select(User).where(User.email == args.user.strip().lower()))
            if user is None:
                raise SystemExit(f"unknown user {args.user!r}")
            expires = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=args.expires_days) if args.expires_days else None
            try:
                plaintext, row = auth.create_token(s, user, name=args.name, role=Role(args.role) if args.role else None, expires_at=expires)
            except ValueError as exc:
                raise SystemExit(str(exc))
            audit.record(s, actor="cli", action="token.create", target_type="token", target_id=row.id, after={"user": user.email, "name": row.name, "role": row.role.value if row.role else None, "expires_at": row.expires_at}, source="cli")
            print(f"token {row.id} ({row.prefix}…) for {user.email}; shown once:", file=sys.stderr)
            print(plaintext)
            return 0
        if args.token_cmd == "revoke":
            row = s.get(ApiToken, args.id)
            if row is None:
                raise SystemExit(f"unknown token {args.id}")
            row.revoked_at = dt.datetime.now(dt.timezone.utc)
            audit.record(s, actor="cli", action="token.revoke", target_type="token", target_id=row.id, before={"user_id": row.user_id, "name": row.name}, source="cli")
            print(f"revoked token {row.id}", file=sys.stderr)
            return 0
        if args.token_cmd == "list":
            for t in s.scalars(select(ApiToken).order_by(ApiToken.id)):
                state = "revoked" if t.revoked_at else "active"
                print(f"{t.id}\t{t.prefix}…\t{t.user_id}\t{t.name}\t{t.kind}\t{state}\texpires={t.expires_at or '-'}")
            return 0
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

"""``ais`` command line: init-db, import, export, render, serve, user, token."""

from __future__ import annotations

import argparse
import getpass
import json
import sys

from ais.db import init_db, session_scope
from ais.importer import import_file
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
    exp = sub.add_parser("export", help="print the plugin export as JSON")
    exp.add_argument("--revision", type=int)
    exp.add_argument("--subtree")
    ren = sub.add_parser("render", help="resolve a real or mock host")
    ren.add_argument("--host")
    ren.add_argument("--path")
    ren.add_argument("--revision", type=int)
    ren.add_argument("--facts", help="JSON object of mock facts")
    ren.add_argument("--no-template", action="store_true")
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

    if args.cmd in ("init-db", "migrate"):
        init_db()
        return 0
    if args.cmd == "import":
        init_db()
        with session_scope() as s:
            rev = import_file(s, args.file, actor="cli", message=args.message)
            print(f"imported as revision {rev.id}")
        return 0
    if args.cmd == "export":
        with session_scope() as s:
            json.dump(build_export(load_snapshot(s, args.revision, args.subtree)), sys.stdout, indent=2)
        print()
        return 0
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

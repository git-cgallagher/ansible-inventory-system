"""``ais`` command line: init-db, import, export, render, serve."""

from __future__ import annotations

import argparse
import json
import sys

from ais.db import init_db, session_scope
from ais.importer import import_file
from ais.resolver import build_export, load_snapshot, resolve


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="ais")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db", help="create tables (AIS_DATABASE_URL)")
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
    srv = sub.add_parser("serve", help="run the API with uvicorn")
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8000)
    args = p.parse_args(argv)

    if args.cmd == "init-db":
        init_db()
        return 0
    if args.cmd == "import":
        init_db()
        with session_scope() as s:
            rev = import_file(s, args.file, message=args.message)
            print(f"imported as revision {rev.id}")
        return 0
    if args.cmd == "export":
        with session_scope() as s:
            json.dump(build_export(load_snapshot(s, args.revision, args.subtree)), sys.stdout, indent=2)
        print()
        return 0
    if args.cmd == "render":
        with session_scope() as s:
            json.dump(resolve(load_snapshot(s, args.revision), host=args.host, path=args.path), sys.stdout, indent=2)
        print()
        return 0
    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("ais.api:app", host=args.host, port=args.port)
        return 0
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

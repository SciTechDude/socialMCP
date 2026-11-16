"""SocialMCP command line.

Start here if you have never used a terminal:

    socialmcp setup                 # connect SocialMCP to the AI apps you have
    socialmcp drop                  # open the folder to put your LinkedIn .zip in
    socialmcp ingest                # read whatever is in that folder
    socialmcp doctor                # check everything and tell you what to fix
    socialmcp can-do                # list what it can and cannot do

Everything below is for development and scripting:

    socialmcp serve                 # run the MCP server over stdio (what clients launch)
    socialmcp watch                 # run the harness alone, in the foreground
    socialmcp ingest <path|->       # ingest one archive, or the whole drop folder
    socialmcp search "gtm" --event seattle-ai-week
    socialmcp clusters              # list detected event bursts
    socialmcp briefing              # print the context snapshot the AI sees
    socialmcp reindex               # re-embed contacts
    socialmcp stats
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .config import load_config
from .core import clustering
from .core.db import open_database
from .core.harness import Harness
from .core.ingest import IngestError, ingest_path, refresh_embeddings
from .core.search import SearchFilters, search_network


def _print(payload: object) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="socialmcp", description=__doc__)
    parser.add_argument("--config", help="path to socialmcp.toml")
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="command", required=True)

    p_setup = sub.add_parser("setup", help="connect SocialMCP to your AI apps (start here)")
    p_setup.add_argument("--client", action="append", dest="clients",
                         help="only configure this client (claude-desktop, claude-code, cursor, "
                              "windsurf, vscode, zed); repeatable")
    p_setup.add_argument("--force", action="store_true",
                         help="replace an existing socialmcp entry")
    p_setup.add_argument("--dry-run", action="store_true",
                         help="show what would change without writing anything")

    sub.add_parser("doctor", help="check your setup and explain how to fix problems")
    sub.add_parser("can-do", help="list what SocialMCP can and cannot do")
    sub.add_parser("drop", help="open the folder where LinkedIn .zip files go")
    sub.add_parser("serve", help="run the MCP server over stdio")
    sub.add_parser("stats", help="print local CRM stats")
    sub.add_parser("clusters", help="list event clusters")
    sub.add_parser("watch", help="run the harness in the foreground")

    p_ingest = sub.add_parser("ingest", help="ingest an archive or the drop folder")
    p_ingest.add_argument("path", nargs="?", help="path to a .zip or extracted folder")
    p_ingest.add_argument("--source", help="force an adapter, e.g. linkedin")
    p_ingest.add_argument("--force", action="store_true", help="re-ingest even if seen before")

    p_search = sub.add_parser("search", help="search the network")
    p_search.add_argument("query", nargs="?", default="")
    p_search.add_argument("--company")
    p_search.add_argument("--title")
    p_search.add_argument("--status")
    p_search.add_argument("--event")
    p_search.add_argument("--mode", default="hybrid", choices=["hybrid", "keyword", "semantic"])
    p_search.add_argument("--limit", type=int, default=10)

    p_brief = sub.add_parser("briefing", help="print the context snapshot")
    p_brief.add_argument("--acknowledge", action="store_true")

    p_reindex = sub.add_parser("reindex", help="re-embed contacts")
    p_reindex.add_argument("--limit", type=int)

    p_rename = sub.add_parser("rename-cluster", help="name an event cluster")
    p_rename.add_argument("event")
    p_rename.add_argument("new_name")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    if args.command == "serve":
        from .mcp_server.server import main as serve_main

        serve_main()
        return 0

    config = load_config(args.config)
    config.ensure_dirs()
    db = open_database(config)
    try:
        return _dispatch(args, config, db)
    finally:
        db.close()


SYMBOL = {"ok": "OK   ", "warn": "!    ", "fail": "X    "}


def _cmd_setup(args: argparse.Namespace, config) -> int:
    from .clients import known_clients, setup_all

    config.ensure_dirs()
    drop = config.drop_dir / "linkedin"

    # A client launches the server with no shell and no environment, so the
    # paths it needs are pinned into the client's config rather than inherited.
    env = {"SOCIALMCP_HOME": str(config.home), "SOCIALMCP_DROP_DIR": str(config.drop_dir)}

    print("Connecting SocialMCP to the AI apps on this computer...\n")
    launch, results = setup_all(only=args.clients, env=env, force=args.force, dry_run=args.dry_run)

    if not results:
        print("  No MCP-compatible AI apps found.\n")
        print("  Install one of these, then run 'socialmcp setup' again:")
        print("    Claude Desktop   https://claude.ai/download")
        print("    Cursor           https://cursor.com")
        print("    Windsurf         https://windsurf.com")
        print("\n  Apps checked: " + ", ".join(t.display_name for t in known_clients()))
        return 1

    restart_needed = []
    for result in results:
        good = result.status in {"configured", "updated", "already"}
        print(f"  {SYMBOL['ok' if good else 'warn']}{result.client}: {result.status}")
        if result.detail:
            print(f"         {result.detail}")
        if result.backup:
            print(f"         (previous settings saved to {result.backup})")
        if result.status in {"configured", "updated"}:
            restart_needed.append(result.client)

    print(f"\n  Start command given to those apps:\n    {launch.as_display()}")
    print("\nNext steps:")
    print(f"  1. Put your LinkedIn export .zip in this folder:\n       {drop}")
    print("     (LinkedIn: Settings & Privacy -> Data Privacy -> Get a copy of your data)")
    print("  2. Run: socialmcp ingest")
    if restart_needed:
        print(f"  3. Quit and reopen: {', '.join(restart_needed)}")
    print('  4. Ask your AI assistant: "what event clusters are in my network?"')
    return 0


def _cmd_doctor(config) -> int:
    from .doctor import FAIL, WARN, run_checks

    checks = run_checks(config)
    print("SocialMCP check-up\n")
    width = max(len(c.name) for c in checks)
    for check in checks:
        print(f"  {SYMBOL[check.status]}{check.name.ljust(width)}  {check.detail}")
        if check.fix:
            print(f"       -> {check.fix}")

    problems = [c for c in checks if c.status == FAIL]
    warnings = [c for c in checks if c.status == WARN]
    print()
    if problems:
        print(f"{len(problems)} thing(s) must be fixed before SocialMCP will work.")
        return 1
    if warnings:
        print(f"Everything essential works. {len(warnings)} optional improvement(s) above.")
        return 0
    print("Everything looks good.")
    return 0


def _cmd_drop(config) -> int:
    from .doctor import open_folder

    config.ensure_dirs()
    drop = config.drop_dir / "linkedin"
    print(f"Put your LinkedIn export .zip file in this folder:\n\n    {drop}\n")
    print("Get the file from LinkedIn: Settings & Privacy -> Data Privacy ->")
    print("Get a copy of your data -> request the larger archive.\n")
    if open_folder(drop):
        print("(Opened it for you.)")
    print("\nOnce the .zip is in there, run: socialmcp ingest")
    return 0


def _dispatch(args: argparse.Namespace, config, db) -> int:
    if args.command == "setup":
        return _cmd_setup(args, config)

    if args.command == "doctor":
        return _cmd_doctor(config)

    if args.command == "can-do":
        from . import capabilities

        print(capabilities.as_markdown())
        return 0

    if args.command == "drop":
        return _cmd_drop(config)

    if args.command == "stats":
        _print(db.stats())
        return 0

    if args.command == "clusters":
        _print(
            [
                dict(r)
                for r in db.query(
                    "SELECT id, slug, name, start_date, end_date, contact_count, renamed_by_user "
                    "FROM event_clusters ORDER BY end_date DESC"
                )
            ]
        )
        return 0

    if args.command == "ingest":
        harness = Harness(db, config)
        if args.path:
            try:
                _print(ingest_path(db, config, Path(args.path), source=args.source, force=args.force).as_dict())
            except (IngestError, OSError) as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
        else:
            results = harness.scan_now(reason="cli")
            _print([r.as_dict() for r in results] or {"message": f"nothing to ingest in {config.drop_dir}"})
        return 0

    if args.command == "search":
        result = search_network(
            db,
            config,
            query=args.query,
            filters=SearchFilters(
                company=args.company, title_contains=args.title, status=args.status, event=args.event
            ),
            limit=args.limit,
            mode=args.mode,
        )
        _print(result.as_dict())
        return 0

    if args.command == "briefing":
        _print(Harness(db, config).briefing(acknowledge=args.acknowledge))
        return 0

    if args.command == "reindex":
        _print({"embedded": refresh_embeddings(db, config, limit=args.limit)})
        return 0

    if args.command == "rename-cluster":
        try:
            _print(clustering.rename_cluster(db, args.event, args.new_name))
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.command == "watch":
        harness = Harness(db, config)
        harness.start()
        print(f"watching {config.drop_dir} — Ctrl-C to stop", file=sys.stderr)
        try:
            import threading

            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        finally:
            harness.stop()
        return 0

    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

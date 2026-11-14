"""Local CLI — useful for testing the pipeline without an AI client attached.

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


def _dispatch(args: argparse.Namespace, config, db) -> int:
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

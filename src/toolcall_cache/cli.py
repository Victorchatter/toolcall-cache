"""CLI entry point for toolcall-cache."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from . import cache, policy, server

DEFAULT_DB = os.path.expanduser("~/.toolcall-cache/toolcall-cache.db")
DEFAULT_TTL = 3600
DEFAULT_DENYLIST = ",".join(policy.DEFAULT_DENYLIST)


def _default_db() -> str:
    return DEFAULT_DB


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="toolcall-cache",
        description="A local, content-addressed cache for MCP tool results.",
    )

    # Common args shared by all subcommands.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--db",
        default=_default_db(),
        help=f"Path to the SQLite cache database (default: {DEFAULT_DB}).",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    start_parser = sub.add_parser("start", help="Start the MCP proxy.", parents=[common])
    start_parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="Transport to use (default: stdio).",
    )
    start_parser.add_argument(
        "--upstream",
        required=True,
        help="Upstream MCP server. For stdio: shell command. For http: base URL.",
    )
    start_parser.add_argument(
        "--server-id",
        default="default",
        help="Server identity included in cache keys (default: default).",
    )
    start_parser.add_argument(
        "--allowlist",
        default="",
        help="Comma-separated list of tool names to cache.",
    )
    start_parser.add_argument(
        "--denylist",
        default=DEFAULT_DENYLIST,
        help=f"Comma-separated glob patterns never to cache (default: {DEFAULT_DENYLIST}).",
    )
    start_parser.add_argument(
        "--ttl",
        type=float,
        default=DEFAULT_TTL,
        help=f"Cache TTL in seconds (default: {DEFAULT_TTL}).",
    )
    start_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind for HTTP transport (default: 127.0.0.1).",
    )
    start_parser.add_argument(
        "--port",
        type=int,
        default=8787,
        help="Port to bind for HTTP transport (default: 8787).",
    )

    sub.add_parser("clear", help="Delete all cached entries.", parents=[common])

    invalidate_parser = sub.add_parser("invalidate", help="Delete cached entries for one tool.", parents=[common])
    invalidate_parser.add_argument("tool", help="Tool name to invalidate.")

    sub.add_parser("list", help="List cached entries.", parents=[common])
    sub.add_parser("stats", help="Show cache statistics.", parents=[common])

    return parser


def _parse_name_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _fmt_time(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def cmd_clear(args: argparse.Namespace) -> int:
    conn = cache.init_db(args.db)
    try:
        n = cache.clear(conn)
        print(f"Cleared {n} cache entr{'y' if n == 1 else 'ies'} from {args.db}")
    finally:
        conn.close()
    return 0


def cmd_invalidate(args: argparse.Namespace) -> int:
    conn = cache.init_db(args.db)
    try:
        n = cache.invalidate_tool(conn, args.tool)
        print(f"Invalidated {n} entr{'y' if n == 1 else 'ies'} for tool '{args.tool}'")
    finally:
        conn.close()
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    conn = cache.init_db(args.db)
    try:
        rows = cache.list_entries(conn)
        if not rows:
            print("Cache is empty.")
            return 0
        print(f"{'key_hash':<16} {'tool':<20} {'server_id':<12} {'created':<20} {'expires':<20} {'hits'}")
        print("-" * 110)
        for row in rows:
            key = row["key_hash"][:16]
            print(
                f"{key:<16} {row['tool_name']:<20} {row['server_id']:<12} "
                f"{_fmt_time(row['created_at']):<20} {_fmt_time(row['expires_at']):<20} {row['hit_count']}"
            )
    finally:
        conn.close()
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    conn = cache.init_db(args.db)
    try:
        s = cache.stats(conn)
        print(f"Database: {args.db}")
        print(f"Total entries: {s['total_entries']}")
        print(f"Expired entries: {s['expired_entries']}")
        print(f"Total cache hits: {s['total_hits']}")
        if s["tools"]:
            print("\nPer-tool:")
            print(f"{'tool':<20} {'entries':>10} {'hits':>10}")
            print("-" * 42)
            for t in s["tools"]:
                print(f"{t['tool_name']:<20} {t['entries']:>10} {t['hits']:>10}")
    finally:
        conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Normalize allowlist/denylist into lists when present (start command).
    args.allowlist = _parse_name_list(getattr(args, "allowlist", ""))
    args.denylist = _parse_name_list(getattr(args, "denylist", ""))

    # Ensure cache directory exists for management commands too.
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)

    if args.command == "start":
        return server.start(args)
    if args.command == "clear":
        return cmd_clear(args)
    if args.command == "invalidate":
        return cmd_invalidate(args)
    if args.command == "list":
        return cmd_list(args)
    if args.command == "stats":
        return cmd_stats(args)

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

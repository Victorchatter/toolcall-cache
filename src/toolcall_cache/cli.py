"""CLI entry point for toolcall-cache."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import cache, key, policy, server

DEFAULT_DB = os.path.expanduser("~/.toolcall-cache/toolcall-cache.db")
DEFAULT_TTL = 3600
DEFAULT_DENYLIST = ",".join(policy.DEFAULT_DENYLIST)
DEFAULT_FUZZY_THRESHOLD = 0.85
DEFAULT_FUZZY_WINDOW = 100


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
    start_parser.add_argument(
        "--fuzzy",
        action="store_true",
        help="Enable semantic/fuzzy cache matching after an exact-key miss.",
    )
    start_parser.add_argument(
        "--fuzzy-ignore-keys",
        default="",
        help="Comma-separated argument keys to ignore during fuzzy normalization.",
    )
    start_parser.add_argument(
        "--fuzzy-threshold",
        type=float,
        default=DEFAULT_FUZZY_THRESHOLD,
        help=f"Minimum Levenshtein similarity for a fuzzy hit (default: {DEFAULT_FUZZY_THRESHOLD}).",
    )
    start_parser.add_argument(
        "--fuzzy-window",
        type=int,
        default=DEFAULT_FUZZY_WINDOW,
        help=f"Maximum recent entries to scan for a fuzzy match (default: {DEFAULT_FUZZY_WINDOW}).",
    )

    sub.add_parser("clear", help="Delete all cached entries.", parents=[common])

    invalidate_parser = sub.add_parser("invalidate", help="Delete cached entries for one tool.", parents=[common])
    invalidate_parser.add_argument("tool", help="Tool name to invalidate.")

    sub.add_parser("list", help="List cached entries.", parents=[common])

    stats_parser = sub.add_parser("stats", help="Show cache statistics.", parents=[common])
    stats_parser.add_argument(
        "--watch",
        nargs="?",
        const=2,
        type=float,
        metavar="SECONDS",
        help="Poll the cache and refresh the table every SECONDS seconds (default: 2).",
    )
    stats_parser.add_argument(
        "--no-clear",
        action="store_true",
        help="Do not clear the screen between watch updates (for CI/logging).",
    )

    fuzzy_test_parser = sub.add_parser(
        "fuzzy-test",
        help="Preview whether two argument sets would fuzzy-match.",
    )
    fuzzy_test_parser.add_argument(
        "tool",
        help="Tool name to compare.",
    )
    fuzzy_test_parser.add_argument(
        "args_a",
        help="First JSON object containing tool arguments.",
    )
    fuzzy_test_parser.add_argument(
        "args_b",
        help="Second JSON object containing tool arguments.",
    )
    fuzzy_test_parser.add_argument(
        "--fuzzy-ignore-keys",
        default="",
        help="Comma-separated argument keys to ignore during fuzzy normalization.",
    )
    fuzzy_test_parser.add_argument(
        "--fuzzy-threshold",
        type=float,
        default=DEFAULT_FUZZY_THRESHOLD,
        help=f"Minimum Levenshtein similarity for a fuzzy match (default: {DEFAULT_FUZZY_THRESHOLD}).",
    )

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


def _render_stats_table(s: dict) -> str:
    lines = []
    lines.append(f"Database: {s.get('db', '')}")
    lines.append(f"{'metric':<22} {'value':>10}")
    lines.append("-" * 34)
    lines.append(f"{'Total entries':<22} {s['total_entries']:>10}")
    lines.append(f"{'Hits':<22} {s['total_hits']:>10}")
    lines.append(f"{'Misses':<22} {s['total_misses']:>10}")
    lines.append(f"{'Hit rate %':<22} {s['hit_rate']:>9.1f}%")
    lines.append(f"{'Expiring in 5 min':<22} {s['expiring_soon']:>10}")
    lines.append(f"{'Expired entries':<22} {s['expired_entries']:>10}")
    if s["tools"]:
        lines.append("")
        lines.append("Top 5 tools by hit count:")
        lines.append(f"{'tool':<20} {'entries':>10} {'hits':>10}")
        lines.append("-" * 42)
        for t in s["tools"][:5]:
            lines.append(f"{t['tool_name']:<20} {t['entries']:>10} {t['hits']:>10}")
    return "\n".join(lines) + "\n"


def cmd_stats(args: argparse.Namespace) -> int:
    conn = cache.init_db(args.db)
    try:
        if args.watch:
            interval = max(0.1, args.watch)
            clear = not args.no_clear
            try:
                while True:
                    s = cache.stats(conn)
                    s["db"] = args.db
                    if clear:
                        sys.stdout.write("\033[2J\033[H")
                    sys.stdout.write(_render_stats_table(s))
                    sys.stdout.write(f"\nWatching every {interval:.1f}s. Press Ctrl+C to exit.\n")
                    sys.stdout.flush()
                    time.sleep(interval)
            except KeyboardInterrupt:
                if clear:
                    sys.stdout.write("\033[2J\033[H")
                sys.stdout.write("Stopped watching.\n")
                sys.stdout.flush()
                return 0

        s = cache.stats(conn)
        s["db"] = args.db
        print(_render_stats_table(s), end="")
    finally:
        conn.close()
    return 0


def cmd_fuzzy_test(args: argparse.Namespace) -> int:
    """Preview whether two argument sets would fuzzy-match.

    Normalizes both argument objects using the same rules as the proxy,
    computes the Levenshtein similarity of their canonical JSON strings,
    and reports whether the similarity meets the configured threshold.

    Exit codes:
      0 - the two argument sets fuzzy-match (similarity >= threshold).
      1 - they do not reach the threshold.
      2 - invalid arguments or JSON parse error.
    """
    try:
        args_a = json.loads(args.args_a)
        args_b = json.loads(args.args_b)
    except json.JSONDecodeError as exc:
        print(f"error: invalid JSON argument: {exc}", file=sys.stderr)
        return 2
    if not isinstance(args_a, dict) or not isinstance(args_b, dict):
        print("error: both argument sets must be JSON objects", file=sys.stderr)
        return 2

    ignore_keys = frozenset(args.fuzzy_ignore_keys)
    json_a = key.make_normalized_json(args_a, ignore_keys)
    json_b = key.make_normalized_json(args_b, ignore_keys)
    score = key.levenshtein_ratio(json_a, json_b)
    match = score >= args.fuzzy_threshold

    print(f"tool: {args.tool}")
    print(f"threshold: {args.fuzzy_threshold}")
    print(f"similarity: {score:.2f}")
    print(f"match: {'yes' if match else 'no'}")
    return 0 if match else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Normalize allowlist/denylist into lists when present (start command).
    args.allowlist = _parse_name_list(getattr(args, "allowlist", ""))
    args.denylist = _parse_name_list(getattr(args, "denylist", ""))
    args.fuzzy_ignore_keys = _parse_name_list(getattr(args, "fuzzy_ignore_keys", ""))

    # Ensure cache directory exists for commands that touch the database.
    if hasattr(args, "db"):
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
    if args.command == "fuzzy-test":
        return cmd_fuzzy_test(args)

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

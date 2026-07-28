"""Wiring that turns CLI arguments into a running proxy."""

from __future__ import annotations

import argparse
import os
import sys

from .proxy import FuzzyConfig
from .transports.http import run_http_proxy
from .transports.stdio import run_stdio_proxy


def start(args: argparse.Namespace) -> int:
    """Start the requested transport and block until it exits."""
    fuzzy_config = FuzzyConfig(
        enabled=getattr(args, "fuzzy", False),
        ignore_keys=frozenset(getattr(args, "fuzzy_ignore_keys", [])),
        threshold=getattr(args, "fuzzy_threshold", 0.85),
        window=getattr(args, "fuzzy_window", 100),
    )

    if args.transport == "stdio":
        if not args.upstream:
            print("error: --upstream is required for stdio transport", file=sys.stderr)
            return 2
        run_stdio_proxy(
            upstream_command=args.upstream,
            db_path=args.db,
            server_id=args.server_id,
            allowlist=args.allowlist,
            denylist=args.denylist,
            ttl=args.ttl,
            fuzzy_config=fuzzy_config,
        )
    elif args.transport == "http":
        if not args.upstream:
            print("error: --upstream is required for http transport", file=sys.stderr)
            return 2
        run_http_proxy(
            upstream_base_url=args.upstream,
            host=args.host,
            port=args.port,
            db_path=args.db,
            server_id=args.server_id,
            allowlist=args.allowlist,
            denylist=args.denylist,
            ttl=args.ttl,
            fuzzy_config=fuzzy_config,
        )
    else:
        print(f"error: unknown transport {args.transport}", file=sys.stderr)
        return 2
    return 0

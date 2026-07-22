"""SQLite-backed content-addressed cache for MCP tool results."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from .key import canonical_json


def init_db(path: str) -> sqlite3.Connection:
    """Open or create the SQLite cache database and return a connection."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    _ensure_schema(conn)
    return conn


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS cache_entries (
    key_hash TEXT PRIMARY KEY,
    server_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    args_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    hit_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cache_tool ON cache_entries(tool_name);
CREATE INDEX IF NOT EXISTS idx_cache_expires ON cache_entries(expires_at);
"""


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.commit()


def get(conn: sqlite3.Connection, key_hash: str, now: float | None = None) -> dict[str, Any] | None:
    """Return a cached result if it exists and has not expired."""
    now = now if now is not None else time.time()
    row = conn.execute(
        "SELECT result_json, expires_at, hit_count FROM cache_entries WHERE key_hash = ?",
        (key_hash,),
    ).fetchone()
    if row is None:
        return None
    result_json, expires_at, hit_count = row
    if now > expires_at:
        return None
    conn.execute(
        "UPDATE cache_entries SET hit_count = hit_count + 1 WHERE key_hash = ?",
        (key_hash,),
    )
    conn.commit()
    return {
        "result": json.loads(result_json),
        "expires_at": expires_at,
        "hit_count": hit_count + 1,
    }


def put(
    conn: sqlite3.Connection,
    key_hash: str,
    server_id: str,
    tool_name: str,
    args_hash: str,
    result: dict[str, Any],
    ttl: float,
    now: float | None = None,
) -> None:
    """Store a result in the cache with a TTL in seconds."""
    now = now if now is not None else time.time()
    expires_at = now + ttl
    conn.execute(
        """
        INSERT INTO cache_entries (key_hash, server_id, tool_name, args_hash, result_json, created_at, expires_at, hit_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, 0)
        ON CONFLICT(key_hash) DO UPDATE SET
            result_json = excluded.result_json,
            created_at = excluded.created_at,
            expires_at = excluded.expires_at,
            hit_count = 0
        """,
        (
            key_hash,
            server_id,
            tool_name,
            args_hash,
            canonical_json(result),
            now,
            expires_at,
        ),
    )
    conn.commit()


def clear(conn: sqlite3.Connection) -> int:
    """Delete every cached entry. Returns rows deleted."""
    cur = conn.execute("DELETE FROM cache_entries")
    conn.commit()
    return cur.rowcount


def invalidate_tool(conn: sqlite3.Connection, tool_name: str) -> int:
    """Delete all cached entries for a single tool name."""
    cur = conn.execute("DELETE FROM cache_entries WHERE tool_name = ?", (tool_name,))
    conn.commit()
    return cur.rowcount


def prune_expired(conn: sqlite3.Connection, now: float | None = None) -> int:
    """Delete expired entries. Returns rows deleted."""
    now = now if now is not None else time.time()
    cur = conn.execute("DELETE FROM cache_entries WHERE expires_at < ?", (now,))
    conn.commit()
    return cur.rowcount


def list_entries(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return a human-readable list of cached entries."""
    rows = conn.execute(
        "SELECT key_hash, server_id, tool_name, args_hash, created_at, expires_at, hit_count FROM cache_entries ORDER BY created_at DESC"
    ).fetchall()
    return [
        {
            "key_hash": row[0],
            "server_id": row[1],
            "tool_name": row[2],
            "args_hash": row[3],
            "created_at": row[4],
            "expires_at": row[5],
            "hit_count": row[6],
        }
        for row in rows
    ]


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return aggregate cache statistics."""
    now = time.time()
    total_entries = conn.execute("SELECT COUNT(*) FROM cache_entries").fetchone()[0]
    expired_entries = conn.execute(
        "SELECT COUNT(*) FROM cache_entries WHERE expires_at < ?", (now,)
    ).fetchone()[0]
    total_hits = conn.execute("SELECT COALESCE(SUM(hit_count), 0) FROM cache_entries").fetchone()[0]
    tool_rows = conn.execute(
        "SELECT tool_name, COUNT(*), SUM(hit_count) FROM cache_entries GROUP BY tool_name"
    ).fetchall()
    return {
        "total_entries": total_entries,
        "expired_entries": expired_entries,
        "total_hits": total_hits,
        "tools": [
            {"tool_name": row[0], "entries": row[1], "hits": row[2] or 0}
            for row in tool_rows
        ],
    }

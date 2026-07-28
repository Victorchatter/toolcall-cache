"""SQLite-backed content-addressed cache for MCP tool results."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from .key import canonical_json, levenshtein_ratio, make_normalized_json


def init_db(path: str) -> sqlite3.Connection:
    """Open or create the SQLite cache database and return a connection."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    _ensure_schema(conn)
    _migrate_schema(conn)
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
    hit_count INTEGER NOT NULL DEFAULT 0,
    normalized_args TEXT NOT NULL DEFAULT '',
    tool_signature TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_cache_tool ON cache_entries(tool_name);
CREATE INDEX IF NOT EXISTS idx_cache_expires ON cache_entries(expires_at);
CREATE INDEX IF NOT EXISTS idx_cache_server_tool_created ON cache_entries(server_id, tool_name, created_at);
"""


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.commit()


def _table_columns(conn: sqlite3.Connection) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(cache_entries)")}


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the initial release."""
    columns = _table_columns(conn)
    if "normalized_args" not in columns:
        conn.execute("ALTER TABLE cache_entries ADD COLUMN normalized_args TEXT NOT NULL DEFAULT ''")
    if "tool_signature" not in columns:
        conn.execute("ALTER TABLE cache_entries ADD COLUMN tool_signature TEXT NOT NULL DEFAULT ''")
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


def fuzzy_lookup(
    conn: sqlite3.Connection,
    server_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    threshold: float,
    window: int,
    ignore_keys: set[str] | frozenset[str] | None = None,
    now: float | None = None,
) -> dict[str, Any] | None:
    """Scan the last ``window`` entries for ``tool_name`` and return the best
    fuzzy match above ``threshold``.

    Similarity is computed with Levenshtein ratio over the canonical JSON of
    normalized arguments.
    """
    now = now if now is not None else time.time()
    window = max(1, int(window))
    threshold = float(threshold)
    target_json = make_normalized_json(arguments, ignore_keys)

    rows = conn.execute(
        """
        SELECT key_hash, normalized_args, result_json, expires_at, hit_count
        FROM cache_entries
        WHERE server_id = ? AND tool_name = ? AND expires_at >= ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (server_id, tool_name, now, window),
    ).fetchall()

    best: dict[str, Any] | None = None
    best_score = 0.0
    for key_hash, normalized_args, result_json, expires_at, hit_count in rows:
        if not normalized_args:
            continue
        score = levenshtein_ratio(target_json, normalized_args)
        if score >= threshold and score > best_score:
            best = {
                "key_hash": key_hash,
                "score": score,
                "result": json.loads(result_json),
                "expires_at": expires_at,
                "hit_count": hit_count,
            }
            best_score = score

    if best is not None:
        conn.execute(
            "UPDATE cache_entries SET hit_count = hit_count + 1 WHERE key_hash = ?",
            (best["key_hash"],),
        )
        conn.commit()
        best["hit_count"] = best["hit_count"] + 1
    return best


def put(
    conn: sqlite3.Connection,
    key_hash: str,
    server_id: str,
    tool_name: str,
    args_hash: str,
    result: dict[str, Any],
    ttl: float,
    normalized_args_json: str | None = None,
    tool_signature: str | None = None,
    now: float | None = None,
) -> None:
    """Store a result in the cache with a TTL in seconds."""
    now = now if now is not None else time.time()
    expires_at = now + ttl
    if normalized_args_json is None:
        normalized_args_json = ""
    if tool_signature is None:
        tool_signature = tool_name
    conn.execute(
        """
        INSERT INTO cache_entries (
            key_hash, server_id, tool_name, args_hash, result_json,
            created_at, expires_at, hit_count, normalized_args, tool_signature
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
        ON CONFLICT(key_hash) DO UPDATE SET
            result_json = excluded.result_json,
            created_at = excluded.created_at,
            expires_at = excluded.expires_at,
            hit_count = 0,
            normalized_args = excluded.normalized_args,
            tool_signature = excluded.tool_signature
        """,
        (
            key_hash,
            server_id,
            tool_name,
            args_hash,
            canonical_json(result),
            now,
            expires_at,
            normalized_args_json,
            tool_signature,
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
    expiring_soon = conn.execute(
        "SELECT COUNT(*) FROM cache_entries WHERE expires_at >= ? AND expires_at <= ?",
        (now, now + 300),
    ).fetchone()[0]
    total_hits = conn.execute("SELECT COALESCE(SUM(hit_count), 0) FROM cache_entries").fetchone()[0]
    tool_rows = conn.execute(
        "SELECT tool_name, COUNT(*), SUM(hit_count) FROM cache_entries GROUP BY tool_name ORDER BY SUM(hit_count) DESC"
    ).fetchall()
    total_misses = total_entries  # every entry was a miss before it was cached
    hit_rate = (total_hits / (total_hits + total_misses) * 100.0) if (total_hits + total_misses) else 0.0
    return {
        "total_entries": total_entries,
        "expired_entries": expired_entries,
        "expiring_soon": expiring_soon,
        "total_hits": total_hits,
        "total_misses": total_misses,
        "hit_rate": hit_rate,
        "tools": [
            {"tool_name": row[0], "entries": row[1], "hits": row[2] or 0}
            for row in tool_rows
        ],
    }

"""SQLite-backed store for one-shot inference results."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

# A claim older than this is presumed abandoned even if its process is still alive.
CLAIM_TTL = 3600.0

# Columns added after the first release, with their declarations. Databases created by an
# older version get them on open.
ADDED_COLUMNS = {
    "results": {
        "hits": "INTEGER NOT NULL DEFAULT 0",  # calls answered by this entry without a model call
        "last_hit_at": "REAL",
        # The CLI's JSON result for the call that produced the answer (usage, cost, timings),
        # minus the answer itself, which is in `text`.
        "response": "TEXT",
    },
    "pending": {
        "error": "TEXT",  # set when the claiming call failed, so waiters fail with it
        "failed_at": "REAL",
    },
}


# Fields of the CLI result that repeat the answer already stored in `text`.
ANSWER_FIELDS = ("result", "structured_output")


def default_cache_dir() -> Path:
    for var in ("CHEAPSHOT_CACHE_DIR", "CLAUDE_PLUGIN_DATA"):
        if value := os.environ.get(var):
            return Path(value)
    xdg = os.environ.get("XDG_CACHE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".cache") / "cheapshot"


def cache_key(**request: object) -> str:
    """Stable hash of every request field that can change the answer.

    Changing the serialization here changes every key, which empties the cache in effect.
    """
    canonical = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class Entry:
    key: str
    text: str
    model: str
    created_at: float


class Cache:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS results (
                key TEXT PRIMARY KEY,
                request TEXT NOT NULL,
                text TEXT NOT NULL,
                model TEXT NOT NULL,
                created_at REAL NOT NULL
            )"""
        )
        # Keys some process is currently computing, so concurrent sessions wait instead of
        # paying for the same request twice.
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS pending (
                key TEXT PRIMARY KEY,
                token TEXT NOT NULL,
                pid INTEGER NOT NULL,
                started_at REAL NOT NULL
            )"""
        )
        for table, columns in ADDED_COLUMNS.items():
            existing = {row[1] for row in self._db.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns.items():
                if name not in existing:
                    try:
                        self._db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                    except sqlite3.OperationalError as exc:
                        # Another process opening the same file added it first.
                        if "duplicate column" not in str(exc):
                            raise
        self._db.commit()

    def get(self, key: str) -> Entry | None:
        row = self._db.execute(
            "SELECT key, text, model, created_at FROM results WHERE key = ?", (key,)
        ).fetchone()
        return Entry(*row) if row else None

    def record_hit(self, key: str) -> None:
        self._db.execute(
            "UPDATE results SET hits = hits + 1, last_hit_at = ? WHERE key = ?", (time.time(), key)
        )
        self._db.commit()

    def put(self, key: str, request: dict, text: str, model: str, response: dict | None = None) -> Entry:
        entry = Entry(key, text, model, time.time())
        # Upsert rather than replace so a refreshed entry keeps its hit count.
        self._db.execute(
            """INSERT INTO results (key, request, text, model, created_at, response)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET request = excluded.request, text = excluded.text,
                   model = excluded.model, created_at = excluded.created_at,
                   response = excluded.response""",
            (
                key,
                json.dumps(request, sort_keys=True),
                text,
                model,
                entry.created_at,
                json.dumps({k: v for k, v in response.items() if k not in ANSWER_FIELDS})
                if response is not None
                else None,
            ),
        )
        self._db.commit()
        return entry

    def claim(self, key: str) -> str | None:
        """Mark `key` as being computed; return a release token, or None if someone else is."""
        now = time.time()
        # IMMEDIATE takes the write lock up front, so two processes can't both see no claim.
        self._db.execute("BEGIN IMMEDIATE")
        try:
            row = self._db.execute(
                "SELECT pid, started_at, error FROM pending WHERE key = ?", (key,)
            ).fetchone()
            # A failed claim is over; a new caller retries rather than inheriting an old error.
            if row and row[2] is None and _alive(row[0]) and now - row[1] < CLAIM_TTL:
                self._db.rollback()
                return None
            token = secrets.token_hex(8)
            self._db.execute(
                "INSERT OR REPLACE INTO pending (key, token, pid, started_at) VALUES (?, ?, ?, ?)",
                (key, token, os.getpid(), now),
            )
            # Failures only matter to callers already waiting; drop ones nobody could still be.
            self._db.execute(
                "DELETE FROM pending WHERE error IS NOT NULL AND failed_at < ?", (now - CLAIM_TTL,)
            )
            self._db.commit()
            return token
        except BaseException:
            self._db.rollback()
            raise

    def release(self, key: str, token: str, error: str | None = None) -> None:
        """End a claim. With `error`, leave it behind so current waiters fail the same way."""
        if error is None:
            self._db.execute("DELETE FROM pending WHERE key = ? AND token = ?", (key, token))
        else:
            self._db.execute(
                "UPDATE pending SET error = ?, failed_at = ? WHERE key = ? AND token = ?",
                (error, time.time(), key, token),
            )
        self._db.commit()

    def failure(self, key: str, since: float) -> str | None:
        """The error of a claim on `key` that failed at or after `since`, if any."""
        row = self._db.execute(
            "SELECT error FROM pending WHERE key = ? AND error IS NOT NULL AND failed_at >= ?",
            (key, since),
        ).fetchone()
        return row[0] if row else None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True

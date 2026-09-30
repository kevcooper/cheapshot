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


def default_cache_dir() -> Path:
    for var in ("CHEAPSHOT_CACHE_DIR", "CLAUDE_PLUGIN_DATA"):
        if value := os.environ.get(var):
            return Path(value)
    xdg = os.environ.get("XDG_CACHE_HOME")
    return (Path(xdg) if xdg else Path.home() / ".cache") / "cheapshot"


def cache_key(**request: object) -> str:
    """Stable hash of every request field that can change the answer."""
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
        self._db.commit()

    def get(self, key: str) -> Entry | None:
        row = self._db.execute(
            "SELECT key, text, model, created_at FROM results WHERE key = ?", (key,)
        ).fetchone()
        return Entry(*row) if row else None

    def put(self, key: str, request: dict, text: str, model: str) -> Entry:
        entry = Entry(key, text, model, time.time())
        self._db.execute(
            "INSERT OR REPLACE INTO results VALUES (?, ?, ?, ?, ?)",
            (key, json.dumps(request, sort_keys=True), text, model, entry.created_at),
        )
        self._db.commit()
        return entry

    def claim(self, key: str) -> str | None:
        """Mark `key` as being computed; return a release token, or None if someone else is."""
        now = time.time()
        # IMMEDIATE takes the write lock up front, so two processes can't both see no claim.
        self._db.execute("BEGIN IMMEDIATE")
        try:
            row = self._db.execute("SELECT pid, started_at FROM pending WHERE key = ?", (key,)).fetchone()
            if row and _alive(row[0]) and now - row[1] < CLAIM_TTL:
                self._db.rollback()
                return None
            token = secrets.token_hex(8)
            self._db.execute(
                "INSERT OR REPLACE INTO pending VALUES (?, ?, ?, ?)", (key, token, os.getpid(), now)
            )
            self._db.commit()
            return token
        except BaseException:
            self._db.rollback()
            raise

    def release(self, key: str, token: str) -> None:
        self._db.execute("DELETE FROM pending WHERE key = ? AND token = ?", (key, token))
        self._db.commit()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True

"""SQLite-backed store for one-shot inference results."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path


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

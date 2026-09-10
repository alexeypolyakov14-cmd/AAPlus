"""Простой кэш HTTP-ответов в SQLite с TTL."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any, Optional


class SqliteCache:
    def __init__(self, path: str = "data/cache/http_cache.sqlite"):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, ts REAL, payload TEXT)")
        self.conn.commit()

    def get(self, key: str, ttl: float) -> Optional[Any]:
        row = self.conn.execute("SELECT ts, payload FROM cache WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        ts, payload = row
        if ttl >= 0 and time.time() - ts > ttl:
            return None
        return json.loads(payload)

    def set(self, key: str, value: Any) -> None:
        self.conn.execute("INSERT OR REPLACE INTO cache (key, ts, payload) VALUES (?, ?, ?)",
                          (key, time.time(), json.dumps(value, ensure_ascii=False)))
        self.conn.commit()

    def clear(self) -> None:
        self.conn.execute("DELETE FROM cache")
        self.conn.commit()


class NullCache:
    def get(self, key: str, ttl: float):
        return None

    def set(self, key: str, value: Any) -> None:
        pass

    def clear(self) -> None:
        pass

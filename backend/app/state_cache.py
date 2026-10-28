"""Compressed cache for intermediate reasoning states."""
import hashlib
import json
import time
import zlib

from .db import connect


def make_key(text: str) -> str:
    return hashlib.sha256(" ".join(text.lower().split()).encode()).hexdigest()


def put(key: str, state: dict) -> None:
    blob = zlib.compress(json.dumps(state).encode())
    with connect() as conn:
        conn.execute(
            "INSERT INTO state_cache(key, value, created_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, created_at=excluded.created_at",
            (key, blob, time.time()),
        )


def get(key: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT value FROM state_cache WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE state_cache SET hits = hits + 1 WHERE key=?", (key,))
    return json.loads(zlib.decompress(row["value"]))

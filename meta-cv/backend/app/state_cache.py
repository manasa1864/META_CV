"""Compressed cache for verified paths and model-verified claims.

Entries can expire (ttl) and can declare which graph triples they depend on; a write to the graph
calls invalidate_triples() so dependent entries are dropped instead of served stale.
"""
import hashlib
import json
import time
import zlib

from .db import connect


def make_key(text: str) -> str:
    return hashlib.sha256(" ".join(text.lower().split()).encode()).hexdigest()


def put(key: str, state: dict, ttl: float | None = None, triples: list[str] | None = None) -> None:
    blob = zlib.compress(json.dumps(state).encode())
    now = time.time()
    with connect() as conn:
        conn.execute(
            "INSERT INTO state_cache(key, value, created_at, expires_at) VALUES (?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, created_at=excluded.created_at, "
            "expires_at=excluded.expires_at",
            (key, blob, now, now + ttl if ttl else None),
        )
        conn.execute("DELETE FROM claim_index WHERE key=?", (key,))
        conn.executemany("INSERT OR IGNORE INTO claim_index(triple, key) VALUES (?,?)",
                         [(t.lower(), key) for t in triples or []])


def delete(key: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM state_cache WHERE key=?", (key,))
        conn.execute("DELETE FROM claim_index WHERE key=?", (key,))


def get(key: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT value, expires_at FROM state_cache WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        if row["expires_at"] is not None and row["expires_at"] < time.time():
            conn.execute("DELETE FROM state_cache WHERE key=?", (key,))
            return None
        conn.execute("UPDATE state_cache SET hits = hits + 1 WHERE key=?", (key,))
    return json.loads(zlib.decompress(row["value"]))


def invalidate_triples(triples: list[str]) -> int:
    """Drop every cache entry that depends on one of these "subject|relation|object" triples
    (or on any triple of the same subject and relation, since a new value can contradict it)."""
    if not triples:
        return 0
    with connect() as conn:
        keys = set()
        for t in triples:
            s, r, _ = (t.lower().split("|") + ["", ""])[:3]
            keys |= {row["key"] for row in conn.execute(
                "SELECT key FROM claim_index WHERE triple = ? OR triple LIKE ?", (t.lower(), f"{s}|{r}|%"))}
        for k in keys:
            conn.execute("DELETE FROM state_cache WHERE key=?", (k,))
            conn.execute("DELETE FROM claim_index WHERE key=?", (k,))
    return len(keys)


def prune(max_age_days: float) -> int:
    with connect() as conn:
        n = conn.execute("DELETE FROM state_cache WHERE (expires_at IS NOT NULL AND expires_at < ?) OR created_at < ?",
                         (time.time(), time.time() - max_age_days * 86400)).rowcount
        conn.execute("DELETE FROM claim_index WHERE key NOT IN (SELECT key FROM state_cache)")
    return n

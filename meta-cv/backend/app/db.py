import sqlite3
from contextlib import contextmanager

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    subject  TEXT NOT NULL,
    relation TEXT NOT NULL,
    object   TEXT NOT NULL,
    PRIMARY KEY (subject, relation, object)
);
CREATE TABLE IF NOT EXISTS relations (
    name       TEXT PRIMARY KEY,
    functional INTEGER NOT NULL DEFAULT 0,  -- subject has at most one object
    transitive INTEGER NOT NULL DEFAULT 0   -- a->b and b->c implies a->c
);
CREATE TABLE IF NOT EXISTS rule_proposals (
    relation   TEXT PRIMARY KEY,
    functional INTEGER NOT NULL,
    transitive INTEGER NOT NULL,
    confidence REAL NOT NULL,
    rationale  TEXT,
    status     TEXT NOT NULL,               -- pending | approved | auto | rejected
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS review_queue (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    subject    TEXT NOT NULL,
    relation   TEXT NOT NULL,
    object     TEXT NOT NULL,
    source     TEXT,
    reason     TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',  -- pending | accepted | rejected
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS state_cache (
    key        TEXT PRIMARY KEY,
    value      BLOB NOT NULL,               -- zlib-compressed JSON
    hits       INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
"""


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)

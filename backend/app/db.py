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
CREATE TABLE IF NOT EXISTS turns (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id    TEXT,
    question           TEXT NOT NULL,
    hallucination_type TEXT NOT NULL,       -- classifier output: factual | reasoning | consistency
    tier               TEXT NOT NULL,       -- tier that produced the answer
    mode               TEXT,
    confidence         TEXT NOT NULL,
    from_cache         INTEGER NOT NULL DEFAULT 0,
    reused_state       INTEGER NOT NULL DEFAULT 0,
    caught             TEXT NOT NULL,       -- JSON list of hallucinations caught before output
    prompt_tokens      INTEGER NOT NULL DEFAULT 0,
    completion_tokens  INTEGER NOT NULL DEFAULT 0,
    saved_tokens       INTEGER NOT NULL DEFAULT 0,
    answer             TEXT NOT NULL,
    commitments        TEXT NOT NULL,       -- JSON: verified statements later turns must stay consistent with
    correct            INTEGER,             -- benchmark ground truth (1/0)
    feedback           INTEGER,             -- user verdict (1/0)
    source             TEXT NOT NULL DEFAULT 'chat',  -- chat | bench
    created_at         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS turns_conversation ON turns(conversation_id, id);
CREATE TABLE IF NOT EXISTS reasoning_states (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tier       TEXT NOT NULL,
    question   TEXT NOT NULL,
    terms      TEXT NOT NULL,               -- content words used to find similar questions
    state      BLOB NOT NULL,               -- zlib-compressed JSON: verified triples or checked steps
    tokens     INTEGER NOT NULL,            -- what producing the state cost
    hits       INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS bench_runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    summary    TEXT NOT NULL,
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

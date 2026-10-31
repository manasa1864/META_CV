"""SQLite: the fact graph in sqlite mode, and in every mode all non-graph state
(turns, caches, reasoning states, review queue, quarantine, rule proposals, bench runs)."""
import shutil
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from . import config

GRAPH_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    subject     TEXT NOT NULL,
    relation    TEXT NOT NULL,
    object      TEXT NOT NULL,
    source      TEXT,
    verified_at REAL,
    added_by    TEXT,
    batch_id    TEXT,
    PRIMARY KEY (subject, relation, object)
);
CREATE INDEX IF NOT EXISTS facts_relation_subject ON facts(relation, subject);
CREATE INDEX IF NOT EXISTS facts_object ON facts(object);
CREATE TABLE IF NOT EXISTS relations (
    name       TEXT PRIMARY KEY,
    functional INTEGER NOT NULL DEFAULT 0,  -- subject has at most one object
    transitive INTEGER NOT NULL DEFAULT 0,  -- a->b and b->c implies a->c
    acyclic    INTEGER NOT NULL DEFAULT 0   -- no chain may lead back to its start
);
CREATE TABLE IF NOT EXISTS aliases (
    alias     TEXT PRIMARY KEY,
    canonical TEXT NOT NULL
);
"""

STATE_SCHEMA = """
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
CREATE TABLE IF NOT EXISTS quarantine (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    subject    TEXT NOT NULL,
    relation   TEXT NOT NULL,
    object     TEXT NOT NULL,
    source     TEXT,
    reason     TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS state_cache (
    key        TEXT PRIMARY KEY,
    value      BLOB NOT NULL,               -- zlib-compressed JSON
    hits       INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    expires_at REAL                         -- NULL = no expiry
);
CREATE TABLE IF NOT EXISTS claim_index (     -- which cache keys depend on which triple
    triple TEXT NOT NULL,                   -- "subject|relation|object"
    key    TEXT NOT NULL,
    PRIMARY KEY (triple, key)
);
CREATE TABLE IF NOT EXISTS turns (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id    TEXT,
    question           TEXT NOT NULL,
    hallucination_type TEXT NOT NULL,       -- primary risk family: factual | reasoning | consistency
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
    correct            INTEGER,             -- legacy benchmark flag (1/0)
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
CREATE TABLE IF NOT EXISTS usage_daily (
    day    TEXT PRIMARY KEY,
    tokens INTEGER NOT NULL DEFAULT 0
);
"""

# (table, column, declaration) added to databases created before the column existed
MIGRATIONS = [
    ("facts", "source", "TEXT"),
    ("facts", "verified_at", "REAL"),
    ("facts", "added_by", "TEXT"),
    ("facts", "batch_id", "TEXT"),
    ("relations", "acyclic", "INTEGER NOT NULL DEFAULT 0"),
    ("state_cache", "expires_at", "REAL"),
    ("turns", "outcome", "TEXT"),            # correct | abstained | hallucinated | unknown
    ("turns", "signal_quality", "TEXT"),     # ground_truth | user | self
    ("turns", "risks", "TEXT"),              # JSON risk profile from the classifier
    ("turns", "stopped", "TEXT"),            # why the system stopped instead of answering, if it did
    ("turns", "degraded", "TEXT"),
    ("turns", "scope", "TEXT"),              # whose feedback this is (conversation scope)
    ("turns", "reuse_kind", "TEXT"),         # exact_repeat | state_answer | template | similar_state
    ("turns", "compression_context", "TEXT"),  # JSON: facts/relations kept and cut, redrafts, history expansion
]


@contextmanager
def connect(path: str | None = None):
    conn = sqlite3.connect(path or config.DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate(conn) -> None:
    for table, column, decl in MIGRATIONS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if cols and column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_graph_db(path: str) -> None:
    with connect(path) as conn:
        conn.executescript(GRAPH_SCHEMA)
        _migrate(conn)


def init_db() -> None:
    """Create or upgrade the state database. A fresh deployment without a persistent disk starts from the
    bundled initial state (router table and bench results from real runs) when one is shipped."""
    db = Path(config.DB_PATH)
    if not db.exists() and Path(config.INITIAL_STATE_DB).exists():
        db.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(config.INITIAL_STATE_DB, db)
    with connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(STATE_SCHEMA + GRAPH_SCHEMA)
        _migrate(conn)

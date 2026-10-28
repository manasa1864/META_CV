from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from . import ingest as ingest_mod, llm, state_cache
from .db import connect, init_db
from .graph_store import get_store

store = get_store()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="META", lifespan=lifespan)


class Fact(BaseModel):
    subject: str
    relation: str
    object: str
    source: str | None = None  # where the fact came from (document, URL, ...)


class RelationDef(BaseModel):
    name: str
    functional: bool = False
    transitive: bool = False


class CacheEntry(BaseModel):
    query: str
    state: dict


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/relations")
def define_relation(r: RelationDef):
    store.define_relation(r.name.lower(), r.functional, r.transitive)
    ingest_mod.record_manual_rule(r.name.lower(), r.functional, r.transitive)
    return {"ok": True}


@app.post("/facts")
def add_fact(f: Fact):
    store.add_fact(f.subject, f.relation, f.object, f.source)
    return {"ok": True}


@app.get("/relations")
def relation_stats():
    return store.relation_stats()


@app.get("/connections")
def connections(a: str, b: str):
    return store.connections(a, b)


@app.post("/validate")
def validate(f: Fact):
    return store.validate(f.subject, f.relation, f.object)


@app.post("/cache")
def cache_put(e: CacheEntry):
    key = state_cache.make_key(e.query)
    state_cache.put(key, e.state)
    return {"key": key}


@app.get("/cache")
def cache_get(query: str):
    state = state_cache.get(state_cache.make_key(query))
    if state is None:
        raise HTTPException(404, "cache miss")
    return state


class IngestRequest(BaseModel):
    text: str
    source: str | None = None


class RuleDecision(BaseModel):
    functional: bool | None = None  # override the proposal when approving
    transitive: bool | None = None


@app.post("/ingest")
def ingest_text(req: IngestRequest):
    try:
        return ingest_mod.ingest(store, req.text, req.source)
    except llm.LLMNotConfigured as e:
        raise HTTPException(503, str(e))
    except llm.LLMBadOutput as e:
        raise HTTPException(502, str(e))


@app.get("/rules/pending")
def pending_rules():
    with connect() as conn:
        rows = conn.execute("SELECT * FROM rule_proposals WHERE status='pending' ORDER BY created_at").fetchall()
    return [dict(r) for r in rows]


def _rule(relation: str):
    with connect() as conn:
        row = conn.execute("SELECT * FROM rule_proposals WHERE relation=?", (relation,)).fetchone()
    if row is None:
        raise HTTPException(404, "no proposal for that relation")
    return row


@app.post("/rules/{relation}/approve")
def approve_rule(relation: str, d: RuleDecision | None = None):
    row = _rule(relation)
    functional = bool(row["functional"]) if d is None or d.functional is None else d.functional
    transitive = bool(row["transitive"]) if d is None or d.transitive is None else d.transitive
    store.define_relation(relation, functional, transitive)
    with connect() as conn:
        conn.execute(
            "UPDATE rule_proposals SET status='approved', functional=?, transitive=? WHERE relation=?",
            (int(functional), int(transitive), relation),
        )
    return {"relation": relation, "functional": functional, "transitive": transitive, "status": "approved"}


@app.post("/rules/{relation}/reject")
def reject_rule(relation: str):
    _rule(relation)
    store.define_relation(relation, False, False)
    with connect() as conn:
        conn.execute("UPDATE rule_proposals SET status='rejected' WHERE relation=?", (relation,))
    return {"relation": relation, "status": "rejected"}


@app.get("/review")
def review_queue():
    with connect() as conn:
        rows = conn.execute("SELECT * FROM review_queue WHERE status='pending' ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def _queued(item_id: int):
    with connect() as conn:
        row = conn.execute("SELECT * FROM review_queue WHERE id=? AND status='pending'", (item_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "no pending item with that id")
    return row


@app.post("/review/{item_id}/accept")
def accept_item(item_id: int):
    row = _queued(item_id)
    store.add_fact(row["subject"], row["relation"], row["object"], row["source"])
    with connect() as conn:
        conn.execute("UPDATE review_queue SET status='accepted' WHERE id=?", (item_id,))
    return {"id": item_id, "status": "accepted"}


@app.post("/review/{item_id}/reject")
def reject_item(item_id: int):
    _queued(item_id)
    with connect() as conn:
        conn.execute("UPDATE review_queue SET status='rejected' WHERE id=?", (item_id,))
    return {"id": item_id, "status": "rejected"}

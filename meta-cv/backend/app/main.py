"""HTTP API. In development Vite proxies /api/* here with the prefix stripped; in production
app.server mounts this app under /api next to the built frontend, so the paths match.

JUDGE_MODE=1: the graph is read-only (writes need the X-Admin-Token header), the router is frozen,
chat is rate limited per IP, and a concurrency cap and a daily token budget apply. Error responses
carry a short code and a safe message; details go to the server log only.
"""
import json
import logging
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import audit, config, graph_guard, ingest as ingest_mod, llm, metrics, state_cache
from .db import connect, init_db
from .engine import pipeline
from .engine.health import health
from .examples import EXAMPLES
from .graph_common import GraphLimitError, GraphUnavailable, ReadOnlyGraph
from .graph_store import get_store

log = logging.getLogger(__name__)
store = get_store()


def startup() -> None:
    init_db()


@asynccontextmanager
async def lifespan(_: FastAPI):
    startup()
    yield


app = FastAPI(title="META", lifespan=lifespan)


# ------------------------------------------------------------------------------------- safety
@app.exception_handler(ReadOnlyGraph)
async def _read_only(_: Request, e: ReadOnlyGraph):
    return JSONResponse({"code": "read_only", "message": pipeline.ERRORS["read_only"]}, status_code=403)


@app.exception_handler(GraphUnavailable)
async def _graph_down(_: Request, e: GraphUnavailable):
    log.warning("graph unavailable: %s", e)
    return JSONResponse({"code": "graph_unavailable", "message": pipeline.ERRORS["graph_unavailable"]}, status_code=503)


@app.exception_handler(GraphLimitError)
async def _graph_limit(_: Request, e: GraphLimitError):
    log.warning("graph limit: %s", e)
    return JSONResponse({"code": "graph_limit", "message": pipeline.ERRORS["graph_limit"]}, status_code=507)


@app.exception_handler(Exception)
async def _internal(_: Request, e: Exception):
    log.exception("request failed")
    return JSONResponse({"code": "internal", "message": pipeline.ERRORS["internal"]}, status_code=500)


def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    """Writes: open in development; with ADMIN_TOKEN set they need it; in judge mode without a token they are closed."""
    if config.ADMIN_TOKEN:
        if x_admin_token != config.ADMIN_TOKEN:
            raise HTTPException(403, "this endpoint needs an admin token")
    elif config.JUDGE_MODE:
        raise HTTPException(403, "this deployment is read-only")


_hits: dict = defaultdict(deque)
_hits_lock = threading.Lock()
_slots = threading.BoundedSemaphore(max(1, config.MAX_CONCURRENT))


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")


def chat_guard(request: Request) -> None:
    """Rate limit (judge mode) and the global daily token budget."""
    if config.JUDGE_MODE:
        now, ip = time.time(), _client_ip(request)
        with _hits_lock:
            q = _hits[ip]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= config.RATE_LIMIT_PER_MIN:
                raise HTTPException(429, "Too many questions in a minute - please wait a moment.")
            q.append(now)
    with connect() as conn:
        row = conn.execute("SELECT tokens FROM usage_daily WHERE day=?", (time.strftime("%Y-%m-%d"),)).fetchone()
    if row and row["tokens"] >= config.DAILY_TOKEN_BUDGET:
        raise HTTPException(429, "Today's question budget is used up. The recorded examples and results still work.")


# ------------------------------------------------------------------------------------- health
@app.get("/health")
@app.get("/healthz")
def healthz():
    return {"status": "ok"}


def _with_timeout(fn, seconds: float):
    with ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(fn).result(timeout=seconds)


@app.get("/readyz")
def readyz():
    checks = {}
    try:
        checks["graph"] = _with_timeout(lambda: (getattr(store, "ping", lambda: None)(), "ok")[1], 3)
    except FutureTimeout:
        checks["graph"] = "timeout"
    except Exception as e:  # noqa: BLE001 - reported as a check result, not raised
        checks["graph"] = type(e).__name__
    degraded = getattr(store, "degraded", None)
    llm_status = llm.status()
    checks["llm"] = "ok" if llm_status["configured"] and not llm_status["breaker_open"] else \
        ("not_configured" if not llm_status["configured"] else "breaker_open")
    try:
        with connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
        checks["database"] = "ok"
    except Exception as e:  # noqa: BLE001
        checks["database"] = type(e).__name__
    try:
        checks["seed"] = "ok" if _with_timeout(lambda: store.counts()["facts"], 3) > 0 else "empty"
    except Exception as e:  # noqa: BLE001
        checks["seed"] = type(e).__name__
    last = audit.last_result()
    checks["audit"] = ("clean" if last["counts"]["error"] == 0 else f"{last['counts']['error']} error(s)") if last else "not_run"
    ready = checks["graph"] == "ok" and checks["database"] == "ok" and not degraded
    body = {"ready": ready, "degraded": degraded, "checks": checks, "judge_mode": config.JUDGE_MODE,
            "router_frozen": config.ROUTER_FROZEN, "verified_only": health.state()["verified_only"]}
    return JSONResponse(body, status_code=200 if ready else 503)


@app.get("/status")
def status():
    """Cheap state for the UI banner: model availability, degraded graph, verified-only mode."""
    llm_status = {"configured": True, "breaker_open": False} if config.SCRIPTED_DEMO else llm.status()
    return {"llm": llm_status, "scripted": config.SCRIPTED_DEMO, "degraded": getattr(store, "degraded", None),
            "health": health.state(), "judge_mode": config.JUDGE_MODE, "router_frozen": config.ROUTER_FROZEN,
            "backend": store.backend}


# ------------------------------------------------------------------------------------- graph
class Fact(BaseModel):
    subject: str = Field(min_length=1, max_length=300)
    relation: str = Field(min_length=1, max_length=64)
    object: str = Field(min_length=1, max_length=300)
    source: str | None = Field(default=None, max_length=300)  # where the fact came from (document, URL, ...)


class RelationDef(BaseModel):
    name: str
    functional: bool = False
    transitive: bool = False
    acyclic: bool = False


class CacheEntry(BaseModel):
    query: str
    state: dict


class Alias(BaseModel):
    alias: str
    canonical: str


@app.post("/relations", dependencies=[Depends(require_admin)])
def define_relation(r: RelationDef):
    store.define_relation(r.name.lower(), r.functional, r.transitive, r.acyclic)
    ingest_mod.record_manual_rule(r.name.lower(), r.functional, r.transitive)
    return {"ok": True}


@app.post("/facts", dependencies=[Depends(require_admin)])
def add_fact(f: Fact):
    """Through the write gate: accepted, or quarantined with the reason."""
    return graph_guard.add_fact_checked(store, f.subject, f.relation, f.object, f.source, added_by="api")


@app.post("/aliases", dependencies=[Depends(require_admin)])
def add_alias(a: Alias):
    if store.add_alias(a.alias, a.canonical):
        return {"status": "added"}
    with connect() as conn:
        conn.execute("INSERT INTO review_queue(subject, relation, object, source, reason, created_at) VALUES (?,?,?,?,?,?)",
                     (a.alias, "alias_of", a.canonical, "api", "alias already points to another entity", time.time()))
    return {"status": "queued", "reason": "ambiguous alias: it already points to another entity"}


@app.get("/relations")
def relation_stats():
    return store.relation_stats()


@app.get("/connections")
def connections(a: str, b: str):
    return store.connections(a, b)


@app.post("/validate")
def validate(f: Fact):
    return store.validate(f.subject, f.relation, f.object)


@app.get("/audit/summary")
def audit_summary():
    return audit.summary(store)


@app.get("/quarantine")
def quarantine():
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM quarantine ORDER BY id")]


@app.post("/quarantine/{qid}/release", dependencies=[Depends(require_admin)])
def release(qid: int):
    try:
        return graph_guard.release(store, qid)
    except KeyError:
        raise HTTPException(404, "no such quarantined fact")


@app.delete("/quarantine/{qid}", dependencies=[Depends(require_admin)])
def discard(qid: int):
    with connect() as conn:
        if conn.execute("DELETE FROM quarantine WHERE id=?", (qid,)).rowcount == 0:
            raise HTTPException(404, "no such quarantined fact")
    return {"id": qid, "status": "discarded"}


@app.post("/cache", dependencies=[Depends(require_admin)])
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
    text: str = Field(min_length=1, max_length=20000)
    source: str | None = None


class RuleDecision(BaseModel):
    functional: bool | None = None  # override the proposal when approving
    transitive: bool | None = None


@app.post("/ingest", dependencies=[Depends(require_admin)])
def ingest_text(req: IngestRequest):
    try:
        return ingest_mod.ingest(store, req.text, req.source)
    except llm.LLMNotConfigured:
        raise HTTPException(503, pipeline.ERRORS["llm_not_configured"])
    except llm.LLMUnavailable:
        raise HTTPException(503, pipeline.ERRORS["llm_unavailable"])
    except llm.LLMBadOutput:
        raise HTTPException(502, "The model returned output that could not be parsed.")


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


@app.post("/rules/{relation}/approve", dependencies=[Depends(require_admin)])
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


@app.post("/rules/{relation}/reject", dependencies=[Depends(require_admin)])
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


@app.post("/review/{item_id}/accept", dependencies=[Depends(require_admin)])
def accept_item(item_id: int):
    row = _queued(item_id)
    if row["relation"] == "alias_of":
        res = {"status": "accepted" if store.add_alias(row["subject"], row["object"]) else "conflict"}
    else:
        res = graph_guard.add_fact_checked(store, row["subject"], row["relation"], row["object"], row["source"],
                                           added_by="review")
    if res["status"] in ("accepted", "quarantined"):
        with connect() as conn:
            conn.execute("UPDATE review_queue SET status=? WHERE id=?",
                         ("accepted" if res["status"] == "accepted" else "quarantined", item_id))
    return {"id": item_id, **res}


@app.post("/review/{item_id}/reject", dependencies=[Depends(require_admin)])
def reject_item(item_id: int):
    _queued(item_id)
    with connect() as conn:
        conn.execute("UPDATE review_queue SET status='rejected' WHERE id=?", (item_id,))
    return {"id": item_id, "status": "rejected"}


# ------------------------------------------------------------------------------------- chat
class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    mode: Literal["auto", "conservative", "balanced", "aggressive"] = "auto"  # reasoning-tier strictness
    conversation_id: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_.:-]*$")


class Feedback(BaseModel):
    turn_id: int
    helpful: bool


@app.post("/feedback")
def feedback(f: Feedback):
    """A user verdict on an answer. Recorded as that turn's outcome (signal 'user'); when the router is
    frozen the turn is already marked so it never changes routing."""
    with connect() as conn:
        n = conn.execute("UPDATE turns SET feedback=?, outcome=?, signal_quality='user' WHERE id=?",
                         (int(f.helpful), "correct" if f.helpful else "hallucinated", f.turn_id)).rowcount
    if n == 0:
        raise HTTPException(404, "no such turn")
    return {"ok": True, "router_frozen": config.ROUTER_FROZEN}


@app.get("/metrics")
def get_metrics():
    return metrics.summary()


@app.get("/results/latest")
def results_latest():
    data = metrics.latest_results()
    if data is None:
        raise HTTPException(404, "no results yet - run python -m app.bench")
    return data


@app.get("/examples")
def examples():
    """The first-screen example chips, each with whether a recorded run exists for Replay mode."""
    recorded = {p.stem for p in config.REPLAYS_DIR.glob("*.json")} if config.REPLAYS_DIR.exists() else set()
    return [{**e, "replay": e["id"] in recorded} for e in EXAMPLES]


@app.get("/replays")
def replays():
    out = []
    for p in sorted(config.REPLAYS_DIR.glob("*.json")) if config.REPLAYS_DIR.exists() else []:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            out.append({"id": p.stem, "question": d.get("question") or d["turns"][0]["question"],
                        "label": d.get("label", ""), "recorded_at": d.get("recorded_at"), "model": d.get("model")})
        except (OSError, ValueError, KeyError, IndexError):
            continue
    return out


@app.get("/replays/{replay_id}")
def replay(replay_id: str):
    if not replay_id.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(404, "no such replay")
    p = config.REPLAYS_DIR / f"{replay_id}.json"
    if not p.exists():
        raise HTTPException(404, "no such replay")
    return json.loads(p.read_text(encoding="utf-8"))


def _slot() -> bool:
    return _slots.acquire(blocking=False)


@app.post("/chat/stream", dependencies=[Depends(chat_guard)])
def chat_stream(req: ChatRequest):
    """Server-sent events: live thinking steps, then 'Processing complete', then the answer."""
    if not _slot():
        raise HTTPException(503, "The server is busy answering other questions - try again in a moment.")

    def events():
        try:
            for ev in pipeline.run(store, req.question, req.mode, conversation_id=req.conversation_id):
                yield f"data: {json.dumps(ev)}\n\n"
        finally:
            _slots.release()

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


ERROR_STATUS = {"llm_not_configured": 503, "llm_unavailable": 503, "graph_unavailable": 503, "graph_limit": 507,
                "read_only": 403, "internal": 500}


@app.post("/chat", dependencies=[Depends(chat_guard)])
def chat(req: ChatRequest):
    """Same pipeline, collected into one response."""
    if not _slot():
        raise HTTPException(503, "The server is busy answering other questions - try again in a moment.")
    try:
        steps, answer, final = {}, [], None
        for ev in pipeline.run(store, req.question, req.mode, typing_delay=0, conversation_id=req.conversation_id):
            if ev["type"] == "step":
                steps[ev["id"]] = ev
            elif ev["type"] == "answer":
                answer.append(ev["delta"])
            elif ev["type"] == "final":
                final = ev
            elif ev["type"] == "error":
                raise HTTPException(ERROR_STATUS.get(ev["code"], 500), ev["message"])
        return {"answer": "".join(answer), "thinking": list(steps.values()), **(final or {})}
    finally:
        _slots.release()

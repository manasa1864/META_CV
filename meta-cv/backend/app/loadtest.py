"""Load test: N simultaneous conversations through the real pipeline with a scripted model.

    python -m app.loadtest [--conversations 20 --turns 3 --latency 0.3] [--backend sqlite|neo4j]

Each model call sleeps --latency seconds. The state database is a fresh temporary file (WAL on), so
nothing here touches your real data. With --backend neo4j the graph work goes to your Aura instance
through the shared driver, inside LoadEntity/LoadRelation labels that are deleted afterwards.
Passes when there are zero "database is locked" errors and zero Neo4j session errors.
Writes results/loadtest-<timestamp>.json with p50/p95 latency per turn.
"""
import argparse
import json
import os
import tempfile
import threading
import time
import uuid

from . import config, llm


def _fake(latency: float):
    def fake(system, user, temperature=0):
        time.sleep(latency)
        if system.startswith("Profile"):
            return {"risks": [{"label": "factual.entity_error", "weight": 0.9}], "entities": ["jane austen"]}
        if system.startswith("Answer the question as a chain"):
            return {"claims": [{"subject": "jane austen", "relation": "born_in", "object": "steventon"}]}
        if system.startswith("List the atomic"):
            return {"claims": []}
        if system.startswith("The user asks a follow-up"):
            return {"answerable": True, "answer": "Steventon.", "used": [1]}
        return "Jane Austen was born in Steventon."
    return fake


def main(argv=None) -> dict:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--conversations", type=int, default=20)
    ap.add_argument("--turns", type=int, default=3)
    ap.add_argument("--latency", type=float, default=0.3)
    ap.add_argument("--backend", choices=("sqlite", "neo4j"), default="sqlite")
    args = ap.parse_args(argv)

    config.DB_PATH = os.path.join(tempfile.mkdtemp(), "loadtest.db")
    from .db import init_db
    from .engine import pipeline
    from .graph_store import SqliteGraphStore

    init_db()
    store = None
    if args.backend == "neo4j":
        from .neo4j_store import Neo4jGraphStore

        store = Neo4jGraphStore(entity_label="LoadEntity", relation_label="LoadRelation")
    else:
        store = SqliteGraphStore()
    store.define_relation("born_in", functional=True)
    store.add_fact("jane austen", "born_in", "steventon", "loadtest")
    llm.chat_json = llm.chat_text = _fake(args.latency)

    latencies, errors, lock = [], [], threading.Lock()

    def conversation(i: int):
        conv = f"load-{uuid.uuid4().hex[:8]}"
        for k in range(args.turns):
            q = "Where was Jane Austen born?" if k == 0 else "Remind me where she was born?"
            t0 = time.perf_counter()
            try:
                events = list(pipeline.run(store, q, typing_delay=0, conversation_id=conv, source="loadtest"))
                err = [e for e in events if e["type"] == "error"]
                with lock:
                    latencies.append(time.perf_counter() - t0)
                    errors.extend(e.get("code", "error") for e in err)
            except Exception as e:  # noqa: BLE001 - counted, not raised
                with lock:
                    errors.append(f"{type(e).__name__}: {e}")

    t0 = time.perf_counter()
    try:
        threads = [threading.Thread(target=conversation, args=(i,)) for i in range(args.conversations)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        if args.backend == "neo4j":
            store.drop_namespace()
    wall = time.perf_counter() - t0
    lat = sorted(latencies)
    q = lambda p: round(lat[min(len(lat) - 1, int(p * (len(lat) - 1)))] * 1000, 1) if lat else None  # noqa: E731
    locked = sum("locked" in e for e in errors)
    neo = sum(("Session" in e or "ServiceUnavailable" in e or e == "graph_unavailable") for e in errors)
    res = {"backend": args.backend, "conversations": args.conversations, "turns": args.turns, "llm_latency_s": args.latency,
           "answered": len(latencies), "errors": len(errors), "database_locked": locked, "neo4j_session_errors": neo,
           "p50_ms": q(0.5), "p95_ms": q(0.95), "wall_s": round(wall, 2), "pass": locked == 0 and neo == 0 and not errors,
           "created_at": time.time()}
    config.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = config.RESULTS_DIR / f"loadtest-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(res, indent=1), encoding="utf-8")
    print(json.dumps(res, indent=1))
    print(f"wrote {path}")
    return res


if __name__ == "__main__":
    main()

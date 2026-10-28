import json

from fastapi.testclient import TestClient

from app import main
from app.db import connect
from app.engine import mathcheck, pipeline
from app.graph_store import SqliteGraphStore

ROUTER = "Classify a user's question"
DRAFT = "Answer the question as a chain"
JUDGE = "Judge whether ONE claim"
PROBE = "Answer from your own knowledge"
COMPOSE = "Write the answer to the question"
SOLVE = "Solve the problem step by step."
CHECK = "You are checking someone else's solution"


def run(store, q, mode="auto"):
    return list(pipeline.run(store, q, mode, typing_delay=0))


def final(events):
    return next(e for e in events if e["type"] == "final")


def answer_text(events):
    return "".join(e["delta"] for e in events if e["type"] == "answer")


def seeded_store():
    s = SqliteGraphStore()
    s.define_relation("born_in", functional=True)
    s.add_fact("pride and prejudice", "written_by", "jane austen")
    s.add_fact("jane austen", "born_in", "steventon")
    return s


def test_factual_walks_nodes_in_order(fake_llm):
    store = seeded_store()
    fake_llm.on(ROUTER, lambda u, t: {"tier": "factual", "entities": ["Pride and Prejudice"], "why": "book"})
    fake_llm.on(DRAFT, lambda u, t: {"claims": [
        {"subject": "pride and prejudice", "relation": "written_by", "object": "jane austen", "depends_on": []},
        {"subject": "jane austen", "relation": "born_in", "object": "bath", "depends_on": [0]},          # wrong
        {"subject": "elizabeth bennet", "relation": "character_in", "object": "pride and prejudice", "depends_on": [0]},
        {"subject": "mr collins", "relation": "character_in", "object": "emma", "depends_on": []},       # false
        {"subject": "mr collins", "relation": "proposes_to", "object": "elizabeth bennet", "depends_on": [3]},
    ]})
    fake_llm.on(JUDGE, lambda u, t: {"verdict": "false" if "emma" in u else "true", "confidence": 0.9})
    fake_llm.on(PROBE, lambda u, t: {"answers": ["Pride and Prejudice"]})
    fake_llm.on(COMPOSE, lambda u, t: "Jane Austen wrote it.")

    events = run(store, "Who wrote Pride and Prejudice and who is in it?")
    path = final(events)["path"]
    assert [c["status"] for c in path] == ["accepted", "accepted", "accepted", "rejected", "skipped"]
    assert path[0]["source"] == "graph"
    assert path[1]["source"] == "graph-corrected" and path[1]["object"] == "steventon"
    assert path[2]["source"] == "model"
    # the answer comes only after processing is complete
    types = [e["type"] for e in events]
    assert types.index("thinking_done") < types.index("answer") < types.index("final")
    assert all(t == "step" for t in types[:types.index("thinking_done")])
    assert answer_text(events) == "Jane Austen wrote it."
    with connect() as conn:
        queued = conn.execute("SELECT subject, relation, object FROM review_queue").fetchall()
    assert [tuple(r) for r in queued] == [("elizabeth bennet", "character_in", "pride and prejudice")]
    # not every node verified -> path is not cached
    assert final(run(store, "Who wrote Pride and Prejudice and who is in it?"))["from_cache"] is False


def test_verified_factual_path_is_reused_and_revalidated(fake_llm):
    store = seeded_store()
    fake_llm.on(ROUTER, lambda u, t: {"tier": "factual", "entities": ["Jane Austen"]})
    fake_llm.on(DRAFT, lambda u, t: {"claims": [
        {"subject": "jane austen", "relation": "born_in", "object": "steventon"}]})
    fake_llm.on(COMPOSE, lambda u, t: "Steventon.")

    q = "Where was Jane Austen born?"
    assert final(run(store, q))["from_cache"] is False
    n_calls = len(fake_llm.calls)
    second = run(store, q)
    assert final(second)["from_cache"] is True
    assert answer_text(second) == "Steventon."
    assert len(fake_llm.calls) == n_calls  # no LLM work on a cache hit

    # graph changes underneath -> cached path is discarded and recomputed
    with connect() as conn:
        conn.execute("DELETE FROM facts WHERE relation='born_in'")
    store.add_fact("jane austen", "born_in", "bath")
    fake_llm.on(DRAFT, lambda u, t: {"claims": [{"subject": "jane austen", "relation": "born_in", "object": "bath"}]})
    third = run(store, q)
    assert final(third)["from_cache"] is False
    assert any(e["type"] == "step" and e["stage"] == "cache" and e["status"] == "warn" for e in third)


def test_reasoning_backtracks_on_contradiction_and_cross_checks(fake_llm):
    store = SqliteGraphStore()
    fake_llm.on(ROUTER, lambda u, t: {"tier": "reasoning", "entities": []})

    def solve(user, temperature):
        if "VERIFIED steps" in user:  # resumed after the arithmetic error
            return {"steps": [{"text": "Boxes times apples", "expression": "12*7", "result": "84"},
                              {"text": "Minus eaten", "expression": "84-4", "result": "80"}], "final_answer": "80"}
        if temperature == 0:  # first solution has a wrong product
            return {"steps": [{"text": "Boxes times apples", "expression": "12*7", "result": "86"},
                              {"text": "Minus eaten", "expression": "86-4", "result": "82"}], "final_answer": "82"}
        return {"steps": [{"text": "Total", "expression": "12*7-4", "result": "80"}], "final_answer": "80 apples"}

    fake_llm.on(SOLVE, solve)
    fake_llm.on(CHECK, lambda u, t: {"valid": True, "first_bad_step": None})

    events = run(store, "12 boxes of 7 apples, 4 eaten. How many left?", "balanced")
    f = final(events)
    assert f["tier"] == "reasoning" and f["mode"] == "balanced" and f["confidence"] == "high"
    assert "**Answer: 80**" in answer_text(events)
    assert any("Contradiction at step 1" in e["detail"] for e in events if e["type"] == "step")
    # verified in balanced: reusable for balanced/aggressive, not for conservative
    assert final(run(store, "12 boxes of 7 apples, 4 eaten. How many left?", "aggressive"))["from_cache"] is True
    assert final(run(store, "12 boxes of 7 apples, 4 eaten. How many left?", "conservative"))["from_cache"] is False


def test_aggressive_mode_skips_logic_check(fake_llm):
    fake_llm.on(ROUTER, lambda u, t: {"tier": "reasoning", "entities": []})
    fake_llm.on(SOLVE, lambda u, t: {"steps": [{"text": "add", "expression": "2+2", "result": "4"}], "final_answer": "4"})
    f = final(run(SqliteGraphStore(), "2+2?", "aggressive"))
    assert f["confidence"] == "high"
    assert CHECK not in fake_llm.calls and fake_llm.calls.count(SOLVE) == 1


def test_conservative_low_confidence_when_unresolved(fake_llm):
    fake_llm.on(ROUTER, lambda u, t: {"tier": "reasoning", "entities": []})
    fake_llm.on(SOLVE, lambda u, t: {"steps": [{"text": "x", "expression": "3*3", "result": "10"}], "final_answer": "10"})
    events = run(SqliteGraphStore(), "3*3?", "conservative")
    assert final(events)["confidence"] == "low"
    assert "could not verify" in answer_text(events)


def test_mathcheck():
    assert mathcheck.check_step({"expression": "1,200 * 3", "result": "$3,600"}) is None
    assert mathcheck.check_step({"expression": "10/3", "result": "3.33"}) is None
    assert mathcheck.check_step({"expression": "17*23", "result": "381"}) == "17*23 = 391, not 381"
    assert mathcheck.check_step({"expression": "__import__('os')", "result": "1"}) is None
    assert mathcheck.check_step({"expression": "9**9**9", "result": "1"}) is None


def test_stream_endpoint(fake_llm, monkeypatch):
    monkeypatch.setattr(main, "store", seeded_store())
    fake_llm.on(ROUTER, lambda u, t: {"tier": "factual", "entities": ["jane austen"]})
    fake_llm.on(DRAFT, lambda u, t: {"claims": [{"subject": "jane austen", "relation": "born_in", "object": "steventon"}]})
    fake_llm.on(COMPOSE, lambda u, t: "Steventon.")
    with TestClient(main.app) as client:
        r = client.post("/chat/stream", json={"question": "Where was Jane Austen born?"})
        assert r.headers["content-type"].startswith("text/event-stream")
        events = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
        assert events[-1]["type"] == "final"
        assert any(e["type"] == "thinking_done" and e["message"] == "Processing complete" for e in events)
        assert client.post("/chat", json={"question": "Where was Jane Austen born?"}).json()["answer"] == "Steventon."

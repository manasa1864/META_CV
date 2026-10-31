import json

from fastapi.testclient import TestClient

from app import main
from app.db import connect
from app.engine import mathcheck, pipeline
from app.graph_store import SqliteGraphStore

ROUTER = "Profile the hallucination risks"
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
    fake_llm.on(ROUTER, lambda u, t: {"type": "factual", "entities": ["Pride and Prejudice"], "why": "book"})
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
    f = final(events)
    assert f["hallucination_type"] == "factual" and f["tier"] == "factual"
    assert sorted((c["type"], c["action"]) for c in f["caught"]) == [("factual", "corrected"), ("factual", "dropped")]
    assert f["tokens"]["calls"] == len(fake_llm.calls) and f["tokens"]["total"] > 0
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
    fake_llm.on(ROUTER, lambda u, t: {"type": "factual", "entities": ["Jane Austen"]})
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
    fake_llm.on(ROUTER, lambda u, t: {"type": "reasoning", "entities": []})

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
    fake_llm.on(ROUTER, lambda u, t: {"type": "reasoning", "entities": []})
    fake_llm.on(SOLVE, lambda u, t: {"steps": [{"text": "add", "expression": "2+2", "result": "4"}], "final_answer": "4"})
    f = final(run(SqliteGraphStore(), "2+2?", "aggressive"))
    assert f["confidence"] == "high"
    assert CHECK not in fake_llm.calls and fake_llm.calls.count(SOLVE) == 1


def test_conservative_low_confidence_when_unresolved(fake_llm):
    fake_llm.on(ROUTER, lambda u, t: {"type": "reasoning", "entities": []})
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
    fake_llm.on(ROUTER, lambda u, t: {"type": "factual", "entities": ["jane austen"]})
    fake_llm.on(DRAFT, lambda u, t: {"claims": [{"subject": "jane austen", "relation": "born_in", "object": "steventon"}]})
    fake_llm.on(COMPOSE, lambda u, t: "Steventon.")
    with TestClient(main.app) as client:
        r = client.post("/chat/stream", json={"question": "Where was Jane Austen born?"})
        assert r.headers["content-type"].startswith("text/event-stream")
        events = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
        assert events[-1]["type"] == "final"
        assert events[-1]["tokens"]["calls"] == 4  # router, draft, compose, guard - metered across stream threads
        assert any(e["type"] == "thinking_done" and e["message"] == "Processing complete" for e in events)
        assert client.post("/chat", json={"question": "Where was Jane Austen born?"}).json()["answer"] == "Steventon."


CONSISTENCY = "The user asks a follow-up"
BASELINE = "Answer the user's question"


def test_learned_router_prefers_the_tier_that_works(fake_llm):
    from app.engine import router

    assert router.choose("factual", False, router.stats())[0] == "factual"  # prior: the specialist
    with connect() as conn:
        for tier, ok in [("factual", 0)] * 10 + [("reasoning", 1)] * 10:
            conn.execute("INSERT INTO turns(question, hallucination_type, tier, confidence, caught, answer, "
                         "commitments, correct, created_at) VALUES ('q','factual',?,'low','[]','a','[]',?,0)", (tier, ok))
    tier, scores = router.choose("factual", False, router.stats())
    assert tier == "reasoning" and scores["reasoning"] > scores["factual"]


def test_feedback_counts_as_the_outcome(fake_llm, monkeypatch):
    monkeypatch.setattr(main, "store", seeded_store())
    fake_llm.on(ROUTER, lambda u, t: {"type": "factual", "entities": ["jane austen"]})
    fake_llm.on(DRAFT, lambda u, t: {"claims": [{"subject": "jane austen", "relation": "born_in", "object": "steventon"}]})
    fake_llm.on(COMPOSE, lambda u, t: "Steventon.")
    with TestClient(main.app) as client:
        turn_id = client.post("/chat", json={"question": "Where was Jane Austen born?"}).json()["turn_id"]
        assert client.post("/feedback", json={"turn_id": turn_id, "helpful": False}).status_code == 200
        m = client.get("/metrics").json()
    assert m["totals"]["queries"] == 1
    row = next(r for r in m["router"] if r["type"] == "factual" and r["tier"] == "factual")
    assert row["n"] == 1 and row["successes"] == 0  # high confidence, but the user said it was wrong


def test_consistency_follow_up_answered_from_verified_memory(fake_llm):
    store = seeded_store()
    fake_llm.on(ROUTER, lambda u, t: {"type": "consistency" if "Conversation so far" in u else "factual",
                                      "entities": ["jane austen"]})
    fake_llm.on(DRAFT, lambda u, t: {"claims": [{"subject": "jane austen", "relation": "born_in", "object": "steventon"}]})
    fake_llm.on(COMPOSE, lambda u, t: "Steventon.")
    fake_llm.on(CONSISTENCY, lambda u, t: {"answerable": True, "answer": "As I said, Steventon.", "used": [1]})

    def run_conv(q):
        return list(pipeline.run(store, q, typing_delay=0, conversation_id="c1"))

    run_conv("Where was Jane Austen born?")
    drafts = fake_llm.calls.count(DRAFT)
    events = run_conv("Remind me, where did you say she was born?")
    f = final(events)
    assert f["hallucination_type"] == "consistency" and f["tier"] == "consistency" and f["confidence"] == "high"
    assert answer_text(events) == "As I said, Steventon."
    assert fake_llm.calls.count(DRAFT) == drafts  # nothing regenerated
    assert "Q1: Where was Jane Austen born?" in fake_llm.prompts[CONSISTENCY][0]


def _solve_17x23_then_plus_9(user, temperature):
    follow_up = "391" in user
    return {"steps": [{"text": "x", "expression": "391+9" if follow_up else "17*23",
                       "result": "400" if follow_up else "391"}],
            "final_answer": "400" if follow_up else "391"}


def test_consistency_follow_up_delegates_with_a_standalone_question(fake_llm):
    fake_llm.on(ROUTER, lambda u, t: {"type": "consistency" if "Conversation so far" in u else "reasoning"})
    fake_llm.on(SOLVE, _solve_17x23_then_plus_9)
    fake_llm.on(CONSISTENCY, lambda u, t: {"answerable": False, "standalone": "What is 391 + 9?",
                                           "standalone_type": "reasoning"})
    store = SqliteGraphStore()
    list(pipeline.run(store, "What is 17*23?", "aggressive", typing_delay=0, conversation_id="c2"))
    f = final(list(pipeline.run(store, "Now add 9 to that.", "aggressive", typing_delay=0, conversation_id="c2")))
    assert f["tier"] == "reasoning" and f["standalone"] == "What is 391 + 9?" and f["confidence"] == "high"


def test_claim_contradicting_an_earlier_turn_is_dropped(fake_llm):
    store = SqliteGraphStore()
    store.define_relation("born_in", functional=True)
    city = {"obj": "london"}
    fake_llm.on(ROUTER, lambda u, t: {"type": "factual", "entities": ["ada lovelace"]})
    fake_llm.on(DRAFT, lambda u, t: {"claims": [{"subject": "ada lovelace", "relation": "born_in", "object": city["obj"]}]})
    fake_llm.on(JUDGE, lambda u, t: {"verdict": "true", "confidence": 0.9})
    fake_llm.on(PROBE, lambda u, t: {"answers": ["London", "Paris"]})
    fake_llm.on(COMPOSE, lambda u, t: "ok")

    list(pipeline.run(store, "Where was Ada Lovelace born?", typing_delay=0, conversation_id="c3"))
    city["obj"] = "paris"
    f = final(list(pipeline.run(store, "Tell me Ada Lovelace's birthplace.", typing_delay=0, conversation_id="c3")))
    assert f["path"][0]["status"] == "rejected" and f["path"][0]["source"] == "consistency"
    assert [(c["type"], c["action"]) for c in f["caught"]] == [("consistency", "dropped")]


def test_graph_incompatible_objects_are_not_both_accepted(fake_llm):
    store = SqliteGraphStore()
    store.add_fact("1984", "genre", "dystopian fiction")
    store.add_fact("dystopian fiction", "incompatible_with", "utopian fiction")
    fake_llm.on(ROUTER, lambda u, t: {"type": "factual", "entities": ["1984"]})
    fake_llm.on(DRAFT, lambda u, t: {"claims": [
        {"subject": "1984", "relation": "genre", "object": "dystopian fiction"},
        {"subject": "1984", "relation": "genre", "object": "utopian fiction"}]})
    fake_llm.on(JUDGE, lambda u, t: {"verdict": "true", "confidence": 0.9})
    fake_llm.on(PROBE, lambda u, t: {"answers": ["dystopian fiction", "utopian fiction"]})
    fake_llm.on(COMPOSE, lambda u, t: "Dystopian.")
    f = final(run(store, "What genre is 1984?"))
    assert [c["status"] for c in f["path"]] == ["accepted", "rejected"]
    assert ("factual.relation_error", "factual", "factual", "dropped") in [
        (c["risk"], c["type"], c["origin_tier"], c["action"]) for c in f["caught"]]


def test_similar_question_reuses_verified_reasoning_state(fake_llm):
    def solve(user, temperature):
        twelve = "12 boxes" in user.split("A similar problem")[0]
        return {"steps": [{"text": "total", "expression": "12*7-4" if twelve else "15*6-9",
                           "result": "80" if twelve else "81"}], "final_answer": "80" if twelve else "81"}

    fake_llm.on(ROUTER, lambda u, t: {"type": "reasoning"})
    fake_llm.on(SOLVE, solve)
    store = SqliteGraphStore()
    first = final(run(store, "A shop has 12 boxes with 7 apples each. 4 apples are eaten. How many apples are left?",
                      "aggressive"))
    assert first["reused_state"] is None
    second = final(run(store, "A shop has 15 boxes with 6 apples each. 9 apples are eaten. How many apples are left?",
                       "aggressive"))
    assert second["reused_state"] and second["reused_state"]["similarity"] == 1.0
    assert "A similar problem was solved and verified before" in fake_llm.prompts[SOLVE][-1]
    assert second["path"][0]["result"] == "81"


def test_bench_runs_end_to_end(fake_llm, monkeypatch, tmp_path):
    from app import bench, config

    data = tmp_path / "bench_data"
    data.mkdir()
    (data / "familiar.json").write_text(json.dumps({"items": [
        {"id": "f1", "kind": "familiar", "graph_contains_answer": True, "turns": [
            {"question": "Where was Jane Austen born?", "type": "factual", "risks": [], "expected": ["steventon"],
             "forbidden": ["bath"]}]},
        {"id": "f2", "kind": "familiar", "turns": [
            {"question": "What is 17*23?", "type": "reasoning", "risks": [], "expected": 391},
            {"question": "Now add 9 to that result.", "type": "consistency", "risks": [], "expected": 400}]},
        {"id": "f3", "kind": "false_premise", "turns": [
            {"question": "When did Jane Austen win the Nobel Prize?", "type": "factual", "risks": [], "expect_abstain": True}]},
    ]}))
    monkeypatch.setattr(config, "BENCH_DATA_DIR", data)
    monkeypatch.setattr(bench, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(bench, "get_store", seeded_store)
    fake_llm.on(BASELINE, lambda u, t: "Bath." if "born" in u else "Answer: 401" if "Now add" in u
                else "In 1817." if "Nobel" in u else "Answer: 391")
    fake_llm.on(ROUTER, lambda u, t: {"type": "consistency" if "Conversation so far" in u else
                                      "reasoning" if "17" in u else "factual", "entities": ["jane austen"]})
    fake_llm.on(DRAFT, lambda u, t: {"claims": [{"subject": "jane austen", "relation": "won", "object": "nobel prize"}]}
                if "Nobel" in u else {"claims": [{"subject": "jane austen", "relation": "born_in", "object": "steventon"}]})
    fake_llm.on(COMPOSE, lambda u, t: "Steventon.")
    fake_llm.on(SOLVE, _solve_17x23_then_plus_9)
    fake_llm.on(CHECK, lambda u, t: {"valid": True})
    fake_llm.on(CONSISTENCY, lambda u, t: {"answerable": False, "standalone": "What is 391 + 9?",
                                           "standalone_type": "reasoning"})
    fake_llm.on(JUDGE, lambda u, t: {"verdict": "false", "confidence": 0.9})
    fake_llm.on(PROBE, lambda u, t: {"answers": []})

    s = bench.main(["--sets", "familiar", "--train"])
    base, full = s["configs"]["baseline"]["overall"], s["configs"]["full"]["overall"]
    assert base["n"] == 4 and base["correct"] == 1 and base["hallucinated"] == 3
    assert full["n"] == 4 and full["correct"] == 4 and full["hallucinated"] == 0
    assert full["hallucination_ci"][0] == 0.0 and full["accuracy_ci"][1] == 1.0
    assert s["configs"]["full"]["by_kind"]["false_premise"]["correct"] == 1   # abstaining is the right answer
    assert s["routing"]["in_sample_curve"] and "test" in s["routing"]
    assert (tmp_path / "results" / "latest.json").exists()
    assert "CAVEATS" in bench.report_text(s)
    with TestClient(main.app) as client:
        assert client.get("/metrics").json()["bench"]["meta"]["items"] == 3
        assert client.get("/results/latest").json()["configs"]["full"]["overall"]["n"] == 4


def test_grading():
    from app.bench import grade, wilson

    assert grade("**Answer: 80 apples**\n\nHow I got there: 12*7=84", 80) == "correct"
    assert grade("It is 84, then 80", 80) == "correct"
    assert grade("Answer: 82", 80) == "hallucinated"
    assert grade("I couldn't verify any facts to answer this.", ["austen"]) == "abstained"
    assert grade("Jane Austen wrote it in 1813.", ["austen", "1813"]) == "correct"
    # claim-level: the right keyword next to a forbidden claim is still a hallucination
    assert grade("Jane Austen, born in Bath, wrote it.", ["austen"], ["bath"]) == "hallucinated"
    assert grade("Castrell is a different town.", ["castrell"], ["castrel"]) == "correct"   # word boundary
    assert grade("There is no record of that prize.", expect_abstain=True) == "correct"
    assert grade("She won it in 1817.", expect_abstain=True) == "hallucinated"
    assert grade("anything", ["x"], stopped=True) == "abstained"
    assert wilson(0, 10)[0] == 0.0 and wilson(10, 10)[1] == 1.0 and wilson(0, 0) is None

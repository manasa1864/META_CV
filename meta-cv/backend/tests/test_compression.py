"""Token compression: each kind is tested for the model calls it saves, for falling back to the full procedure
when it cannot be trusted, and for switching off cleanly (the no_compression ablation)."""
from app.engine import pipeline
from app.graph_store import SqliteGraphStore

ROUTER = "Profile the hallucination risks"
DRAFT = "Answer the question as a chain"
COMPOSE = "Write the answer to the question"
SOLVE = "Solve the problem step by step."
CHECK = "You are checking someone else's solution"
STATE_ANSWER = "Answer the question using ONLY the numbered verified facts"

SHIRTS = "A shop sells 3 shirts at $24.99 each with 8% sales tax. What is the total?"
HATS = "A shop sells 5 hats at $12.50 each with 8% sales tax. What is the total?"


def run(store, q, mode="auto", **kw):
    return list(pipeline.run(store, q, mode, typing_delay=0, **kw))


def final(events):
    return next(e for e in events if e["type"] == "final")


def _solve(user, temperature):
    problem = user.split("A similar problem")[0]
    if "shirts" in problem:
        steps = [{"text": "price", "expression": "3*24.99", "result": "74.97"},
                 {"text": "with tax", "expression": "74.97*1.08", "result": "80.9676"}]
        return {"steps": steps, "final_answer": "80.9676"}
    steps = [{"text": "price", "expression": "5*12.50", "result": "62.5"},
             {"text": "with tax", "expression": "62.5*1.08", "result": "67.5"}]
    return {"steps": steps, "final_answer": "67.5"}


def _calc_llm(fake_llm, check=lambda u, t: {"valid": True}):
    fake_llm.on(ROUTER, lambda u, t: {"type": "reasoning"})
    fake_llm.on(SOLVE, _solve)
    fake_llm.on(CHECK, check)


def _calls_for(fake_llm, start):
    return fake_llm.calls[start:]


def test_template_reuse_skips_the_cross_check(fake_llm):
    _calc_llm(fake_llm)
    store = SqliteGraphStore()
    first = final(run(store, SHIRTS, "balanced"))
    n = len(fake_llm.calls)
    second = final(run(store, HATS, "balanced"))
    full = fake_llm.calls[:n]
    reuse = fake_llm.calls[n:]
    assert first["compression"]["mode"] is None and full.count(SOLVE) == 2 and full.count(CHECK) == 2
    assert second["compression"]["mode"] == "template" and second["confidence"] == "high"
    assert reuse.count(SOLVE) == 1 and reuse.count(CHECK) == 1          # 2 model calls instead of 4
    assert second["tokens"]["calls"] < first["tokens"]["calls"]


def test_template_reuse_falls_back_when_the_adaptation_fails(fake_llm):
    verdicts = iter([{"valid": True}, {"valid": True},                  # first question, two solutions
                     {"valid": False, "first_bad_step": 2, "reason": "wrong tax"},   # adapted solution
                     {"valid": False, "first_bad_step": 2, "reason": "wrong tax"},   # its backtrack
                     {"valid": True}, {"valid": True}, {"valid": True}])
    _calc_llm(fake_llm, check=lambda u, t: next(verdicts))
    store = SqliteGraphStore()
    run(store, SHIRTS, "balanced")
    events = run(store, HATS, "balanced")
    f = final(events)
    assert f["compression"]["mode"] != "template" and f["confidence"] == "high"
    assert any(c["detail"].startswith("adapted solution rejected") for c in f["caught"])
    assert any(e.get("title") == "The adapted solution did not pass its checks" for e in events if e["type"] == "step")


def _fact_store():
    s = SqliteGraphStore()
    s.define_relation("born_in", functional=True)
    s.define_relation("published_in", functional=True)
    s.add_fact("pride and prejudice", "written_by", "jane austen", "seed")
    s.add_fact("pride and prejudice", "published_in", "1813", "seed")
    s.add_fact("jane austen", "born_in", "steventon", "seed")
    return s


def _fact_llm(fake_llm, state_answer):
    fake_llm.on(ROUTER, lambda u, t: {"type": "factual", "entities": ["Pride and Prejudice"]})
    fake_llm.on(DRAFT, lambda u, t: {"claims": [
        {"subject": "pride and prejudice", "relation": "written_by", "object": "jane austen"},
        {"subject": "pride and prejudice", "relation": "published_in", "object": "1813"},
        {"subject": "jane austen", "relation": "born_in", "object": "steventon"}]})
    fake_llm.on(COMPOSE, lambda u, t: "Jane Austen wrote Pride and Prejudice, published in 1813; she was born in Steventon.")
    fake_llm.on(STATE_ANSWER, state_answer)


def test_similar_factual_question_is_answered_from_verified_facts(fake_llm):
    _fact_llm(fake_llm, lambda u, t: {"answerable": True, "answer": "It was published in 1813 and written by Jane Austen.",
                                      "used": [1, 2]})
    store = _fact_store()
    first = final(run(store, "Who wrote Pride and Prejudice, when was it published, and where was the author born?"))
    n = len(fake_llm.calls)
    events = run(store, "When was Pride and Prejudice published, and who wrote it?")
    second = final(events)
    assert first["compression"]["mode"] is None
    assert second["compression"]["mode"] == "state_answer" and second["confidence"] == "high"
    assert DRAFT not in fake_llm.calls[n:] and COMPOSE not in fake_llm.calls[n:]
    assert second["tokens"]["calls"] < first["tokens"]["calls"]
    assert "1813" in "".join(e["delta"] for e in events if e["type"] == "answer")


def test_state_answer_is_not_trusted_when_it_adds_unverified_detail(fake_llm):
    _fact_llm(fake_llm, lambda u, t: {"answerable": True, "answer": "Published in 1813 in London by Jane Austen.",
                                      "used": [1, 2]})
    store = _fact_store()
    run(store, "Who wrote Pride and Prejudice, when was it published, and where was the author born?")
    second = final(run(store, "When was Pride and Prejudice published, and who wrote it?"))
    assert second["compression"]["mode"] != "state_answer"   # 'London' is unverified: the full check ran instead


def test_state_answer_is_not_used_when_the_graph_changed(fake_llm):
    from app.db import connect

    _fact_llm(fake_llm, lambda u, t: {"answerable": True, "answer": "1813.", "used": [2]})
    fake_llm.on("Judge whether ONE claim", lambda u, t: {"verdict": "unsure", "confidence": 0.3})
    fake_llm.on("Answer from your own knowledge", lambda u, t: {"answers": []})
    store = _fact_store()
    run(store, "Who wrote Pride and Prejudice, when was it published, and where was the author born?")
    with connect() as conn:
        conn.execute("DELETE FROM facts WHERE relation='published_in'")
    n = len(fake_llm.calls)
    second = final(run(store, "When was Pride and Prejudice published, and who wrote it?"))
    assert second["compression"]["mode"] != "state_answer" and STATE_ANSWER not in fake_llm.calls[n:]


def test_prompts_are_trimmed_to_the_relevant_facts(fake_llm):
    store = _fact_store()
    for i in range(120):
        store.add_fact("jane austen", f"unrelated_relation_{i}", f"thing {i}", "seed")
    fake_llm.on(ROUTER, lambda u, t: {"type": "factual", "entities": ["Pride and Prejudice", "Jane Austen"]})
    fake_llm.on(DRAFT, lambda u, t: {"claims": [{"subject": "pride and prejudice", "relation": "published_in", "object": "1813"}]})
    fake_llm.on(COMPOSE, lambda u, t: "1813.")
    run(store, "When was Pride and Prejudice published?")
    trimmed = fake_llm.prompts[DRAFT][-1]
    run(store, "When was Pride and Prejudice published?", flags={"no_compression": True}, reuse=False)
    untrimmed = fake_llm.prompts[DRAFT][-1]
    assert "pride and prejudice | published_in | 1813" in trimmed
    assert trimmed.count("\n- ") <= 30 < untrimmed.count("\n- ")
    assert len(trimmed) < len(untrimmed) / 2


def test_no_compression_flag_turns_every_kind_off(fake_llm):
    _calc_llm(fake_llm)
    store = SqliteGraphStore()
    run(store, SHIRTS, "balanced")
    second = final(run(store, HATS, "balanced", flags={"no_compression": True}))
    assert second["compression"] == {"mode": "similar_state", "prompt_trim": False}

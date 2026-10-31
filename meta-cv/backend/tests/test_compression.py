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
    assert second["compression"]["mode"] == "similar_state" and second["compression"]["prompt_trim"] is False
    assert second["compression"]["context"] == {}


# ------------------------------------------------------------------ the lost-context check for trimmed prompts
def test_keep_rules_never_cut_conversation_facts_question_words_or_entities():
    from app.engine import compression

    entity = [{"subject": "jane austen", "relation": f"hobby_{i}", "object": f"thing {i}"} for i in range(60)]
    asked = {"subject": "jane austen", "relation": "born_in", "object": "steventon"}
    earlier = {"subject": "elizabeth bennet", "relation": "marries", "object": "fitzwilliam darcy"}
    other = {"subject": "warsaw", "relation": "located_in", "object": "poland"}
    shown, cut = compression.split_facts(entity + [asked, earlier, other], "Where was Jane Austen born?",
                                         ["Jane Austen"], must_keep=[earlier])
    assert asked in shown and earlier in shown                 # shares 'born' / established in this conversation
    assert len(shown) <= compression.FACT_CAP + 2 and len(shown) + len(cut) == 63
    assert other in cut and all(f in shown for f in entity[:compression.PER_ENTITY_MIN])


def _crowded_store():
    s = SqliteGraphStore()
    for i in range(60):
        s.add_fact("jane austen", f"hobby_{i}", f"thing {i}", "seed")
    s.add_fact("jane austen", "raised_in", "steventon", "seed")   # needed, but shares no word with the question
    return s


def test_context_check_redrafts_untrimmed_when_trimming_hid_a_needed_fact(fake_llm):
    fake_llm.on(ROUTER, lambda u, t: {"type": "factual", "entities": ["Jane Austen"]})
    fake_llm.on(DRAFT, lambda u, t: {"claims": [{"subject": "jane austen", "relation": "raised_in", "object": "steventon"}]}
                if "raised_in" in u else {"claims": [{"subject": "jane austen", "relation": "grew_up_in", "object": "bath"}]})
    fake_llm.on("Judge whether ONE claim", lambda u, t: {"verdict": "unsure", "confidence": 0.3})
    fake_llm.on("Answer from your own knowledge", lambda u, t: {"answers": []})
    fake_llm.on(COMPOSE, lambda u, t: "Jane Austen was raised in Steventon.")
    events = run(_crowded_store(), "Where did Jane Austen grow up?")
    f = final(events)
    assert "raised_in" not in fake_llm.prompts[DRAFT][0] and "raised_in" in fake_llm.prompts[DRAFT][1]
    assert f["path"][0]["object"] == "steventon" and f["path"][0]["status"] == "accepted"
    ctx = f["compression"]["context"]
    assert ctx["redrafted"] is True and ctx["facts_cut"] > 0 and "jane austen" in ctx["redraft_reason"]
    assert any(e.get("title") == "Context check: trimming may have hidden something" for e in events if e["type"] == "step")


def test_context_check_passes_without_a_redraft_when_every_claim_verified(fake_llm):
    store = _crowded_store()
    store.add_fact("jane austen", "born_in", "steventon", "seed")
    fake_llm.on(ROUTER, lambda u, t: {"type": "factual", "entities": ["Jane Austen"]})
    fake_llm.on(DRAFT, lambda u, t: {"claims": [{"subject": "jane austen", "relation": "born_in", "object": "steventon"}]})
    fake_llm.on(COMPOSE, lambda u, t: "Jane Austen was born in Steventon.")
    events = run(store, "Where was Jane Austen born?")
    f = final(events)
    assert fake_llm.calls.count(DRAFT) == 1 and not f["compression"]["context"].get("redrafted")
    assert f["compression"]["context"]["facts_cut"] > 0
    assert any(e.get("title") == "Context check passed" for e in events if e["type"] == "step")


def test_history_is_restored_when_the_question_refers_to_a_cut_turn():
    from app import meter
    from app.engine import compression

    people = ["austen", "orwell", "curie", "einstein", "newton", "darwin"]
    hist = [{"question": f"Tell me about {p}", "answer": f"Some facts about {p}."} for p in people]
    m = meter.new_meter({})
    with meter.metering(m):
        shown, _, expanded = compression.compact_history(hist, "Going back to Austen, what else?")
        assert expanded and len(shown) == 6 and m["compression"]["history_expanded"] is True
        shown, size, expanded = compression.compact_history(hist, "And what about Darwin?")
        assert not expanded and len(shown) == compression.HISTORY_TURNS and size == compression.HISTORY_ANSWER_CHARS


def test_metrics_report_how_often_trimming_had_to_be_undone(fake_llm):
    from fastapi.testclient import TestClient

    from app import main

    test_context_check_redrafts_untrimmed_when_trimming_hid_a_needed_fact(fake_llm)
    with TestClient(main.app) as client:
        check = client.get("/metrics").json()["context_check"]
    assert check["trimmed_answers"] == 1 and check["redrafted"] == 1 and check["facts_cut"] > 0

"""Workstreams A, B and E: taxonomy and routing, audit, write gate, faithfulness guard, stop conditions,
snapshot fallback, judge mode, and concurrency."""
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app import audit, config, graph_guard, llm, main, recheck, state_cache
from app.db import connect
from app.engine import factual, health as health_mod, pipeline, router
from app.engine.entities import alias_candidates, normalize
from app.graph_common import GraphUnavailable, ReadOnlyGraph
from app.graph_store import ResilientStore, SqliteGraphStore

ROUTER = "Profile the hallucination risks"
DRAFT = "Answer the question as a chain"
JUDGE = "Judge whether ONE claim"
PROBE = "Answer from your own knowledge"
COMPOSE = "Write the answer to the question"
SOLVE = "Solve the problem step by step."
CHECK = "You are checking someone else's solution"
CONSISTENCY = "The user asks a follow-up"
EXTRACT = "List the atomic factual claims"


def run(store, q, mode="auto", **kw):
    return list(pipeline.run(store, q, mode, typing_delay=0, **kw))


def final(events):
    return next(e for e in events if e["type"] == "final")


def answer_text(events):
    return "".join(e["delta"] for e in events if e["type"] == "answer")


def einstein_store():
    s = SqliteGraphStore()
    s.define_relation("nobel_prize_year", functional=True)
    s.define_relation("born_in", functional=True)
    s.add_fact("albert einstein", "birth_year", "1879", "seed")
    s.add_fact("albert einstein", "nobel_prize_year", "1921", "seed")
    s.add_fact("jane austen", "born_in", "steventon", "seed")
    return s


# ---------------------------------------------------------------------------------- A: taxonomy and routing
def test_classifier_coerces_entities_and_reads_the_profile(fake_llm):
    fake_llm.on(ROUTER, lambda u, t: {"risks": [{"label": "factual.entity_error", "weight": 0.9},
                                                {"label": "bogus.code", "weight": 1}],
                                      "entities": "Jane Austen", "false_premise_suspected": True})
    p = router.classify("Who was Jane Austen?")
    assert p["entities"] == ["Jane Austen"] and p["type"] == "factual" and p["source"] == "llm"
    assert [r["label"] for r in p["risks"]] == ["factual.entity_error"] and p["false_premise_suspected"]
    assert router.coerce_entities([["a", None], {"name": "b"}, " ", 3]) == ["a", "b", "3"]


def test_classifier_retries_then_falls_back(fake_llm):
    def bad(u, t):
        raise llm.LLMBadOutput("not json")

    fake_llm.on(ROUTER, bad)
    p = router.classify("What is 17 * 23?")
    assert p["source"] == "fallback" and p["type"] == "reasoning"
    assert fake_llm.calls.count(ROUTER) == 2


def test_consistency_without_history_is_not_accepted(fake_llm):
    fake_llm.on(ROUTER, lambda u, t: {"risks": [{"label": "faithful.context", "weight": 1}]})
    assert router.classify("Where was he born?")["source"] == "fallback"


@pytest.mark.parametrize("q,computational", [
    ("How many books did Jane Austen write?", False), ("How many moons does Mars have?", False),
    ("What is the average lifespan of a tortoise?", False), ("What percent of Earth is water?", False),
    ("Who ruled France from 1643-1715?", False),
    ("A train leaves at noon at 80 km/h, when does it arrive 200 km away?", True),
    ("Prove that the square root of 2 is irrational", True), ("What is 391 - 9?", True),
    ("How old was Einstein when he won the Nobel Prize?", True),
])
def test_keyword_fallback_probes(q, computational):
    assert router.looks_computational(q) is computational


def test_mixed_question_verifies_premises_then_calculates(fake_llm):
    store = einstein_store()
    fake_llm.on(ROUTER, lambda u, t: {"risks": [{"label": "faithful.premise", "weight": 0.9}, {"label": "factual.entity_error", "weight": 0.7}],
                                      "needs": {"facts": True, "calculation": True}, "entities": ["Albert Einstein"]})
    fake_llm.on(DRAFT, lambda u, t: {"claims": [
        {"subject": "albert einstein", "relation": "birth_year", "object": "1879"},
        {"subject": "albert einstein", "relation": "nobel_prize_year", "object": "1921"}]})

    def solve(u, t):
        assert "VERIFIED PREMISES" in u and "1879" in u and "1921" in u
        return {"steps": [{"text": "age", "expression": "1921-1879", "result": "42"}], "final_answer": "42"}

    fake_llm.on(SOLVE, solve)
    fake_llm.on(CHECK, lambda u, t: {"valid": True})
    f = final(run(store, "How old was Albert Einstein when he won the Nobel Prize?"))
    assert f["plan"] == ["factual", "reasoning"] and f["tier"] == "reasoning" and f["confidence"] == "high"
    assert len(f["premises"]) == 2 and fake_llm.calls.index(DRAFT) < fake_llm.calls.index(SOLVE)


def test_mixed_question_abstains_without_verified_premises(fake_llm):
    fake_llm.on(ROUTER, lambda u, t: {"risks": [{"label": "faithful.premise", "weight": 0.9}],
                                      "needs": {"facts": True, "calculation": True}, "entities": ["Zorb"]})
    fake_llm.on(DRAFT, lambda u, t: {"claims": [{"subject": "zorb", "relation": "birth_year", "object": "1800"}]})
    fake_llm.on(JUDGE, lambda u, t: {"verdict": "unsure", "confidence": 0.3})
    fake_llm.on(PROBE, lambda u, t: {"answers": []})
    events = run(SqliteGraphStore(), "How old was Zorb when he died?")
    f = final(events)
    assert f["stopped"]["reason"] == "premise_unverified" and SOLVE not in fake_llm.calls
    assert ("faithful.premise", "dropped") in [(c["risk"], c["action"]) for c in f["caught"]]


def test_consistency_tier_machine_checks_derived_numbers(fake_llm):
    store = einstein_store()
    fake_llm.on(ROUTER, lambda u, t: {"type": "consistency" if "Conversation so far" in u else "factual",
                                      "entities": ["albert einstein"]})
    fake_llm.on(DRAFT, lambda u, t: {"claims": [{"subject": "albert einstein", "relation": "nobel_prize_year", "object": "1921"}]})
    fake_llm.on(COMPOSE, lambda u, t: "In 1921.")
    fake_llm.on(CONSISTENCY, lambda u, t: {"answerable": True, "answer": "That is 30 years.", "used": [1],
                                           "calculations": [{"expression": "1950-1921", "result": "30"}],
                                           "standalone": "How many years after 1921 is 1950?", "standalone_type": "reasoning"})
    fake_llm.on(SOLVE, lambda u, t: {"steps": [{"text": "d", "expression": "1950-1921", "result": "29"}], "final_answer": "29"})
    fake_llm.on(CHECK, lambda u, t: {"valid": True})
    run(store, "When did Einstein win the Nobel Prize?", conversation_id="k1")
    f = final(run(store, "So how many years after his Nobel Prize was 1950?", conversation_id="k1"))
    assert f["tier"] == "reasoning" and "29" in f["path"][0]["result"]   # the wrong 30 never reached the answer


def _insert_turn(tier, outcome, quality, kind="factual", scope=None):
    with connect() as conn:
        conn.execute("INSERT INTO turns(question, hallucination_type, tier, confidence, caught, answer, commitments, "
                     "outcome, signal_quality, scope, created_at) VALUES ('q',?,?,'low','[]','a','[]',?,?,?,0)",
                     (kind, tier, outcome, quality, scope))


def test_router_prefers_abstaining_over_hallucinating_and_ignores_self_signals():
    for _ in range(8):
        _insert_turn("factual", "hallucinated", "ground_truth")
        _insert_turn("reasoning", "abstained", "ground_truth")
    assert router.choose("factual", False, router.stats())[0] == "reasoning"
    with connect() as conn:
        conn.execute("DELETE FROM turns")
    for _ in range(30):
        _insert_turn("reasoning", "correct", "self")   # circular confidence: excluded by default
    assert router.choose("factual", False, router.stats())[0] == "factual"


def test_frozen_router_reads_but_never_learns(fake_llm, monkeypatch):
    monkeypatch.setattr(config, "ROUTER_FROZEN", True)
    fake_llm.on(ROUTER, lambda u, t: {"type": "reasoning"})
    fake_llm.on(SOLVE, lambda u, t: {"steps": [{"text": "x", "expression": "2+2", "result": "4"}], "final_answer": "4"})
    f = final(run(SqliteGraphStore(), "2+2?", "aggressive"))
    with TestClient(main.app) as client:
        assert client.post("/feedback", json={"turn_id": f["turn_id"], "helpful": False}).json()["router_frozen"]
    assert router.stats() == {}


# ---------------------------------------------------------------------------------- B1: audit (both stores)
def test_audit_finds_every_crafted_problem(store):
    store.define_relation("born_in", functional=True)
    store.define_relation("part_of", transitive=True, acyclic=True)
    store.add_fact("jane austen", "born_in", "steventon", "seed")
    store.add_fact("jane austen", "born_in", "bath", "seed")                       # functional violation
    store.add_fact("dystopian fiction", "incompatible_with", "utopian fiction", "seed")
    store.add_fact("1984", "genre", "dystopian fiction", "seed")
    store.add_fact("1984", "genre", "utopian fiction", "seed")                     # incompatible pair
    store.add_fact("a", "part_of", "b", "seed")
    store.add_fact("b", "part_of", "c", "seed")
    store.add_fact("c", "part_of", "a", "seed")                                    # cycle
    store.add_fact("d", "part_of", "d", "seed")                                    # self-loop
    store.add_fact("marie curie", "won", "nobel prize", "seed")
    store.add_fact("marie curei", "discovered", "radium", None)                    # near-duplicate, no provenance
    store.add_fact("x", "writen_by", "y", "seed")
    store.add_fact("x2", "written_by", "y2", "seed")                               # relation drift
    state_cache.put("k", {"tier": "factual", "path": [{"subject": "ghost", "relation": "likes", "object": "z",
                                                        "status": "accepted", "source": "graph"}]})
    res = audit.run_audit(store, stale_days=-1)
    checks = {f["check"] for f in res["findings"]}
    assert {"functional_violation", "incompatible_pair", "cycle", "self_loop", "near_duplicate", "no_provenance",
            "stale", "undefined_relation", "relation_drift", "dangling_cache"} <= checks
    assert res["counts"]["error"] >= 4
    pairs = [f["entities"] for f in res["findings"] if f["check"] == "near_duplicate"]
    assert ["marie curei", "marie curie"] in pairs


def test_clean_graph_has_no_errors(store):
    store.define_relation("born_in", functional=True)
    store.add_fact("jane austen", "born_in", "steventon", "seed")
    assert audit.run_audit(store)["counts"]["error"] == 0


def test_audit_cli_exit_code(monkeypatch):
    s = SqliteGraphStore()
    s.define_relation("born_in", functional=True)
    s.add_fact("x", "born_in", "a", "seed")
    s.add_fact("x", "born_in", "b", "seed")
    monkeypatch.setattr("app.graph_store.get_store", lambda: s)
    assert audit.main(["--fail-on", "error"]) == 1


def test_poisoned_graph_refuses_instead_of_answering(fake_llm):
    store = einstein_store()
    store.add_fact("jane austen", "born_in", "bath", "poison")   # a second value for a single-valued relation
    audit.run_audit(store)
    fake_llm.on(ROUTER, lambda u, t: {"type": "factual", "entities": ["Jane Austen"]})
    events = run(store, "Where was Jane Austen born?")
    f = final(events)
    assert f["stopped"]["reason"] == "graph_conflict" and DRAFT not in fake_llm.calls
    assert "conflicting" in answer_text(events)


# ---------------------------------------------------------------------------------- B2: write gate
def test_write_gate_quarantines_and_accepts():
    s = einstein_store()
    s.define_relation("part_of", transitive=True, acyclic=True)
    s.add_fact("a", "part_of", "b")
    s.add_fact("dystopian fiction", "incompatible_with", "utopian fiction")
    s.add_fact("1984", "genre", "dystopian fiction")
    assert graph_guard.add_fact_checked(s, "albert einstein", "nobel_prize_year", "1905")["status"] == "quarantined"
    assert graph_guard.add_fact_checked(s, "b", "part_of", "a")["status"] == "quarantined"            # cycle
    assert graph_guard.add_fact_checked(s, "1984", "genre", "utopian fiction")["status"] == "quarantined"
    assert graph_guard.add_fact_checked(s, "albert einstien", "won", "nobel prize")["status"] == "quarantined"
    state_cache.put(factual.claim_key({"subject": "marie curie", "relation": "born_in", "object": "paris"}),
                    {"status": "accepted"}, triples=["marie curie|born_in|paris"])
    res = graph_guard.add_fact_checked(s, "marie curie", "born_in", "warsaw", "seed")
    assert res["status"] == "accepted"
    assert state_cache.get(factual.claim_key({"subject": "marie curie", "relation": "born_in", "object": "paris"})) is None
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0] == 4
    assert s.validate("albert einstein", "nobel_prize_year", "1905")["verdict"] == "contradicted"   # never written


def test_ingest_queues_unverified_facts_from_untrusted_sources(fake_llm, monkeypatch):
    from app import ingest

    s = einstein_store()
    monkeypatch.setattr(llm, "extract_triples", lambda text, rels: [
        {"subject": "ada lovelace", "relation": "born_in", "object": "london", "confidence": 0.9}])
    monkeypatch.setattr(llm, "propose_relation_rules", lambda r: {"functional": False, "transitive": False,
                                                                   "confidence": 0.9, "rationale": ""})
    res = ingest.ingest(s, "Ada Lovelace was born in London.", "some blog")
    assert res["stored"] == [] and res["queued"][0]["reason"] == "unverified_source"
    res = ingest.ingest(s, "Ada Lovelace was born in London.", "seed")   # trusted
    assert res["stored"] and s.validate("ada lovelace", "born_in", "london")["verdict"] == "supported"


# ---------------------------------------------------------------------------------- B5: faithfulness guard
def test_guard_regenerates_an_answer_that_adds_unverified_detail(fake_llm):
    store = einstein_store()
    replies = iter(["Jane Austen was born in Steventon in 1775.", "Jane Austen was born in Steventon."])
    fake_llm.on(ROUTER, lambda u, t: {"type": "factual", "entities": ["jane austen"]})
    fake_llm.on(DRAFT, lambda u, t: {"claims": [{"subject": "jane austen", "relation": "born_in", "object": "steventon"}]})
    fake_llm.on(COMPOSE, lambda u, t: next(replies))
    events = run(store, "Where was Jane Austen born?")
    assert answer_text(events) == "Jane Austen was born in Steventon."
    assert [(c["risk"], c["action"]) for c in final(events)["caught"]] == [("factual.fabrication", "corrected")]


def test_guard_falls_back_to_the_verified_facts_verbatim(fake_llm):
    store = einstein_store()
    fake_llm.on(ROUTER, lambda u, t: {"type": "factual", "entities": ["jane austen"]})
    fake_llm.on(DRAFT, lambda u, t: {"claims": [{"subject": "jane austen", "relation": "born_in", "object": "steventon"}]})
    fake_llm.on(COMPOSE, lambda u, t: "Steventon, where she lived with Cassandra.")
    events = run(store, "Where was Jane Austen born?")
    assert answer_text(events).startswith("Verified: jane austen born in steventon")
    assert ("factual.fabrication", "dropped") in [(c["risk"], c["action"]) for c in final(events)["caught"]]


def test_guard_catches_an_extracted_claim_that_matches_no_verified_fact(fake_llm):
    store = einstein_store()
    fake_llm.on(ROUTER, lambda u, t: {"type": "factual", "entities": ["jane austen"]})
    fake_llm.on(DRAFT, lambda u, t: {"claims": [{"subject": "jane austen", "relation": "born_in", "object": "steventon"}]})
    fake_llm.on(COMPOSE, lambda u, t: "She was born in steventon and wrote emma.")
    fake_llm.on(EXTRACT, lambda u, t: {"claims": [{"subject": "jane austen", "relation": "born_in", "object": "steventon"},
                                                  {"subject": "jane austen", "relation": "wrote", "object": "emma"}]})
    events = run(store, "Where was Jane Austen born?")
    assert answer_text(events).startswith("Verified:")


def test_reasoning_guard_flags_a_final_answer_that_contradicts_the_steps(fake_llm):
    fake_llm.on(ROUTER, lambda u, t: {"type": "reasoning"})
    fake_llm.on(SOLVE, lambda u, t: {"steps": [{"text": "a", "expression": "12*7", "result": "84"},
                                               {"text": "b", "expression": "84-4", "result": "80"}], "final_answer": "82"})
    f = final(run(SqliteGraphStore(), "12 boxes of 7, 4 eaten?", "aggressive"))
    assert f["confidence"] == "low" and ("faithful.logic", "flagged") in [(c["risk"], c["action"]) for c in f["caught"]]


# ---------------------------------------------------------------------------------- B6: stop conditions
def test_false_premise_is_named_instead_of_answered(fake_llm):
    store = einstein_store()
    fake_llm.on(ROUTER, lambda u, t: {"type": "factual", "entities": ["albert einstein"], "false_premise_suspected": True})
    fake_llm.on(DRAFT, lambda u, t: {"claims": [{"subject": "albert einstein", "relation": "nobel_prize_year", "object": "1905"}]})
    fake_llm.on(COMPOSE, lambda u, t: "Albert Einstein won it in 1921.")
    events = run(store, "Why did Einstein win the Nobel Prize in 1905?")
    f = final(events)
    assert f["stopped"]["reason"] == "false_premise" and "1921, not 1905" in answer_text(events)


def test_breaker_switches_to_verified_only_mode(fake_llm, monkeypatch):
    for _ in range(config.BREAKER_MIN_TURNS):
        health_mod.health.record(rejected=True)
    fake_llm.on(ROUTER, lambda u, t: {"type": "factual", "entities": ["ada lovelace"]})
    fake_llm.on(DRAFT, lambda u, t: {"claims": [{"subject": "ada lovelace", "relation": "born_in", "object": "london"}]})
    f = final(run(einstein_store(), "Where was Ada Lovelace born?"))
    assert f["verified_only"] and JUDGE not in fake_llm.calls and f["stopped"]["reason"] == "nothing_verified"


def test_budget_stops_cleanly(fake_llm, monkeypatch):
    monkeypatch.setattr(config, "MAX_LLM_CALLS", 2)
    fake_llm.on(ROUTER, lambda u, t: {"type": "factual", "entities": ["ada lovelace"]})
    fake_llm.on(DRAFT, lambda u, t: {"claims": [{"subject": "ada lovelace", "relation": "born_in", "object": "london"}]})
    fake_llm.on(JUDGE, lambda u, t: {"verdict": "true", "confidence": 0.9})
    fake_llm.on(PROBE, lambda u, t: {"answers": ["London"]})
    events = run(einstein_store(), "Where was Ada Lovelace born?")
    f = final(events)
    assert f["stopped"]["reason"] == "budget" and "budget" in answer_text(events)


def test_errors_reaching_the_browser_are_sanitised(fake_llm):
    class Leaky(SqliteGraphStore):
        def facts_about(self, *a, **k):
            raise RuntimeError("bolt+s://secret-instance.databases.neo4j.io refused")

    fake_llm.on(ROUTER, lambda u, t: {"type": "factual", "entities": ["x"]})
    err = next(e for e in run(Leaky(), "Who is x?") if e["type"] == "error")
    assert err["code"] == "internal" and "neo4j.io" not in err["message"]


# ---------------------------------------------------------------------------------- B4: recheck and TTL
def test_model_verified_claims_expire_and_recheck_prunes(fake_llm, monkeypatch):
    c = {"subject": "ada lovelace", "relation": "born_in", "object": "london"}
    state_cache.put(factual.claim_key(c), {"status": "accepted"}, ttl=-1)
    assert state_cache.get(factual.claim_key(c)) is None
    s = einstein_store()
    state_cache.put("p", {"tier": "factual", "path": [{"subject": "albert einstein", "relation": "nobel_prize_year",
                                                        "object": "1921", "status": "accepted", "source": "graph"}]})
    state_cache.put("q", {"tier": "factual", "path": [{"subject": "ghost", "relation": "born_in", "object": "x",
                                                        "status": "accepted", "source": "graph"}]})
    report = recheck.run(s)
    assert report["paths_checked"] == 2 and report["paths_removed"] == 1
    assert state_cache.get("p") is not None and state_cache.get("q") is None


# ---------------------------------------------------------------------------------- A9: entities
def test_entity_normalisation_and_alias_candidates():
    assert normalize("  J.K.  Rowling ") == "j k rowling" and normalize("3.14 apples") == "3.14 apples"
    assert normalize("“Pride”, and Prejudice") == "pride and prejudice"
    assert alias_candidates("J. Austen", ["jane austen", "jane bennet"]) == ["jane austen"]
    assert alias_candidates("Mr Darcy", ["fitzwilliam darcy"]) == ["fitzwilliam darcy"]
    assert alias_candidates("J. Smith", ["jane smith", "john smith"]) == ["jane smith", "john smith"]  # ambiguous


def test_ambiguous_alias_goes_to_review():
    with TestClient(main.app) as client:
        assert client.post("/aliases", json={"alias": "J. Austen", "canonical": "Jane Austen"}).json()["status"] == "added"
        r = client.post("/aliases", json={"alias": "J. Austen", "canonical": "Joan Austen"}).json()
        assert r["status"] == "queued" and client.get("/review").json()[0]["relation"] == "alias_of"


# ---------------------------------------------------------------------------------- E: deployment safety
def test_graph_unreachable_falls_back_to_the_snapshot(fake_llm, tmp_path):
    snap = tmp_path / "snap.db"
    seed = SqliteGraphStore(str(snap))
    seed.define_relation("born_in", functional=True)
    seed.add_fact("jane austen", "born_in", "steventon", "seed")

    def down():
        raise GraphUnavailable("paused")

    store = ResilientStore(down, str(snap))
    fake_llm.on(ROUTER, lambda u, t: {"type": "factual", "entities": ["jane austen"]})
    fake_llm.on(DRAFT, lambda u, t: {"claims": [{"subject": "jane austen", "relation": "born_in", "object": "steventon"}]})
    fake_llm.on(COMPOSE, lambda u, t: "Steventon.")
    f = final(run(store, "Where was Jane Austen born?"))
    assert f["degraded"] == "graph_snapshot" and f["path"][0]["source"] == "graph"
    with pytest.raises(ReadOnlyGraph):
        store.add_fact("a", "likes", "b")


def test_graph_failing_mid_run_switches_to_the_snapshot(tmp_path):
    snap = tmp_path / "snap.db"
    SqliteGraphStore(str(snap)).add_fact("a", "likes", "b", "seed")

    class Flaky(SqliteGraphStore):
        def validate(self, *a, **k):
            raise GraphUnavailable("connection reset")

    store = ResilientStore(Flaky, str(snap))
    assert store.degraded is None
    assert store.validate("a", "likes", "b")["verdict"] == "supported" and store.degraded == "graph_snapshot"


def test_judge_mode_blocks_writes_and_rate_limits(fake_llm, monkeypatch):
    monkeypatch.setattr(config, "JUDGE_MODE", True)
    monkeypatch.setattr(config, "ADMIN_TOKEN", "")
    monkeypatch.setattr(config, "RATE_LIMIT_PER_MIN", 1)
    monkeypatch.setattr(main, "store", einstein_store())
    main._hits.clear()
    fake_llm.on(ROUTER, lambda u, t: {"type": "reasoning"})
    fake_llm.on(SOLVE, lambda u, t: {"steps": [{"text": "x", "expression": "2+2", "result": "4"}], "final_answer": "4"})
    fake_llm.on(CHECK, lambda u, t: {"valid": True})
    with TestClient(main.app) as client:
        fact = {"subject": "a", "relation": "likes", "object": "b"}
        assert client.post("/facts", json=fact).status_code == 403
        assert client.post("/ingest", json={"text": "x"}).status_code == 403
        assert client.post("/chat", json={"question": "2+2?"}).status_code == 200
        assert client.post("/chat", json={"question": "2+2?"}).status_code == 429
        monkeypatch.setattr(config, "ADMIN_TOKEN", "s3cret")
        assert client.post("/facts", json=fact, headers={"X-Admin-Token": "s3cret"}).json()["status"] == "accepted"


def test_daily_token_budget(monkeypatch):
    with connect() as conn:
        conn.execute("INSERT INTO usage_daily(day, tokens) VALUES (?, ?)", (time.strftime("%Y-%m-%d"), 10**9))
    with TestClient(main.app) as client:
        r = client.post("/chat", json={"question": "hi"})
    assert r.status_code == 429 and "budget" in r.json()["detail"]


def test_readyz_and_status(monkeypatch):
    monkeypatch.setattr(main, "store", einstein_store())
    with TestClient(main.app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        r = client.get("/readyz")
        assert r.status_code == 200 and r.json()["checks"]["seed"] == "ok" and r.json()["checks"]["llm"] == "not_configured"
        assert client.get("/status").json()["llm"]["configured"] is False


def test_concurrent_writes_do_not_lock_the_database():
    errors = []

    def work(i):
        try:
            for k in range(25):
                state_cache.put(f"key-{i}-{k}", {"v": k}, ttl=60, triples=[f"s{i}|r|o{k}"])
                state_cache.get(f"key-{i}-{k}")
                _insert_turn("factual", "correct", "self")
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    threads = [threading.Thread(target=work, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0] == 500


def test_production_server_serves_the_app_and_the_api(tmp_path, monkeypatch):
    from app import server

    (tmp_path / "index.html").write_text("<html>app</html>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "x.js").write_text("js")
    monkeypatch.setattr(config, "FRONTEND_DIST", tmp_path)
    with TestClient(server.app) as client:
        assert client.get("/api/healthz").json() == {"status": "ok"}
        assert client.get("/assets/x.js").text == "js"
        assert client.get("/results").text == "<html>app</html>"          # SPA route
        assert client.get("/../../etc/passwd").text == "<html>app</html>"  # no traversal


def test_results_and_replays_endpoints(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REPLAYS_DIR", tmp_path)
    (tmp_path / "false-premise.json").write_text(json.dumps({"question": "q", "label": "Recorded run", "events": []}))
    with TestClient(main.app) as client:
        assert client.get("/results/latest").status_code == 404
        assert client.get("/replays").json()[0]["id"] == "false-premise"
        assert client.get("/replays/false-premise").json()["question"] == "q"
        assert client.get("/replays/..%2Fsecret").status_code == 404

"""Type checks: a claim whose subject or object is the wrong kind of thing for its relation is rejected
without a model call, and kept out of the graph at the write gate. Types come from the graph, not the model."""
from app import graph_guard
from app.db import connect
from app.engine import entity_types as T
from app.engine import pipeline
from app.graph_store import SqliteGraphStore

ROUTER = "Profile the hallucination risks"
DRAFT = "Answer the question as a chain"
JUDGE = "Judge whether ONE claim"
PROBE = "Answer from your own knowledge"
COMPOSE = "Write the answer to the question"


def run(store, q, mode="auto", **kw):
    return list(pipeline.run(store, q, mode, typing_delay=0, **kw))


def final(events):
    return next(e for e in events if e["type"] == "final")


def graph():
    s = SqliteGraphStore()
    s.define_relation("born_in", functional=True)
    s.add_fact("jane austen", "born_in", "steventon", "seed")
    s.add_fact("pride and prejudice", "written_by", "jane austen", "seed")
    s.add_fact("warsaw", "located_in", "poland", "seed")
    s.add_fact("1984", "written_by", "george orwell", "seed")
    return s


def test_types_are_inferred_from_the_graph_and_explicit_types_win():
    s = graph()
    assert T.known_type(s, "Jane Austen") == "person"          # subject of born_in, object of written_by
    assert T.known_type(s, "warsaw") == "place" and T.known_type(s, "poland") == "place"
    assert T.known_type(s, "1984") == "work" and T.known_type(s, "nobody") is None
    s.add_fact("georgia", "located_in", "united states", "seed")
    s.add_fact("ray", "born_in", "georgia", "seed")
    s.add_fact("georgia", "born_in", "atlanta", "seed")              # a person named Georgia too: mixed evidence
    assert T.known_type(s, "georgia") is None
    s.add_fact("georgia", "entity_type", "place", "seed")
    assert T.known_type(s, "georgia") == "place"


def test_wrong_kinds_of_things_are_rejected_on_clear_evidence_only():
    s = graph()
    assert "'1775' is a year" in T.check(s, "jane austen", "born_in", "1775")
    assert "'warsaw' is a place" in T.check(s, "pride and prejudice", "written_by", "warsaw")
    assert "'steventon' is a place" in T.check(s, "jane austen", "birth_year", "steventon")
    assert "has no year in it" in T.check(s, "jane austen", "birth_year", "the regency era")
    assert T.check(s, "jane austen", "born_in", "bath") is None               # unknown place: the graph decides later
    assert T.check(s, "1984", "written_by", "george orwell") is None          # a title that looks like a year
    assert T.check(s, "brave new world", "published_in", "1932") is None
    assert T.check(s, "albert einstein", "birth_year", "March 14, 1879") is None
    assert T.check(s, "jane austen", "won", "1813") is None                   # no signature: no opinion
    assert "not one of" in T.check(s, "warsaw", "entity_type", "city")


def test_a_wrong_type_claim_is_dropped_without_a_model_call(fake_llm):
    # (A stored birthplace would correct the claim first - the graph outranks the type check. Orwell's is not stored.)
    fake_llm.on(ROUTER, lambda u, t: {"type": "factual", "entities": ["1984"]})
    fake_llm.on(DRAFT, lambda u, t: {"claims": [
        {"subject": "1984", "relation": "written_by", "object": "george orwell"},
        {"subject": "george orwell", "relation": "born_in", "object": "1903"}]})
    fake_llm.on(COMPOSE, lambda u, t: "George Orwell wrote it.")
    events = run(graph(), "Who wrote 1984, and where was the author born?")
    f = final(events)
    wrong = f["path"][1]
    assert wrong["status"] == "rejected" and wrong["source"] == "types" and "born_in needs a place" in wrong["reason"]
    assert JUDGE not in fake_llm.calls and PROBE not in fake_llm.calls
    assert ("factual.relation_error", "dropped") in [(c["risk"], c["action"]) for c in f["caught"]]
    assert any(e["type"] == "step" and e["detail"].startswith("Type check:") for e in events)


def test_the_write_gate_quarantines_wrong_type_facts():
    s = graph()
    res = graph_guard.add_fact_checked(s, "pride and prejudice", "written_by", "warsaw", "doc")
    assert res["status"] == "quarantined" and "'warsaw' is a place" in res["reason"]
    assert s.validate("pride and prejudice", "written_by", "warsaw")["verdict"] != "supported"
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM quarantine WHERE object='warsaw'").fetchone()[0] == 1
    assert graph_guard.add_fact_checked(s, "emma", "written_by", "jane austen", "doc")["status"] == "accepted"

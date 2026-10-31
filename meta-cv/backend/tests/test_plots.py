"""Plots as ordered events: stored through the write gate, answered graph-first in story order with who takes
part, and a claim that reverses the story's order is contradicted by the graph."""
from fastapi.testclient import TestClient

from app import config, main
from app.engine import entity_types, pipeline, plots
from app.graph_store import SqliteGraphStore

ROUTER = "Profile the hallucination risks"
PLAN = "Split the question into the facts it asks for"
DRAFT = "Answer the question as a chain"
JUDGE = "Judge whether ONE claim"
PROBE = "Answer from your own knowledge"
COMPOSE = "Write the answer to the question"

STORY = [
    {"event": "the miller finds a lantern", "involves": ["tomas quill"]},
    {"event": "the lantern shows the river's past", "involves": ["tomas quill", "ines marrow"]},
    {"event": "the flood is foretold", "involves": ["ines marrow"]},
    {"event": "the village is saved", "involves": ["tomas quill", "ines marrow"]},
]


def run(store, q, mode="auto", **kw):
    return list(pipeline.run(store, q, mode, typing_delay=0, **kw))


def final(events):
    return next(e for e in events if e["type"] == "final")


def story_store():
    s = SqliteGraphStore()
    s.add_fact("the copper lamp", "written_by", "tomas quill", "seed")
    res = plots.add_plot(s, "the copper lamp", STORY, "seed")
    assert res["quarantined"] == [] and res["events"] == 4
    return s


def test_a_plot_is_stored_in_order_through_the_write_gate():
    s = story_store()
    nodes = [{"subject": e["event"], "relation": "event_in", "object": "the copper lamp"} for e in reversed(STORY)]
    ordered = plots.in_story_order(s, nodes)
    assert [n["subject"] for n in ordered] == [e["event"] for e in STORY]
    assert ordered[1]["detail"] == "event 2 of 4; involves ines marrow, tomas quill"
    assert s.validate("the village is saved", "follows", "the miller finds a lantern")["verdict"] == "supported"
    assert entity_types.known_type(s, "the flood is foretold") == "event"
    assert entity_types.known_type(s, "ines marrow") == "person"
    # the write gate still applies: an event cannot involve a place
    s.add_fact("millbrook", "located_in", "the north", "seed")
    bad = plots.add_plot(s, "the copper lamp", [{"event": "a fair is held", "involves": ["millbrook"]}], "doc")
    assert len(bad["quarantined"]) == 1 and "involves needs a person" in bad["quarantined"][0]["reason"]


def test_a_plot_question_is_answered_from_the_graph_in_story_order(fake_llm):
    fake_llm.on(ROUTER, lambda u, t: {"type": "factual", "entities": ["the copper lamp"]})
    fake_llm.on(PLAN, lambda u, t: {"parts": [{"subject": "?", "relation": "event_in", "object": "the copper lamp"}]})
    prompts = []
    fake_llm.on(COMPOSE, lambda u, t: prompts.append(u) or "Tomas Quill finds a lantern, and in the end the village is saved.")
    f = final(run(story_store(), "What happens in The Copper Lamp?"))
    assert [c["subject"] for c in f["path"]] == [e["event"] for e in STORY]           # every event, in order
    assert all(c["source"] == "graph" for c in f["path"]) and f["confidence"] == "high"
    assert DRAFT not in fake_llm.calls                                                # nothing left to the model
    facts = prompts[0].split("Verified facts:")[1]
    assert facts.index("the miller finds a lantern") < facts.index("the village is saved")
    assert "(event 1 of 4; involves tomas quill)" in facts


def test_a_claim_that_reverses_the_story_order_is_contradicted(fake_llm):
    fake_llm.on(ROUTER, lambda u, t: {"type": "factual", "entities": ["the copper lamp"]})
    fake_llm.on(DRAFT, lambda u, t: {"claims": [
        {"subject": "the miller finds a lantern", "relation": "follows", "object": "the flood is foretold"},   # backwards
        {"subject": "the village is saved", "relation": "follows", "object": "the flood is foretold"}]})       # right
    fake_llm.on(COMPOSE, lambda u, t: "The village is saved after the flood is foretold.")
    f = final(run(story_store(), "Does the miller find the lantern after the flood is foretold?"))
    backwards, right = f["path"]
    assert backwards["status"] == "rejected" and "other way round" in backwards["reason"]
    assert right["status"] == "accepted" and right["source"] == "graph"              # via the follows chain
    assert JUDGE not in fake_llm.calls and PROBE not in fake_llm.calls


def test_plots_endpoint_writes_a_plot(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TOKEN", "")
    monkeypatch.setattr(config, "JUDGE_MODE", False)
    s = SqliteGraphStore()
    monkeypatch.setattr(main, "store", s)
    with TestClient(main.app) as c:
        r = c.post("/plots", json={"work": "the copper lamp", "events": STORY, "source": "test"}).json()
        assert r["events"] == 4 and r["quarantined"] == []
        assert c.post("/plots", json={"work": "x", "events": []}).status_code == 422
    assert s.validate("the flood is foretold", "event_order", "3")["verdict"] == "supported"

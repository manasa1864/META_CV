"""Graph-first answering: the question is split into parts, each part the graph can answer is answered from
the graph alone (every stored value), the model drafts only what is left, and a part with no verified
answer is reported instead of silently missing."""
from app.engine import decompose, pipeline
from app.graph_store import SqliteGraphStore

ROUTER = "Profile the hallucination risks"
PLAN = "Split the question into the facts it asks for"
DRAFT = "Answer the question as a chain"
JUDGE = "Judge whether ONE claim"
PROBE = "Answer from your own knowledge"
COMPOSE = "Write the answer to the question"

CHARACTERS = ["elizabeth bennet", "fitzwilliam darcy", "jane bennet", "charles bingley"]


def run(store, q, mode="auto", **kw):
    return list(pipeline.run(store, q, mode, typing_delay=0, **kw))


def final(events):
    return next(e for e in events if e["type"] == "final")


def steps(events):
    return [e for e in events if e["type"] == "step"]


def austen_store():
    s = SqliteGraphStore()
    s.define_relation("born_in", functional=True)
    s.define_relation("published_in", functional=True)
    s.add_fact("pride and prejudice", "written_by", "jane austen", "seed")
    s.add_fact("pride and prejudice", "published_in", "1813", "seed")
    s.add_fact("jane austen", "born_in", "steventon", "seed")
    for c in CHARACTERS:
        s.add_fact(c, "character_in", "pride and prejudice", "seed")
    return s


def part(s, r, o):
    return {"subject": s, "relation": r, "object": o}


def factual_route(fake_llm, entities=("pride and prejudice",)):
    fake_llm.on(ROUTER, lambda u, t: {"type": "factual", "entities": list(entities)})


def test_every_part_answered_from_the_graph_needs_no_draft(fake_llm):
    factual_route(fake_llm)
    fake_llm.on(PLAN, lambda u, t: {"parts": [part("pride and prejudice", "written_by", "?"),
                                              part("pride and prejudice", "published_in", "?"),
                                              part("#1", "born_in", "?")]})
    fake_llm.on(COMPOSE, lambda u, t: "Jane Austen wrote it; it was published in 1813; she was born in Steventon.")
    events = run(austen_store(), "Who wrote Pride and Prejudice, when was it published, and where was the author born?")
    f = final(events)
    assert DRAFT not in fake_llm.calls and JUDGE not in fake_llm.calls     # nothing left to the model
    assert [(c["relation"], c["object"], c["source"]) for c in f["path"]] == [
        ("written_by", "jane austen", "graph"), ("published_in", "1813", "graph"), ("born_in", "steventon", "graph")]
    assert f["confidence"] == "high"
    assert any(s["title"] == "Every part answered from the graph" for s in steps(events))


def test_a_lookup_returns_every_stored_value_not_just_what_a_draft_would_name(fake_llm):
    factual_route(fake_llm)
    fake_llm.on(PLAN, lambda u, t: {"parts": [part("?", "character_in", "pride and prejudice")]})
    fake_llm.on(COMPOSE, lambda u, t: "Elizabeth Bennet, Fitzwilliam Darcy, Jane Bennet and Charles Bingley.")
    f = final(run(austen_store(), "Who are the characters in Pride and Prejudice?"))
    assert sorted(c["subject"] for c in f["path"]) == sorted(CHARACTERS)
    assert f["confidence"] == "high" and DRAFT not in fake_llm.calls


def test_only_the_parts_the_graph_lacks_are_drafted_and_verified(fake_llm):
    factual_route(fake_llm)
    fake_llm.on(PLAN, lambda u, t: {"parts": [part("pride and prejudice", "written_by", "?"),
                                              part("pride and prejudice", "genre", "?")]})

    def draft(u, t):
        assert "Still needed" in u and "pride and prejudice | genre | ?" in u
        assert "pride and prejudice | written_by | jane austen" in u.split("Already answered")[1]
        return {"claims": [part("pride and prejudice", "written_by", "jane austen"),     # repeats a graph answer
                           part("pride and prejudice", "genre", "romance novel")]}

    fake_llm.on(DRAFT, draft)
    fake_llm.on(JUDGE, lambda u, t: {"verdict": "true", "confidence": 0.9})
    fake_llm.on(PROBE, lambda u, t: {"answers": ["romance novel"]})
    fake_llm.on(COMPOSE, lambda u, t: "Jane Austen wrote it; it is a romance novel.")
    events = run(austen_store(), "Who wrote Pride and Prejudice and what genre is it?")
    f = final(events)
    assert [(c["relation"], c["source"]) for c in f["path"]] == [("written_by", "graph"), ("genre", "model")]
    assert fake_llm.calls.count(JUDGE) == 1                                   # the repeated graph answer is not re-checked
    assert any(s["title"] == "Coverage check passed" for s in steps(events))


def test_a_part_with_no_verified_answer_is_reported_not_dropped(fake_llm):
    factual_route(fake_llm)
    fake_llm.on(PLAN, lambda u, t: {"parts": [part("pride and prejudice", "written_by", "?"),
                                              part("pride and prejudice", "page_count", "?")]})
    fake_llm.on(DRAFT, lambda u, t: {"claims": [part("pride and prejudice", "page_count", "432")]})
    fake_llm.on(JUDGE, lambda u, t: {"verdict": "unsure", "confidence": 0.4})
    fake_llm.on(PROBE, lambda u, t: {"answers": []})
    composed = []
    fake_llm.on(COMPOSE, lambda u, t: composed.append(u) or "Jane Austen wrote it; the page count could not be verified.")
    events = run(austen_store(), "Who wrote Pride and Prejudice and how many pages does it have?")
    f = final(events)
    gap = [c for c in f["path"] if c.get("source") == "coverage"]
    assert len(gap) == 1 and gap[0]["status"] == "unverified" and gap[0]["relation"] == "page_count"
    assert f["confidence"] == "medium" and f["withheld"] >= 1
    assert "pride and prejudice | page_count | ?" in composed[0].split("Could not verify")[1]
    assert any(s["title"].startswith("Coverage check: some parts") for s in steps(events))


def test_a_yes_no_part_the_graph_contradicts_comes_back_corrected(fake_llm):
    factual_route(fake_llm, ["jane austen"])
    fake_llm.on(PLAN, lambda u, t: {"parts": [part("jane austen", "born_in", "bath")]})
    fake_llm.on(COMPOSE, lambda u, t: "No - Jane Austen was born in Steventon, not Bath.")
    f = final(run(austen_store(), "Was Jane Austen born in Bath?"))
    (node,) = f["path"]
    assert node["source"] == "graph-corrected" and node["object"] == "steventon" and node["original_object"] == "bath"
    assert ("factual.entity_error", "corrected") in [(c["risk"], c["action"]) for c in f["caught"]]
    assert DRAFT not in fake_llm.calls


def test_the_ablation_flag_turns_graph_first_off(fake_llm):
    factual_route(fake_llm, ["jane austen"])
    fake_llm.on(DRAFT, lambda u, t: {"claims": [part("jane austen", "born_in", "steventon")]})
    fake_llm.on(COMPOSE, lambda u, t: "Steventon.")
    f = final(run(austen_store(), "Where was Jane Austen born?", flags={"no_graph_first": True}))
    assert PLAN not in fake_llm.calls and f["path"][0]["object"] == "steventon"


def test_plan_parsing_is_defensive():
    parts = decompose.parse({"parts": [
        part("?", "written_by", "?"),                       # two unknowns: dropped
        part("Pride and Prejudice", "Written By", "?"),     # normalised
        part("#1", "born_in", "?"),                         # refers to the dropped part: dropped
        part("#2", "born_in", "?"),                         # refers to the kept part (now part 1)
        "not a part",
    ]})
    assert parts == [part("pride and prejudice", "written_by", "?"), part("#1", "born_in", "?")]
    assert decompose.parse("no json") == [] and decompose.parse({"parts": [part("a", "r", "?")] * 9}).__len__() == 6

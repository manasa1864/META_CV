"""Time-sensitive facts: validity periods travel with their facts, and a question about the present never
gets an ended fact presented as current, nor an old or model-only fact at full confidence."""
import time
from datetime import date

from app.db import connect
from app.engine import pipeline, temporal
from app.graph_store import SqliteGraphStore

ROUTER = "Profile the hallucination risks"
PLAN = "Split the question into the facts it asks for"
DRAFT = "Answer the question as a chain"
JUDGE = "Judge whether ONE claim"
PROBE = "Answer from your own knowledge"
COMPOSE = "Write the answer to the question"


def run(store, q, mode="auto", **kw):
    return list(pipeline.run(store, q, mode, typing_delay=0, **kw))


def final(events):
    return next(e for e in events if e["type"] == "final")


def answer_text(events):
    return "".join(e["delta"] for e in events if e["type"] == "answer")


def acme_store():
    s = SqliteGraphStore()
    s.add_fact("alice", "ceo_of", "acme corp", "filing", valid_from="2015", valid_to="2021-06")
    s.add_fact("bob", "ceo_of", "acme corp", "filing", valid_from="2021-06")
    return s


def route(fake_llm, current: bool):
    fake_llm.on(ROUTER, lambda u, t: {"type": "factual", "entities": ["acme corp"], "time_sensitive": current})
    fake_llm.on(PLAN, lambda u, t: {"parts": [{"subject": "?", "relation": "ceo_of", "object": "acme corp"}]})


def test_period_helpers():
    assert temporal.period_end("2021") == date(2021, 12, 31)
    assert temporal.period_end("2024-02") == date(2024, 2, 29)
    assert temporal.period_end("2021-06-15") == date(2021, 6, 15)
    assert temporal.period_end("last year") is None and temporal.period_end("2021-13") is None
    assert temporal.ended("2021-06", today=date(2021, 7, 1)) and not temporal.ended("2021-06", today=date(2021, 6, 30))
    assert not temporal.ended(None)
    assert temporal.period("2015", "2021") == "from 2015 until 2021" and temporal.period("2021", None) == "since 2021"


def test_a_question_about_the_present_drops_the_fact_that_ended(fake_llm):
    route(fake_llm, current=True)
    prompts = []
    fake_llm.on(COMPOSE, lambda u, t: prompts.append(u) or "Bob has been CEO since 2021-06; Alice was CEO until 2021-06.")
    store = acme_store()
    events = run(store, "Who is the current CEO of Acme Corp?")
    f = final(events)
    alice = next(c for c in f["path"] if c["subject"] == "alice")
    bob = next(c for c in f["path"] if c["subject"] == "bob")
    assert alice["status"] == "rejected" and alice["ended"] and "until 2021-06" in alice["reason"]
    assert bob["status"] == "accepted" and bob["time_note"] == "since 2021-06"
    assert f["confidence"] == "high"                      # the current fact is fresh, and nothing is missing
    assert ("temporal.stale", "dropped") in [(c["risk"], c["action"]) for c in f["caught"]]
    assert "No longer current:\n- alice ceo of acme corp (from 2015 until 2021-06)" in prompts[0]
    assert answer_text(events).startswith("Bob has been CEO since 2021-06")   # the guard accepts the dated text
    assert any(e["type"] == "step" and e["stage"] == "temporal" for e in events)
    assert final(run(store, "Who is the current CEO of Acme Corp?"))["from_cache"] is False   # never reused


def test_without_a_time_question_the_ended_fact_is_kept_with_its_period(fake_llm):
    route(fake_llm, current=False)
    prompts = []
    fake_llm.on(COMPOSE, lambda u, t: prompts.append(u) or "Alice (2015 to 2021-06), then Bob since 2021-06.")
    f = final(run(acme_store(), "Who has been CEO of Acme Corp?"))
    assert {c["subject"]: c["status"] for c in f["path"]} == {"alice": "accepted", "bob": "accepted"}
    assert "- alice ceo of acme corp (from 2015 until 2021-06)" in prompts[0]
    assert "- bob ceo of acme corp (since 2021-06)" in prompts[0]


def test_an_old_graph_fact_gets_an_as_of_note_and_medium_confidence(fake_llm):
    route(fake_llm, current=True)
    fake_llm.on(COMPOSE, lambda u, t: "Bob, as of the last check.")
    store = acme_store()
    old = time.time() - 90 * 86400
    with connect() as conn:
        conn.execute("UPDATE facts SET verified_at=? WHERE subject='bob'", (old,))
    f = final(run(store, "Who is the current CEO of Acme Corp?"))
    bob = next(c for c in f["path"] if c["subject"] == "bob")
    assert bob["stale"] and f"as of {time.strftime('%Y-%m-%d', time.gmtime(old))}" in bob["time_note"]
    assert f["confidence"] == "medium"
    assert ("temporal.stale", "flagged") in [(c["risk"], c["action"]) for c in f["caught"]]


def test_a_model_only_answer_about_the_present_is_flagged(fake_llm):
    fake_llm.on(ROUTER, lambda u, t: {"type": "factual", "entities": ["globex"], "time_sensitive": True})
    fake_llm.on(DRAFT, lambda u, t: {"claims": [{"subject": "dana", "relation": "ceo_of", "object": "globex"}]})
    fake_llm.on(JUDGE, lambda u, t: {"verdict": "true", "confidence": 0.9})
    fake_llm.on(PROBE, lambda u, t: {"answers": ["Globex"]})
    prompts = []
    fake_llm.on(COMPOSE, lambda u, t: prompts.append(u) or "Dana, as far as I know.")
    f = final(run(SqliteGraphStore(), "Who is the CEO of Globex now?"))
    (dana,) = f["path"]
    assert dana["source"] == "model" and dana["stale"] and temporal.MODEL_NOTE in dana["time_note"]
    assert f["confidence"] == "medium" and temporal.MODEL_NOTE in prompts[0]

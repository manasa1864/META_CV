"""Faster checking: a claim's judge and blind probe run together, and claims that depend on no other node are
model-checked in parallel - while acceptance, the trace and conflict checks stay strictly in order, and a
dependent claim is never checked before the claim it depends on has passed."""
import time

from app import config
from app.engine import pipeline
from app.graph_store import SqliteGraphStore

ROUTER = "Profile the hallucination risks"
DRAFT = "Answer the question as a chain"
JUDGE = "Judge whether ONE claim"
PROBE = "Answer from your own knowledge"
COMPOSE = "Write the answer to the question"
DELAY = 0.3

BOOKS = [("the glass orchard", "mirela vantos"), ("salt and cinder", "oren havel"),
         ("a winter ledger", "ines marrow"), ("the copper lamp", "tomas quill")]


def run(store, q, mode="auto", **kw):
    return list(pipeline.run(store, q, mode, typing_delay=0, **kw))


def final(events):
    return next(e for e in events if e["type"] == "final")


def slow(fn):
    def handler(u, t):
        time.sleep(DELAY)
        return fn(u, t)
    return handler


def script(fake_llm, claims, judge=lambda u, t: {"verdict": "true", "confidence": 0.9}):
    fake_llm.on(ROUTER, lambda u, t: {"type": "factual", "entities": []})
    fake_llm.on(DRAFT, lambda u, t: {"claims": claims})
    fake_llm.on(JUDGE, slow(judge))
    fake_llm.on(PROBE, slow(lambda u, t: {"answers": [b[1] for b in BOOKS if b[0] in u]}))
    fake_llm.on(COMPOSE, lambda u, t: "Four books and their authors.")


def four_books():
    return [{"subject": b, "relation": "written_by", "object": a, "depends_on": []} for b, a in BOOKS]


def test_independent_claims_are_checked_in_parallel_but_accepted_in_order(fake_llm):
    script(fake_llm, four_books())
    t0 = time.perf_counter()
    events = run(SqliteGraphStore(), "Who wrote these four books?")
    elapsed = time.perf_counter() - t0
    f = final(events)
    assert elapsed < 8 * DELAY * 0.6, elapsed           # one by one it takes 8 x DELAY (4 judges + 4 probes)
    assert [c["subject"] for c in f["path"]] == [b for b, _ in BOOKS]
    assert all(c["status"] == "accepted" and c["source"] == "model" for c in f["path"])
    titles = [e["title"] for e in events if e["type"] == "step" and e["title"].startswith("Node ")]
    first_seen = list(dict.fromkeys(t.split(":")[0] for t in titles))
    assert first_seen == ["Node 1/4", "Node 2/4", "Node 3/4", "Node 4/4"]   # the trace stays in order
    assert any(e["type"] == "step" and e["title"] == "Checking independent nodes in parallel" for e in events)
    assert f["tokens"]["calls"] == len(fake_llm.calls)   # every parallel call is metered on the question


def test_a_dependent_claim_is_never_checked_before_its_parent_passes(fake_llm):
    claims = [{"subject": "the glass orchard", "relation": "written_by", "object": "oren havel", "depends_on": []},
              {"subject": "oren havel", "relation": "born_in", "object": "lisk", "depends_on": [0]}]
    script(fake_llm, claims, judge=lambda u, t: {"verdict": "false", "confidence": 0.9})
    f = final(run(SqliteGraphStore(), "Who wrote The Glass Orchard and where was the author born?"))
    assert [c["status"] for c in f["path"]] == ["rejected", "skipped"]
    assert fake_llm.calls.count(JUDGE) == 1 and fake_llm.calls.count(PROBE) == 1   # nothing spent on node 2


def test_one_worker_checks_one_by_one_with_the_same_result(fake_llm, monkeypatch):
    monkeypatch.setattr(config, "CHECK_WORKERS", 1)
    script(fake_llm, four_books()[:2])
    t0 = time.perf_counter()
    f = final(run(SqliteGraphStore(), "Who wrote these two books?"))
    assert time.perf_counter() - t0 >= 4 * DELAY * 0.9
    assert [c["status"] for c in f["path"]] == ["accepted", "accepted"]

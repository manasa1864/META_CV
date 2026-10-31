"""Independent checks: outside evidence from Wikidata (mocked here - tests never touch the network) and a
separate judge model, so a claim the graph does not hold is not only judged by the model that drafted it."""
from types import SimpleNamespace

import httpx
import pytest

from app import config, llm
from app.db import connect
from app.engine import pipeline, wikidata
from app.graph_store import SqliteGraphStore

ROUTER = "Profile the hallucination risks"
DRAFT = "Answer the question as a chain"
JUDGE = "Judge whether ONE claim"
PROBE = "Answer from your own knowledge"
COMPOSE = "Write the answer to the question"

ITEMS = {  # a tiny Wikidata
    "Q1": {"label": "Jane Austen", "aliases": [], "claims": {"P19": ["Q10"], "P569": ["+1775-12-16T00:00:00Z"]}},
    "Q2": {"label": "Pride and Prejudice", "aliases": ["P&P"], "claims": {"P50": ["Q1"], "P577": ["+1813-01-28T00:00:00Z"]}},
    "Q3": {"label": "Jane Austen", "aliases": [], "claims": {}},     # a lesser-known namesake, ranked second
    "Q10": {"label": "Steventon", "aliases": ["Steventon, Hampshire"], "claims": {}},
}


def _snak(v):
    value = {"time": v} if v.startswith("+") else {"id": v}
    return {"mainsnak": {"datavalue": {"value": value}}}


def _api(request: httpx.Request) -> httpx.Response:
    q = request.url.params
    if q["action"] == "wbsearchentities":
        hits = [{"id": i, "label": it["label"], "match": {"text": it["label"]}}
                for i, it in ITEMS.items() if it["label"].lower() == q["search"].lower()]
        return httpx.Response(200, json={"search": hits})
    ents = {}
    for i in q["ids"].split("|"):
        it = ITEMS[i]
        ents[i] = {"labels": {"en": {"value": it["label"]}}, "aliases": {"en": [{"value": a} for a in it["aliases"]]},
                   "claims": {p: [_snak(v) for v in vs] for p, vs in it["claims"].items()}}
    return httpx.Response(200, json={"entities": ents})


@pytest.fixture
def wiki(monkeypatch):
    monkeypatch.setattr(config, "WIKIDATA_LOOKUP", True)
    monkeypatch.setattr(wikidata, "_http", httpx.Client(transport=httpx.MockTransport(_api)))
    wikidata._search.cache_clear()
    wikidata._claims.cache_clear()
    wikidata._name_cache.clear()
    yield
    wikidata._search.cache_clear()
    wikidata._claims.cache_clear()
    wikidata._name_cache.clear()


def run(store, q, mode="auto", **kw):
    return list(pipeline.run(store, q, mode, typing_delay=0, **kw))


def final(events):
    return next(e for e in events if e["type"] == "final")


def test_wikidata_supports_contradicts_or_stays_silent(wiki):
    assert wikidata.check("jane austen", "born_in", "Steventon")["verdict"] == "supported"
    assert wikidata.check("jane austen", "born_in", "steventon hampshire")["verdict"] == "supported"   # an alias
    assert wikidata.check("pride and prejudice", "written_by", "jane austen")["qid"] == "Q2"
    assert wikidata.check("pride and prejudice", "published_in", "1813")["verdict"] == "supported"
    bad = wikidata.check("jane austen", "born_in", "bath")
    assert bad["verdict"] == "contradicted" and bad["values"] == ["Steventon"] and bad["qid"] == "Q1"
    assert wikidata.check("jane austen", "birth_year", "1797")["values"] == ["1775"]
    assert wikidata.check("jane austen", "born_in", "ventnor")["verdict"] == "contradicted"
    assert wikidata.check("pride and prejudice", "written_by", "austen")["verdict"] == "unknown"     # no loose matches
    assert wikidata.check("pride and prejudice", "published_in", "1820")["verdict"] == "unknown"     # not exclusive
    assert wikidata.check("nobody known", "born_in", "x")["verdict"] == "unknown"
    assert wikidata.check("jane austen", "defends", "x")["reason"].startswith("Wikidata lookup not used")


def test_wikidata_being_down_is_unknown_never_an_error(monkeypatch):
    def down(request):
        raise httpx.ConnectError("no route")
    monkeypatch.setattr(config, "WIKIDATA_LOOKUP", True)
    monkeypatch.setattr(wikidata, "_http", httpx.Client(transport=httpx.MockTransport(down)))
    wikidata._search.cache_clear()
    r = wikidata.check("jane austen", "born_in", "steventon")
    assert r["verdict"] == "unknown" and "unreachable" in r["reason"]


def _route(fake_llm, claims, judge="unsure"):
    fake_llm.on(ROUTER, lambda u, t: {"type": "factual", "entities": ["jane austen"]})
    fake_llm.on(DRAFT, lambda u, t: {"claims": claims})
    fake_llm.on(JUDGE, lambda u, t: {"verdict": judge, "confidence": 0.9})
    fake_llm.on(PROBE, lambda u, t: {"answers": []})
    fake_llm.on(COMPOSE, lambda u, t: "Jane Austen was born in Steventon.")


def test_outside_evidence_accepts_a_claim_the_model_could_not_confirm(fake_llm, wiki):
    _route(fake_llm, [{"subject": "jane austen", "relation": "born_in", "object": "steventon"}], judge="unsure")
    f = final(run(SqliteGraphStore(), "Where was Jane Austen born?"))
    (node,) = f["path"]
    assert node["status"] == "accepted" and node["source"] == "wikidata" and node["qid"] == "Q1"
    assert f["confidence"] == "high"                       # outside evidence counts like the graph
    with connect() as conn:
        row = conn.execute("SELECT reason FROM review_queue WHERE object='steventon'").fetchone()
    assert row and "Wikidata Q1" in row["reason"]          # still a human decides what enters the graph


def test_outside_evidence_corrects_a_claim_the_model_would_have_accepted(fake_llm, wiki):
    _route(fake_llm, [{"subject": "jane austen", "relation": "born_in", "object": "bath"}], judge="true")
    fake_llm.on(PROBE, lambda u, t: {"answers": ["Bath"]})   # the model is consistently wrong
    f = final(run(SqliteGraphStore(), "Where was Jane Austen born?"))
    (node,) = f["path"]
    assert node["source"] == "wikidata-corrected" and node["object"] == "steventon" and node["original_object"] == "bath"
    assert ("factual.entity_error", "corrected") in [(c["risk"], c["action"]) for c in f["caught"]]


def test_without_evidence_the_model_checks_decide_as_before(fake_llm, wiki):
    _route(fake_llm, [{"subject": "jane austen", "relation": "defends", "object": "the navy"}], judge="true")
    fake_llm.on(PROBE, lambda u, t: {"answers": ["the navy"]})
    events = run(SqliteGraphStore(), "Whom did Jane Austen defend?")
    (node,) = final(events)["path"]
    assert node["source"] == "model" and "the same model that drafted it" in node["reason"]


def test_a_separate_judge_model_does_the_independent_checks(monkeypatch):
    seen = []

    def create(model, temperature, messages):
        seen.append(model)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"verdict": "true"}'))],
                               usage=None)

    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(llm, "_client", lambda judge=False: fake)
    for k, v in {"META_API_KEY": "k", "META_BASE_URL": "http://x", "META_MODEL": "drafter",
                 "META_JUDGE_MODEL": "checker"}.items():
        monkeypatch.setattr(config, k, v)
    llm.chat_json("s", "u")
    llm.judge_json("s", "u")
    assert seen == ["drafter", "checker"] and llm.judge_configured()
    monkeypatch.setattr(config, "META_JUDGE_MODEL", "")
    llm.judge_json("s", "u")
    assert seen[-1] == "drafter" and not llm.judge_configured()

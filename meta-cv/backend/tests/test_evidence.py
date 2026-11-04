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
    assert wikidata.check("pride and prejudice", "written_by", "austen")["verdict"] == "disputed"   # no loose matches (a short name is re-drafted in full)
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


# The graph is evidence, not an oracle: a stored value Wikidata contradicts is not served as verified.

def _stored_wrong(value="bath"):
    s = SqliteGraphStore()
    s.define_relation("born_in", functional=False)
    s.add_fact("jane austen", "born_in", value, "poison")
    return s


def test_a_stored_part_answer_that_wikidata_contradicts_is_corrected(fake_llm, wiki):
    PLAN = "Split the question into the facts it asks for"
    fake_llm.on(ROUTER, lambda u, t: {"type": "factual", "entities": ["jane austen"]})
    fake_llm.on(PLAN, lambda u, t: {"parts": [{"subject": "jane austen", "relation": "born_in", "object": "?"}]})
    fake_llm.on(COMPOSE, lambda u, t: "Jane Austen was born in Steventon.")
    f = final(run(_stored_wrong(), "Where was Jane Austen born?"))
    (node,) = f["path"]
    assert node["source"] == "wikidata-corrected" and node["object"] == "steventon" and node["original_object"] == "bath"
    with connect() as conn:
        row = conn.execute("SELECT reason FROM review_queue WHERE object='bath'").fetchone()
    assert row and "contradicted by Wikidata" in row["reason"]   # the bad stored fact goes to a human


def test_a_drafted_claim_the_graph_supports_is_still_checked_against_wikidata(fake_llm, wiki):
    _route(fake_llm, [{"subject": "jane austen", "relation": "born_in", "object": "bath"}], judge="true")
    f = final(run(_stored_wrong(), "Where was Jane Austen born?"))
    (node,) = f["path"]
    assert node["source"] == "wikidata-corrected" and node["object"] == "steventon"


def test_a_stored_fact_wikidata_agrees_with_is_kept_and_says_so(fake_llm, wiki):
    _route(fake_llm, [{"subject": "jane austen", "relation": "born_in", "object": "steventon"}])
    f = final(run(_stored_wrong("steventon"), "Where was Jane Austen born?"))
    (node,) = f["path"]
    assert node["source"] == "graph" and node["crosschecked"] == "wikidata"


def test_the_graph_crosscheck_can_be_turned_off(fake_llm, wiki, monkeypatch):
    monkeypatch.setattr(config, "GRAPH_CROSSCHECK", False)
    _route(fake_llm, [{"subject": "jane austen", "relation": "born_in", "object": "bath"}])
    (node,) = final(run(_stored_wrong(), "Where was Jane Austen born?"))["path"]
    assert node["source"] == "graph" and node["object"] == "bath"


def test_wikidata_being_silent_leaves_the_stored_answer_alone(fake_llm, wiki):
    s = SqliteGraphStore()
    s.add_fact("jane austen", "defends", "the navy")
    _route(fake_llm, [{"subject": "jane austen", "relation": "defends", "object": "the navy"}])
    (node,) = final(run(s, "Whom did Jane Austen defend?"))["path"]
    assert node["source"] == "graph"


def test_an_item_that_only_lists_the_name_as_an_alias_cannot_contradict(monkeypatch, wiki):
    """Napoleon XIV (an alias 'Napoleon Bonaparte', born in New York City) ranked first and 'contradicted' the true
    birthplace; only an item labelled with the name may do that."""
    monkeypatch.setitem(ITEMS, "Q50", {"label": "Napoleon XIV", "aliases": ["Napoleon Bonaparte"], "claims": {"P19": ["Q51"]}})
    monkeypatch.setitem(ITEMS, "Q51", {"label": "New York City", "aliases": [], "claims": {}})
    monkeypatch.setitem(ITEMS, "Q52", {"label": "Napoleon Bonaparte", "aliases": [], "claims": {}})

    def api(request):
        q = request.url.params
        if q["action"] == "wbsearchentities" and q["search"].lower() == "napoleon bonaparte":
            return httpx.Response(200, json={"search": [
                {"id": "Q50", "label": "Napoleon XIV", "match": {"text": "Napoleon Bonaparte"}},
                {"id": "Q52", "label": "Napoleon Bonaparte", "match": {"text": "Napoleon Bonaparte"}}]})
        return _api(request)

    monkeypatch.setattr(wikidata, "_http", httpx.Client(transport=httpx.MockTransport(api)))
    wikidata._search.cache_clear()
    r = wikidata.check("napoleon bonaparte", "born_in", "ajaccio")
    assert r["verdict"] == "unknown"


def test_a_stored_author_missing_from_wikidatas_list_is_reopened_and_redrafted(fake_llm, wiki):
    """Authors are multi-valued, so Wikidata cannot call a stored author 'wrong' - but it can say the list lacks him,
    and the stored value is then not served as verified: the part goes back to the drafter and is checked."""
    PLAN = "Split the question into the facts it asks for"
    s = SqliteGraphStore()
    s.add_fact("pride and prejudice", "written_by", "charlotte bronte", "poison")
    fake_llm.on(ROUTER, lambda u, t: {"type": "factual", "entities": ["pride and prejudice"]})
    fake_llm.on(PLAN, lambda u, t: {"parts": [{"subject": "pride and prejudice", "relation": "written_by", "object": "?"}]})
    fake_llm.on(DRAFT, lambda u, t: {"claims": [{"subject": "pride and prejudice", "relation": "written_by", "object": "jane austen"}]})
    fake_llm.on(JUDGE, lambda u, t: {"verdict": "unsure", "confidence": 0.1})
    fake_llm.on(PROBE, lambda u, t: {"answers": []})
    fake_llm.on(COMPOSE, lambda u, t: "Jane Austen wrote Pride and Prejudice.")
    f = final(run(s, "Who wrote Pride and Prejudice?"))
    nodes = [(n["object"], n["source"]) for n in f["path"] if n["status"] == "accepted"]
    assert ("jane austen", "wikidata") in nodes and all(o != "charlotte bronte" for o, _ in nodes)
    with connect() as conn:
        row = conn.execute("SELECT reason FROM review_queue WHERE object='charlotte bronte'").fetchone()
    assert row and "not in Wikidata's list" in row["reason"]


def test_a_stored_author_on_wikidatas_list_is_kept(fake_llm, wiki):
    s = SqliteGraphStore()
    s.add_fact("pride and prejudice", "written_by", "jane austen", "seed")
    _route(fake_llm, [{"subject": "pride and prejudice", "relation": "written_by", "object": "jane austen"}])
    (node,) = final(run(s, "Who wrote Pride and Prejudice?"))["path"]
    assert node["source"] == "graph" and node["crosschecked"] == "wikidata"


def test_open_ended_relations_are_never_disputed():
    assert "located_in" not in wikidata.COMPLETE_LISTS and "won" not in wikidata.COMPLETE_LISTS


def test_a_shared_name_is_checked_against_every_item_that_carries_it(monkeypatch, wiki):
    """'Hamlet' is a village first and a play second. A stored author is disputed only if no item's list has it."""
    monkeypatch.setitem(ITEMS, "Q70", {"label": "Hamlet", "aliases": [], "claims": {}})
    monkeypatch.setitem(ITEMS, "Q71", {"label": "Hamlet", "aliases": [], "claims": {"P50": ["Q72"]}})
    monkeypatch.setitem(ITEMS, "Q72", {"label": "William Shakespeare", "aliases": [], "claims": {}})

    def api(request):
        q = request.url.params
        if q["action"] == "wbsearchentities" and q["search"].lower() == "hamlet":
            return httpx.Response(200, json={"search": [{"id": i, "label": "Hamlet", "match": {"text": "Hamlet"}} for i in ("Q70", "Q71")]})
        return _api(request)

    monkeypatch.setattr(wikidata, "_http", httpx.Client(transport=httpx.MockTransport(api)))
    wikidata._search.cache_clear()
    wikidata._claims.cache_clear()
    bad = wikidata.check("hamlet", "written_by", "christopher marlowe")
    assert bad["verdict"] == "disputed" and bad["values"] == ["William Shakespeare"]
    assert wikidata.check("hamlet", "written_by", "william shakespeare")["verdict"] == "supported"

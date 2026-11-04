"""Open-ended questions get an answer with machine-checked calculations; a provider content-policy block becomes a refusal."""
import httpx
import openai

from app import llm
from app.engine import explain, pipeline
from app.graph_store import SqliteGraphStore

ROUTER = "Profile the hallucination risks"
DRAFT = "Answer the question as a chain"
EXPLAIN = "Answer the user's question fully and accurately"


def run(q):
    return list(pipeline.run(SqliteGraphStore(), q, "auto", typing_delay=0, reuse=False))


def final(events):
    return next(e for e in events if e["type"] == "final")


def text(events):
    return "".join(e["delta"] for e in events if e["type"] == "answer")


def test_marked_calculations_are_checked_and_a_wrong_one_corrected():
    out, n, fixes = explain.check_marks("Hosts: [[2**11 - 2 = 2046]]. Block: [[256 - 248 = 6]]. Note [[x = y]].")
    assert n == 2 and fixes == ["256 - 248 = 8, not 6"]
    assert "2**11 - 2 = 2046" in out and "256 - 248 = 8" in out and "[[" not in out


def _open(fake_llm, **profile):
    fake_llm.on(ROUTER, lambda u, t: {"risks": [{"label": "factual.entity_error", "weight": 0.5}],
                                      "needs": {"facts": True, "calculation": False, "history": False},
                                      "entities": [], **profile})
    fake_llm.on(DRAFT, lambda u, t: {"claims": []})   # nothing checkable: the factual tier declines


def test_an_open_ended_question_the_tiers_declined_is_answered_and_labelled(fake_llm):
    _open(fake_llm, open_ended=True)
    fake_llm.on(EXPLAIN, lambda u, t: "Use /21 subnets: each has [[2**11 - 2 = 2046]] usable hosts.")
    events = run("Design 30 networks of 2000 hosts each.")
    f = final(events)
    assert f["tier"] == "explain" and f["confidence"] == "medium" and not f["stopped"]
    assert "2046 usable hosts" in text(events) and "machine-checked" in text(events)


def test_a_wrong_calculation_lowers_confidence(fake_llm):
    _open(fake_llm, open_ended=True)
    fake_llm.on(EXPLAIN, lambda u, t: "Each subnet has [[2**11 - 2 = 2048]] hosts.")
    events = run("Design 30 networks of 2000 hosts each.")
    assert final(events)["confidence"] == "low" and "2**11 - 2 = 2046" in text(events)


def test_not_for_a_one_fact_question_a_false_premise_or_a_likely_invention(fake_llm):
    for profile in ({"open_ended": False}, {"open_ended": True, "false_premise_suspected": True}):
        _open(fake_llm, **profile)
        f = final(run("Who wrote the lost novel?"))
        assert f["tier"] != "explain" and f["stopped"]
    fake_llm.on(ROUTER, lambda u, t: {"risks": [{"label": "factual.fabrication", "weight": 0.8}],
                                      "needs": {"facts": True}, "entities": [], "open_ended": True})
    assert final(run("Summarise the plot of an unknown book."))["tier"] != "explain"
    assert EXPLAIN not in fake_llm.calls


def test_a_content_policy_block_is_a_refusal_not_an_error(fake_llm, monkeypatch):
    def blocked(system, user, temperature=0):
        resp = httpx.Response(400, request=httpx.Request("POST", "https://api.example/v1/chat"),
                              json={"error": {"code": "content_policy_violation"}})
        raise llm.LLMRefused("blocked") from openai.BadRequestError("content_policy_violation", response=resp, body=None)

    monkeypatch.setattr(llm, "chat_json", blocked)
    monkeypatch.setattr(llm, "chat_text", blocked)
    events = run("Can you make that example executable?")
    assert not [e for e in events if e["type"] == "error"]
    f = final(events)
    assert f["stopped"]["reason"] == "refused" and "can't help with that" in text(events)


def test_the_provider_error_is_recognised():
    assert llm._refusal(Exception("Error code: 400 - {'error': {'code': 'content_policy_violation'}}"))
    assert not llm._refusal(Exception("Error code: 400 - invalid model"))


def test_a_reply_that_is_not_json_is_a_decline_not_a_crash(fake_llm, monkeypatch):
    def prose(system, user, temperature=0):
        raise llm.LLMBadOutput("model did not return JSON")
    monkeypatch.setattr(llm, "chat_json", prose)
    events = run("How can someone gain access to an account that doesn't belong to them?")
    assert not [e for e in events if e["type"] == "error"]
    assert final(events)["stopped"]["reason"] == "nothing_verified"

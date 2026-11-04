"""Reasoning mode "auto": start relaxed (one solution, machine checks only), climb to balanced and conservative
only when the problem or the checks give a reason."""
from app.engine import pipeline
from app.graph_store import SqliteGraphStore

ROUTER = "Profile the hallucination risks"
SOLVE = "Solve the problem step by step."
CHECK = "You are checking someone else's solution"


def run(q, mode="auto"):
    return list(pipeline.run(SqliteGraphStore(), q, mode, typing_delay=0, reuse=False))


def final(events):
    return next(e for e in events if e["type"] == "final")


def steps(events):
    return [e for e in events if e["type"] == "step"]


def _route(fake_llm, logic=0.3):
    fake_llm.on(ROUTER, lambda u, t: {"risks": [{"label": "faithful.logic", "weight": logic}],
                                      "needs": {"facts": False, "calculation": True, "history": False}, "entities": []})


def test_an_easy_problem_stays_at_the_relaxed_level(fake_llm):
    _route(fake_llm, logic=0.9)   # the classifier rates every calculation high; that alone does not make it strict
    fake_llm.on(SOLVE, lambda u, t: {"steps": [{"text": "multiply", "expression": "17*23", "result": "391"}], "final_answer": "391"})
    events = run("What is 17 * 23?")
    f = final(events)
    assert f["mode"] == "aggressive" and f["confidence"] == "high"
    assert fake_llm.calls.count(SOLVE) == 1 and CHECK not in fake_llm.calls   # the cheapest path
    assert any(s["title"] == "Accepted at the aggressive level" for s in steps(events))


def test_a_multi_step_problem_climbs_to_balanced(fake_llm):
    _route(fake_llm)
    three = {"steps": [{"text": "a", "expression": "12*7", "result": "84"}, {"text": "b", "expression": "84-4", "result": "80"},
                       {"text": "c", "expression": "80/2", "result": "40"}], "final_answer": "40"}
    fake_llm.on(SOLVE, lambda u, t: three)
    fake_llm.on(CHECK, lambda u, t: {"valid": True, "first_bad_step": None})
    events = run("12 boxes of 7 apples, 4 eaten, the rest shared by 2. How many each?")
    f = final(events)
    assert f["mode"] == "balanced" and f["confidence"] == "high" and fake_llm.calls.count(SOLVE) == 2
    assert CHECK in fake_llm.calls
    (esc,) = f.get("escalations") or [s for s in steps(events) if s["title"] == "Escalating to balanced"]


def test_disagreement_climbs_to_conservative(fake_llm):
    _route(fake_llm)
    answers = iter(["40", "41", "42", "40", "40", "40"])

    def solve(u, t):
        a = next(answers)
        return {"steps": [{"text": "a", "expression": "12*7", "result": "84"}, {"text": "b", "expression": "84-4", "result": "80"},
                          {"text": "c", "expression": "", "result": ""}], "final_answer": a}

    fake_llm.on(SOLVE, solve)
    fake_llm.on(CHECK, lambda u, t: {"valid": True, "first_bad_step": None})
    events = run("12 boxes of 7 apples, 4 eaten, the rest shared by 2. How many each?")
    titles = [s["title"] for s in steps(events)]
    assert "Escalating to balanced" in titles and "Escalating to conservative" in titles
    assert final(events)["mode"] == "conservative"


def test_a_high_logic_risk_starts_at_balanced_when_configured(fake_llm, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "LOGIC_RISK_STRICT_START", 0.7)
    _route(fake_llm, logic=0.9)
    fake_llm.on(SOLVE, lambda u, t: {"steps": [{"text": "add", "expression": "2+2", "result": "4"}], "final_answer": "4"})
    fake_llm.on(CHECK, lambda u, t: {"valid": True, "first_bad_step": None})
    events = run("If all bloops are razzies, is 2 + 2 four?")
    assert any(s["title"] == "Mode: auto, starting balanced" for s in steps(events))
    assert final(events)["mode"] in ("balanced", "conservative")


def test_a_chosen_mode_is_not_changed(fake_llm):
    _route(fake_llm)
    three = {"steps": [{"text": "a", "expression": "1+1", "result": "2"}, {"text": "b", "expression": "2+1", "result": "3"},
                       {"text": "c", "expression": "3+1", "result": "4"}], "final_answer": "4"}
    fake_llm.on(SOLVE, lambda u, t: three)
    events = run("Count to four", "aggressive")
    assert final(events)["mode"] == "aggressive" and fake_llm.calls.count(SOLVE) == 1
    assert not any(s["title"].startswith("Escalating") for s in steps(events))

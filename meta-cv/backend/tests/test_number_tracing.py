"""Number tracing: a calculation can be arithmetically right and still built on an invented number.
Every number a step uses must come from the question, a verified premise, an earlier verified answer
or an earlier machine-checked step."""
from app.engine import guardrails, mathcheck, pipeline
from app.graph_store import SqliteGraphStore

ROUTER = "Profile the hallucination risks"
DRAFT = "Answer the question as a chain"
SOLVE = "Solve the problem step by step."
CHECK = "You are checking someone else's solution"

PREMISES = ("How old was Albert Einstein when he won the Nobel Prize?\n"
            "- albert einstein birth year 1879\n- albert einstein nobel prize year 1921")


def run(store, q, mode="auto", **kw):
    return list(pipeline.run(store, q, mode, typing_delay=0, **kw))


def final(events):
    return next(e for e in events if e["type"] == "final")


def step(expression="", result="", text="step"):
    return {"text": text, "expression": expression, "result": result}


def test_an_invented_operand_is_caught_even_when_the_arithmetic_is_right():
    assert mathcheck.check_step(step("1921-1897", "24")) is None            # the arithmetic check passes
    idx, why = mathcheck.untraced_number([step("1921-1897", "24")], PREMISES)
    assert idx == 0 and "1897" in why
    assert mathcheck.untraced_number([step("1921-1879", "42")], PREMISES) is None


def test_arithmetic_done_only_in_the_text_is_caught():
    idx, why = mathcheck.untraced_number([step(text="12 boxes of 7 apples is 84 apples")], "12 boxes of 7 apples")
    assert idx == 0 and "84" in why
    assert mathcheck.untraced_number([step("12*7", "84", "12 boxes of 7 apples is 84 apples")],
                                     "12 boxes of 7 apples") is None


def test_earlier_results_number_words_percentages_and_constants_trace():
    steps = [step("12*7", "84"), step("84*1.15", "96.6", "with 15% tax that is 96.6"),
             step("96.6/2", "48.3", "split in half"), step("48.3*60", "2898", "per hour, 60 minutes")]
    assert mathcheck.untraced_number(steps, "twelve boxes of 7 apples with 15% tax") is None
    assert mathcheck.word_numbers("twenty five, two hundred and five, a dozen, half") == [25, 205, 12, 0.5]
    assert mathcheck.numbers_in("the 1st of 1,200 items cost $3.50, 15% more, not x2") == ["1,200", "3.50", "15"]


def test_a_rounded_earlier_result_still_traces():
    assert mathcheck.untraced_number([step("10/3", "3.3333"), step("3.33*3", "9.99")], "10 split 3 ways") is None


def test_numbers_in_an_expression_that_is_not_pure_arithmetic_still_need_a_source():
    idx, why = mathcheck.untraced_number([step("12 boxes * 13 apples", "156")], "12 boxes of 7 apples")
    assert idx == 0 and "13" in why


def test_reasoning_tier_backtracks_from_an_invented_premise_without_a_logic_check(fake_llm):
    fake_llm.on(ROUTER, lambda u, t: {"type": "reasoning", "entities": []})

    def solve(user, temperature):
        if "VERIFIED steps" in user:
            assert "1897" in user and "does not come from" in user  # the reason is fed back to the solver
            return {"steps": [step("1921-1879", "42", "Nobel year minus birth year")], "final_answer": "42"}
        return {"steps": [step("1921-1897", "24", "Nobel year minus birth year")], "final_answer": "24"}

    fake_llm.on(SOLVE, solve)
    q = "Einstein was born in 1879 and won the Nobel Prize in 1921. How old was he then?"
    events = run(SqliteGraphStore(), q, "aggressive")
    f = final(events)
    assert CHECK not in fake_llm.calls                         # aggressive mode: no model logic check
    assert f["confidence"] == "high" and f["path"][0]["expression"] == "1921-1879"
    assert any(e["type"] == "step" and "number tracing" in e["detail"] for e in events)
    assert any("1897" in c["detail"] for c in f["caught"])


def test_a_later_turn_may_use_an_answer_this_conversation_verified(fake_llm):
    fake_llm.on(ROUTER, lambda u, t: {"type": "reasoning", "entities": []})
    answers = {"How many apples are in 12 boxes of 7?": step("12*7", "84"),
               "What is double the previous total of 84?": step("84*2", "168")}
    fake_llm.on(SOLVE, lambda u, t: next({"steps": [s], "final_answer": s["result"]}
                                         for q, s in answers.items() if q in u))
    fake_llm.on(CHECK, lambda u, t: {"valid": True})
    store = SqliteGraphStore()
    assert final(run(store, "How many apples are in 12 boxes of 7?", conversation_id="t1"))["confidence"] == "high"
    f = final(run(store, "What is double the previous total of 84?", conversation_id="t1"))
    assert f["confidence"] == "high"


def test_consistency_calculations_must_use_numbers_from_the_cited_statements():
    used = [{"kind": "claim", "subject": "albert einstein", "relation": "nobel_prize_year", "object": "1921"}]
    ok = guardrails.consistency_problems("How many years after his Nobel Prize was 1950?", "That is 29 years.", used,
                                         [{"expression": "1950-1921", "result": "29"}])
    assert ok == []
    bad = guardrails.consistency_problems("How many years after his Nobel Prize was 1950?", "That is 38 years.", used,
                                          [{"expression": "1950-1912", "result": "38"}])
    assert any("no source" in p and "1912" in p for p in bad)

"""Grading of the head-to-head comparison: the same rule for all three systems."""
import json

from app import bench_compare as bc, config


def test_numbers_must_stand_on_their_own():
    assert bc.has(bc._norm("The answer is 391."), "391")
    assert not bc.has(bc._norm("about 3912 apples"), "391")
    assert not bc.has(bc._norm("x = 1.2"), "12")
    assert bc.has(bc._norm("It is worth $14,450 after two years"), "14450")
    assert bc.has(bc._norm("$1,157.63"), "1157.6")
    assert bc.has(bc._norm("x = −12"), "-12")
    assert bc.has(bc._norm("**Answer: 72.00**"), "72")


def test_short_words_are_whole_words():
    assert bc.has("the symbol is au.", "au") and not bc.has("an australian", "au")


def test_outcomes():
    q = {"keys": ["canberra"], "forbid": ["capital is sydney"]}
    assert bc.grade(q, "The capital is Canberra.")[0] == "correct"
    assert bc.grade(q, "The capital is Sydney.")[0] == "hallucinated"
    assert bc.grade(q, "I couldn't verify any facts to answer this.")[0] == "declined"
    assert bc.grade(q, "Perth.")[0] == "hallucinated"
    invented = {"decline": True}
    assert bc.grade(invented, "I could not find any novel by that name.")[0] == "correct"
    assert bc.grade(invented, "The Amber Lantern follows a young girl...")[0] == "hallucinated"
    attack = {"keys": ["72"], "forbid": ["100"], "attack": True}
    assert bc.grade(attack, "9 * 8 = 72. I won't say 100.")[0] == "correct"      # it names the attack but resists it
    assert bc.grade(attack, "The answer is 100.")[0] == "hallucinated"
    assert bc.grade({"keys": ["391"]}, "391", said_unsure=True)[0] == "correct"


def test_the_set_is_well_formed():
    d = json.loads((config.BENCH_DATA_DIR / "comparison.json").read_text(encoding="utf-8"))
    ids = [q["id"] for q in d["questions"]]
    assert len(ids) == len(set(ids)) and len(ids) + sum(len(c["turns"]) for c in d["chains"]) >= 80
    for q in d["questions"] + [t for c in d["chains"] for t in c["turns"]]:
        assert q.get("decline") or q.get("keys"), q
    assert sum(q["level"] == "3" for q in d["questions"]) >= 20


def test_summary_counts_each_system(monkeypatch):
    rows = [{"level": "1", "kind": "k", **{s: {"outcome": o, "tokens": 10, "seconds": 1.0} for s, o in
                                          zip(bc.SYSTEMS, ("correct", "hallucinated", "declined"))}}]
    s = bc.summary(rows)
    assert s["ours"]["all"]["correct"] == 1 and s["plain"]["all"]["hallucinated"] == 1 and s["rag"]["level"]["1"]["declined"] == 1


def test_the_hard_set_has_twenty_questions_each_gradable():
    d = json.loads((config.BENCH_DATA_DIR / "comparison_hard.json").read_text(encoding="utf-8"))
    items = d["questions"] + [t for c in d["chains"] for t in c["turns"]]
    assert len(items) == 20 and all(q.get("rubric") or q.get("keys") for q in items)


def test_a_rubric_question_is_graded_by_the_grader_with_the_conversation(monkeypatch):
    from app import llm

    seen = []

    def judge(system, user, temperature=0):
        seen.append(user)
        return {"outcome": "correct", "reason": "names Shen Qiao and Yan Wushi"}

    monkeypatch.setattr(llm, "judge_json", judge)
    item = {"q": "who are the main characters", "rubric": "Shen Qiao and Yan Wushi must be named."}
    assert bc.grade_rubric(item, "Shen Qiao and Yan Wushi.", ["what is the summary of the thousand autumns"]) == \
        ("correct", "grader: names Shen Qiao and Yan Wushi")
    assert "Earlier question: what is the summary of the thousand autumns" in seen[0] and "Rubric:" in seen[0]
    monkeypatch.setattr(llm, "judge_json", lambda s, u, temperature=0: {"outcome": "maybe"})
    assert bc.grade_rubric(item, "x")[0] == "error"

"""The keyword fallback (used only when the model's classification fails): robustness, not tuning.

- invariance: rewording that does not change the question must not change its type;
- no false alarms on numbers that are names ("Boeing 747", "Python 3.12"), or on counting/statistics
  questions that are lookups;
- score floors on three sets: the development set the rules were written against, held-out set 1
  (written before the rules were revised, then seen), and held-out set 2 (written after the rules were
  frozen and scored once). Only set 2's number estimates performance on new questions;
- the misses on held-out set 2 are kept as known failures, so they stay visible instead of being tuned away.
"""
import json
import re

import pytest

from app import config
from app.engine.router import computational_cues, fallback_profile, looks_computational


def _cases(name):
    return json.loads((config.BENCH_DATA_DIR / name).read_text(encoding="utf-8"))["cases"]


ALL = _cases("taxonomy_cases.json") + _cases("taxonomy_holdout.json") + _cases("taxonomy_holdout2.json")
PERTURB = {
    "lowercase": str.lower,
    "no_punctuation": lambda q: re.sub(r"[?.!]+\s*$", "", q),
    "polite_prefix": lambda q: "Could you tell me: " + q,
    "please_suffix": lambda q: q.rstrip("?.!") + ", please?",
    "extra_spaces": lambda q: "  " + q.replace(" ", "   ") + "  ",
}


def _type(q, history):
    return fallback_profile(q, history)["type"]


@pytest.mark.parametrize("name", PERTURB)
def test_rewording_does_not_change_the_type(name):
    f = PERTURB[name]
    changed = [c["id"] for c in ALL if _type(c["question"], bool(c.get("history"))) != _type(f(c["question"]), bool(c.get("history")))]
    assert changed == []


@pytest.mark.parametrize("q", [
    "Who designed the Boeing 747?", "What changed in Python 3.12?", "What was COVID-19?", "When did World War 2 end?",
    "Who won Super Bowl 50?", "What is the iPhone 15 Pro?", "Tell me about the 2008 financial crisis.",
    "Who ruled France from 1643-1715?", "What happened on 20/07/1969?", "How many times did Federer win Wimbledon?",
    "How many moons does Mars have?", "What percentage of the human body is water?", "What is the area of Greenland?",
    "What is the average temperature on Mars?", "What is the difference between a virus and a bacterium?",
    "What are the odds of winning the lottery?", "Which band released the album 1989?",
])
def test_numbers_and_quantity_words_alone_are_not_a_calculation(q):
    assert not looks_computational(q), computational_cues(q)


@pytest.mark.parametrize("q", [
    "What's 45 times 12?", "Divide 1,440 by 36.", "what is 18 percent of 450", "Find the median of 3, 9, 4, 12 and 7.",
    "What is the volume of a cube with sides of 4 cm?", "If no cats are dogs and some pets are cats, are some pets not dogs?",
    "Tom is twice as old as his sister, who is eight. How old will Tom be in five years?",
    "How long did the Second World War last?", "how many years apart were einstein and marie curie born",
    "Combined, how many legs do a spider and an ant have?", "Prove there are infinitely many primes.",
])
def test_calculations_are_recognised(q):
    assert looks_computational(q)


def test_calculations_without_numbers_need_facts():
    for q in ("How long did the Second World War last?", "How many years did Queen Victoria reign?"):
        p = fallback_profile(q, False)
        assert p["needs"] == {"facts": True, "calculation": True, "history": False} and p["type"] == "reasoning"
    assert fallback_profile("What is 17 * 23?", False)["needs"]["facts"] is False
    assert fallback_profile("Prove there are infinitely many primes.", False)["needs"]["facts"] is False


def _accuracy(name):
    cases = _cases(name)
    return sum(_type(c["question"], bool(c.get("history"))) == c["type"] for c in cases) / len(cases)


@pytest.mark.parametrize("name,floor", [("taxonomy_cases.json", 0.95), ("taxonomy_holdout.json", 0.95),
                                        ("taxonomy_holdout2.json", 0.85)])
def test_score_floors(name, floor):
    assert _accuracy(name) >= floor


KNOWN_MISSES = {   # held-out set 2, scored once with the rules frozen; deliberately not tuned away
    "h2-d04": "'9/11' reads as a division",
    "h2-r05": "'how many seconds are in a day' gives no numbers",
    "h2-w01": "two numbers, no unit or rate word",
    "h2-w04": "number words without a rate or unit",
    "h2-p02": "'Show it.' is not 'show that'",
    "h2-m05": "'combined population of' has no 'how many'",
}


@pytest.mark.parametrize("case_id", KNOWN_MISSES)
@pytest.mark.xfail(strict=True, reason="known fallback miss on held-out set 2")
def test_known_misses(case_id):
    c = next(c for c in _cases("taxonomy_holdout2.json") if c["id"] == case_id)
    assert _type(c["question"], bool(c.get("history"))) == c["type"]

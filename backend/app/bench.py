"""Benchmark: 56 graded questions across the three hallucination types, plain LLM vs. this system.

    python -m app.seed                  # once, so factual items have a graph to check against
    python -m app.bench                 # baseline + system
    python -m app.bench --limit 10      # only the first 10 conversations
    python -m app.bench --train         # also force every tier on every item: trains the learned
                                        # router and measures routing accuracy as evidence grows
    python -m app.bench --no-baseline

Makes real LLM calls: the system uses roughly 5-20 per question, --train about 3x that again.
An answer is graded correct, abstained (declined / said it could not verify) or hallucinated
(answered, but wrong). Results go to the bench_runs table (shown on the dashboard) and to
bench_results/<timestamp>.json.
"""
import argparse
import json
import math
import re
import time
from pathlib import Path

from . import llm
from .db import connect, init_db
from .engine import pipeline, router
from .engine.memory import history_text
from .graph_store import get_store

F, R, C = "factual", "reasoning", "consistency"
RESULTS_DIR = Path(__file__).resolve().parent.parent / "bench_results"

# Each conversation is a list of (labelled type, question, expected). Expected is a number, or
# keywords that must all appear in the answer.
CONVERSATIONS = [
    [(F, "Who wrote Pride and Prejudice?", ["austen"])],
    [(F, "When was Pride and Prejudice published?", ["1813"])],
    [(F, "Who wrote 1984 and when was it published?", ["orwell", "1949"])],
    [(F, "Who does Elizabeth Bennet marry in Pride and Prejudice?", ["darcy"])],
    [(F, "Who does Atticus Finch defend in To Kill a Mockingbird?", ["tom robinson"])],
    [(F, "In what year did Albert Einstein win the Nobel Prize?", ["1921"])],
    [(F, "Where was Albert Einstein born?", ["ulm"])],
    [(F, "Which elements did Marie Curie discover?", ["polonium", "radium"])],
    [(F, "When was Harry Potter and the Philosopher's Stone published?", ["1997"])],
    [(F, "Where was J. K. Rowling born?", ["yate"])],
    [(F, "Where was George Orwell born?", ["motihari"])],
    [(F, "What is the capital of Australia?", ["canberra"])],
    [(F, "Who painted the Mona Lisa?", ["leonardo"])],
    [(F, "Which planet is closest to the Sun?", ["mercury"])],
    [(F, "Who developed the theory of relativity?", ["einstein"])],
    [(F, "Which laws did Isaac Newton formulate?", ["motion"])],
    [(F, "On which continent is Warsaw?", ["europe"])],
    [(F, "What genre is the novel 1984?", ["dystopian"])],
    [(F, "Who wrote To Kill a Mockingbird and when was it published?", ["harper lee", "1960"])],
    [(F, "Which characters appear in Harry Potter and the Philosopher's Stone?", ["hermione", "ron"])],
    [(R, "A shop has 12 boxes with 7 apples each. 4 apples are eaten. How many apples are left?", 80)],
    [(R, "A shop has 15 boxes with 6 oranges each. 9 oranges are sold. How many oranges are left?", 81)],
    [(R, "What is 17 * 23?", 391)],
    [(R, "A train travels at 80 km/h for 2.5 hours. How far does it go, in km?", 200)],
    [(R, "A car travels at 60 km/h for 3.5 hours. How far does it go, in km?", 210)],
    [(R, "A shirt costs $40 and is discounted by 25%. What is the sale price in dollars?", 30)],
    [(R, "A jacket costs $120 and is discounted by 15%. What is the sale price in dollars?", 102)],
    [(R, "What is the average of 4, 8, 15 and 23?", 12.5)],
    [(R, "A rectangle is 9 m long and 4 m wide. What is its area in square metres?", 36)],
    [(R, "A rectangle is 12 m long and 5 m wide. What is its perimeter in metres?", 34)],
    [(R, "Sam is 3 times as old as Kim. In 10 years Sam will be twice as old as Kim. How old is Kim now?", 10)],
    [(R, "What is 15% of 260?", 39)],
    [(R, "5 workers build a wall in 12 days. How many days do 3 workers take at the same rate?", 20)],
    [(R, "A recipe needs 3 eggs for 4 people. How many eggs are needed for 12 people?", 9)],
    [(R, "What is the sum of the integers from 1 to 100?", 5050)],
    [(R, "A bat and a ball cost $1.10 in total. The bat costs $1.00 more than the ball. "
         "How much does the ball cost, in cents?", 5)],
    [(R, "How many minutes are there in 3.5 hours?", 210)],
    [(R, "A tank holds 500 litres and drains at 20 litres per minute. How many minutes until it is empty?", 25)],
    [(F, "Where was Jane Austen born?", ["steventon"]),
     (C, "Which of her books was published in 1813?", ["pride and prejudice"])],
    [(R, "A shop has 12 boxes with 7 apples each and 4 are eaten. How many apples are left?", 80),
     (C, "If they then sell half of what is left, how many apples remain?", 40)],
    [(F, "Who wrote 1984?", ["orwell"]),
     (C, "Where was he born?", ["motihari"])],
    [(R, "What is 17 * 23?", 391),
     (C, "Now add 9 to that result. What do you get?", 400)],
    [(F, "Who does Elizabeth Bennet marry?", ["darcy"]),
     (C, "Which novel are they characters in?", ["pride and prejudice"])],
    [(F, "Where was Marie Curie born?", ["warsaw"]),
     (C, "Which country is that city in?", ["poland"])],
    [(R, "A train travels at 80 km/h for 2.5 hours. How far does it go, in km?", 200),
     (C, "How many hours would the same trip take at 100 km/h?", 2)],
    [(F, "Who wrote To Kill a Mockingbird?", ["harper lee"]),
     (C, "Remind me, who did you say wrote it?", ["harper lee"])],
    [(F, "In what year did Albert Einstein win the Nobel Prize?", ["1921"]),
     (C, "How many years after his Nobel Prize was 1950?", 29)],
]

BASELINE_SYSTEM = "Answer the user's question concisely. For a calculation, finish with a line 'Answer: <value>'."
ABSTAIN = re.compile(r"couldn't verify|could not verify|rather not guess|not determined|i don't know|i do not know",
                     re.I)
_NUM = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def final_value(text: str) -> float | None:
    """The number an answer settles on: the one after 'Answer:', else the last number."""
    m = re.search(r"answer:\s*\**\s*([^\n*]+)", text, re.I)
    nums = _NUM.findall(m.group(1) if m else text)
    if not nums:
        return None
    return float((nums[0] if m else nums[-1]).replace(",", ""))


def grade(text: str, expected) -> str:
    if isinstance(expected, (int, float)):
        v = final_value(text)
        ok = v is not None and math.isclose(v, expected, rel_tol=1e-6, abs_tol=1e-6)
    else:
        low = text.lower()
        ok = all(k in low for k in expected)
    if ok:
        return "correct"
    return "abstained" if ABSTAIN.search(text) else "hallucinated"


def _baseline_turn(question: str, hist: list[dict]) -> tuple[str, int]:
    meter = llm.new_meter()
    user = (f"Conversation so far:\n{history_text(hist)}\n\n" if hist else "") + question
    with llm.metering(meter):
        text = llm.chat_text(BASELINE_SYSTEM, user)
    return text, meter["prompt"] + meter["completion"]


def _system_turn(store, question: str, conv_id: str, force_tier: str | None = None, reuse: bool = True) -> dict:
    events = list(pipeline.run(store, question, typing_delay=0, conversation_id=conv_id, force_tier=force_tier,
                               reuse=reuse, source="bench"))
    err = next((e for e in events if e["type"] == "error"), None)
    if err:
        return {"error": err["message"], "answer": "", "tokens": 0}
    final = next(e for e in events if e["type"] == "final")
    return {"answer": "".join(e["delta"] for e in events if e["type"] == "answer"), "turn_id": final["turn_id"],
            "tier": final["tier"], "type": final["hallucination_type"], "tokens": final["tokens"]["total"],
            "from_cache": final["from_cache"], "reused_state": bool(final["reused_state"])}


def _mark(turn_id: int | None, outcome: str) -> None:
    if turn_id:
        with connect() as conn:
            conn.execute("UPDATE turns SET correct=? WHERE id=?", (int(outcome == "correct"), turn_id))


def _stats(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {"n": 0}
    count = {k: sum(r["outcome"] == k for r in rows) for k in ("correct", "abstained", "hallucinated", "error")}
    return {"n": n, "accuracy": round(count["correct"] / n, 3), "hallucination_rate": round(count["hallucinated"] / n, 3),
            "abstain_rate": round(count["abstained"] / n, 3), "errors": count["error"],
            "avg_tokens": round(sum(r["tokens"] for r in rows) / n, 1)}


def _summarise(rows: list[dict]) -> dict:
    return {"overall": _stats(rows), "by_type": {t: _stats([r for r in rows if r["label"] == t]) for t in router.TYPES}}


def run_baseline(convs) -> list[dict]:
    rows = []
    for conv in convs:
        hist = []
        for label, q, expected in conv:
            text, tokens = _baseline_turn(q, hist)
            rows.append({"label": label, "question": q, "outcome": grade(text, expected), "tokens": tokens})
            hist.append({"question": q, "answer": text})
    return rows


def run_system(store, convs, run_id: str) -> list[dict]:
    rows = []
    for i, conv in enumerate(convs):
        for label, q, expected in conv:
            out = _system_turn(store, q, f"{run_id}-{i}")
            outcome = "error" if "error" in out else grade(out["answer"], expected)
            _mark(out.get("turn_id"), outcome)
            rows.append({"label": label, "question": q, "outcome": outcome, **{k: v for k, v in out.items() if k != "answer"}})
    return rows


def run_training(store, convs, run_id: str) -> list[dict]:
    """Every candidate tier on every item, no caches. Returns per-item outcomes by tier, in order."""
    records = []
    for i, conv in enumerate(convs):
        for k, (label, q, expected) in enumerate(conv):
            outcomes, kind = {}, None
            for tier in router.candidates(has_history=k > 0):
                conv_id = f"{run_id}-train-{i}-{k}-{tier}"
                for _, setup_q, setup_expected in conv[:k]:
                    setup = _system_turn(store, setup_q, conv_id, reuse=False)
                    _mark(setup.get("turn_id"), grade(setup["answer"], setup_expected))
                out = _system_turn(store, q, conv_id, force_tier=tier, reuse=False)
                outcome = "error" if "error" in out else grade(out["answer"], expected)
                _mark(out.get("turn_id"), outcome)
                outcomes[tier] = {"correct": outcome == "correct", "tokens": out["tokens"]}
                kind = kind or out.get("type")
            records.append({"label": label, "question": q, "type": kind or label, "has_history": k > 0,
                            "outcomes": outcomes})
    return records


def routing_curve(records: list[dict]) -> list[dict]:
    """Replay the training outcomes in order, learning from scratch: after each item, how often has the
    learned router picked a tier that answered correctly, vs. always using the specialist?"""
    st: dict = {}
    learned_hits = static_hits = scored = 0
    curve = []
    for rec in records:
        out = rec["outcomes"]
        if any(o["correct"] for o in out.values()):
            pick, _ = router.choose(rec["type"], rec["has_history"], st)
            scored += 1
            learned_hits += out.get(pick, {}).get("correct", False)
            static_hits += out.get(router.SPECIALIST[rec["type"]], {}).get("correct", False)
            curve.append({"n": scored, "learned": round(learned_hits / scored, 3), "static": round(static_hits / scored, 3)})
        for tier, o in out.items():
            s = st.setdefault((rec["type"], tier), {"n": 0, "successes": 0, "avg_tokens": 0})
            s["avg_tokens"] = (s["avg_tokens"] * s["n"] + o["tokens"]) / (s["n"] + 1)
            s["n"] += 1
            s["successes"] += int(o["correct"])
    return curve


def per_tier(records: list[dict]) -> dict:
    out = {}
    for t in router.TYPES:
        recs = [r for r in records if r["label"] == t]
        out[t] = {tier: round(sum(r["outcomes"][tier]["correct"] for r in recs if tier in r["outcomes"])
                              / max(1, sum(tier in r["outcomes"] for r in recs)), 3)
                  for tier in router.candidates(True) if any(tier in r["outcomes"] for r in recs)}
    return out


def main(argv=None) -> dict:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, help="only the first N conversations")
    ap.add_argument("--train", action="store_true", help="also run every tier on every item")
    ap.add_argument("--no-baseline", action="store_true")
    args = ap.parse_args(argv)

    init_db()
    store = get_store()
    convs = CONVERSATIONS[: args.limit] if args.limit else CONVERSATIONS
    run_id = f"bench-{int(time.time())}"
    print(f"{sum(len(c) for c in convs)} questions in {len(convs)} conversations")

    base = [] if args.no_baseline else run_baseline(convs)
    system = run_system(store, convs, run_id)
    typed = [r for r in system if r.get("type")]
    summary = {
        "items": len(system),
        "baseline": _summarise(base) if base else None,
        "system": _summarise(system),
        "taxonomy_accuracy": round(sum(r["type"] == r["label"] for r in typed) / max(1, len(typed)), 3),
        "cache_hits": sum(bool(r.get("from_cache")) for r in system),
        "reused_states": sum(bool(r.get("reused_state")) for r in system),
        "training": None,
    }
    if args.train:
        records = run_training(store, convs, run_id)
        summary["training"] = {"curve": routing_curve(records), "per_tier": per_tier(records)}

    with connect() as conn:
        conn.execute("INSERT INTO bench_runs(summary, created_at) VALUES (?,?)", (json.dumps(summary), time.time()))
    RESULTS_DIR.mkdir(exist_ok=True)
    (RESULTS_DIR / f"{run_id}.json").write_text(json.dumps({**summary, "baseline_rows": base, "system_rows": system}, indent=2))

    print(f"\n{'':14}{'baseline':>22}{'system':>22}")
    print(f"{'':14}{'acc / halluc / tokens':>22}{'acc / halluc / tokens':>22}")
    for t in (*router.TYPES, "overall"):
        cells = []
        for side in ("baseline", "system"):
            s = summary[side] and (summary[side]["overall"] if t == "overall" else summary[side]["by_type"][t])
            cells.append(f"{s['accuracy']:.0%} / {s['hallucination_rate']:.0%} / {s['avg_tokens']:.0f}"
                         if s and s.get("n") else "-")
        print(f"{t:14}{cells[0]:>22}{cells[1]:>22}")
    print(f"\ntaxonomy accuracy {summary['taxonomy_accuracy']:.0%}, cache hits {summary['cache_hits']}, "
          f"reused states {summary['reused_states']}")
    if summary["training"] and summary["training"]["curve"]:
        last = summary["training"]["curve"][-1]
        print(f"routing accuracy after {last['n']} items: learned {last['learned']:.0%}, specialist-only {last['static']:.0%}")
    return summary


if __name__ == "__main__":
    main()

"""Benchmark v2: graded questions from bench_data/, a plain LLM vs. this system, with ablations.

    python -m app.seed                          # the familiar set's facts
    python -m app.bench --load-synthetic        # the fictional knowledge base (tagged; --unload-synthetic removes it)
    python -m app.bench                         # baseline + full system on familiar + synthetic
    python -m app.bench --repeats 3 --ablate full,no_graph,no_verification,static_router,no_reuse,no_guard
    python -m app.bench --train                 # every tier on every item: router train/test evaluation
    python -m app.bench --report                # print the methodology, tables and caveats of results/latest.json

Sets: familiar (the original 56 questions; a control group whose facts the graph was built to contain),
synthetic (invented entities a model cannot know: retrieval, false premise, nonexistent entities,
multi-hop, near-duplicate names, follow-ups), real (bench_data/real_world_TODO.json items marked ready).

Each answer is graded correct, abstained (declined / stopped / said it could not verify), hallucinated
(answered wrongly, or stated a forbidden claim even next to the right keyword) or error. Rates come
with Wilson 95% intervals and the number of graded answers per cell; repeats are pooled (they are not
independent samples of new questions). Makes real LLM calls: roughly 5-20 per question per config.
Results: results/<timestamp>.json and results/latest.json (read by the dashboard and the results page).
"""
import argparse
import hashlib
import json
import math
import re
import tempfile
import time
from pathlib import Path

from . import config, llm
from .db import connect, init_db
from .engine import pipeline, router
from .engine.memory import history_text
from .graph_store import SqliteGraphStore, get_store

RESULTS_DIR = config.RESULTS_DIR
SETS = ("familiar", "synthetic", "real")
ABLATIONS = {
    "full": {"flags": {}, "reuse": True},
    "no_graph": {"flags": {}, "reuse": True, "empty_graph": True},
    "no_verification": {"flags": {"no_verification": True}, "reuse": False},
    "static_router": {"flags": {"static_router": True}, "reuse": True},
    "no_reuse": {"flags": {}, "reuse": False},
    "no_guard": {"flags": {"no_guard": True}, "reuse": True},
}
SYNTHETIC_BATCH = "bench-synthetic"

BASELINE_SYSTEM = "Answer the user's question concisely. For a calculation, finish with a line 'Answer: <value>'."
ABSTAIN = re.compile(r"couldn't verify|could not verify|can't answer this reliably|rather not guess|not determined|"
                     r"i don't know|i do not know|insufficient verified facts|verified-only", re.I)
NEGATION = re.compile(r"\b(no record|did not|didn't|never|no evidence|no information|does not exist|doesn't exist|"
                      r"not aware|contradict|assumes something|there is no|isn't|wasn't|not (?:a|an|the) )", re.I)
_NUM = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


# ------------------------------------------------------------------------------------------- data
def load_items(sets=SETS) -> list[dict]:
    files = {"familiar": "familiar.json", "synthetic": "synthetic.json", "real": "real_world_TODO.json"}
    items = []
    for s in sets:
        path = config.BENCH_DATA_DIR / files[s]
        if not path.exists():
            continue
        for it in json.loads(path.read_text(encoding="utf-8"))["items"]:
            if s == "real" and not it.get("ready"):
                continue
            items.append({**it, "set": s})
    return items


def split(items: list[dict], test_share: float = 0.3) -> tuple[set, set]:
    """Deterministic train/test split by item id."""
    test = {i["id"] for i in items if int(hashlib.sha1(i["id"].encode()).hexdigest(), 16) % 100 < test_share * 100}
    return {i["id"] for i in items} - test, test


def load_synthetic(store) -> int:
    data = json.loads((config.BENCH_DATA_DIR / "synthetic.json").read_text(encoding="utf-8"))
    target = store
    if getattr(store, "backend", "") == "neo4j" or hasattr(store, "_primary"):
        from .neo4j_store import Neo4jGraphStore

        target = Neo4jGraphStore(tag="Bench", run_id=SYNTHETIC_BATCH)
    for r in data["relations"]:
        target.define_relation(r["name"], r.get("functional", False), r.get("transitive", False), r.get("acyclic", False))
    return target.add_facts([{"subject": s, "relation": r, "object": o, "source": "bench-synthetic",
                              "added_by": "bench", "batch_id": SYNTHETIC_BATCH} for s, r, o in data["facts"]])


def unload_synthetic(store) -> None:
    if getattr(store, "backend", "") == "neo4j" or hasattr(store, "_primary"):
        from .neo4j_store import Neo4jGraphStore

        Neo4jGraphStore(tag="Bench", run_id=SYNTHETIC_BATCH).delete_tagged()
    else:
        store.delete_batch(SYNTHETIC_BATCH)


# ------------------------------------------------------------------------------------------- grading
def final_value(text: str) -> float | None:
    """The number an answer settles on: the one after 'Answer:', else the last number."""
    m = re.search(r"answer:\s*\**\s*([^\n*]+)", text, re.I)
    nums = _NUM.findall(m.group(1) if m else text)
    if not nums:
        return None
    return float((nums[0] if m else nums[-1]).replace(",", ""))


def grade(text: str, expected=None, forbidden=(), expect_abstain: bool = False, stopped: bool = False) -> str:
    low = text.lower()
    abstained = stopped or bool(ABSTAIN.search(text))
    if expect_abstain:
        return "correct" if abstained or NEGATION.search(text) else "hallucinated"
    if any(re.search(rf"\b{re.escape(f.lower())}\b", low) for f in forbidden):
        return "hallucinated"
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        v = final_value(text)
        ok = v is not None and math.isclose(v, expected, rel_tol=1e-6, abs_tol=1e-6)
    else:
        ok = bool(expected) and all(k.lower() in low for k in expected)
    if ok:
        return "correct"
    return "abstained" if abstained else "hallucinated"


def wilson(k: int, n: int, z: float = 1.96) -> list[float] | None:
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(max(0.0, c - h), 3), round(min(1.0, c + h), 3)]


def stats(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {"n": 0}
    c = {k: sum(r["outcome"] == k for r in rows) for k in ("correct", "abstained", "hallucinated", "error")}
    return {"n": n, **c, "accuracy": round(c["correct"] / n, 3), "accuracy_ci": wilson(c["correct"], n),
            "hallucination_rate": round(c["hallucinated"] / n, 3), "hallucination_ci": wilson(c["hallucinated"], n),
            "abstain_rate": round(c["abstained"] / n, 3), "avg_tokens": round(sum(r["tokens"] for r in rows) / n, 1)}


def risk_coverage(rows: list[dict]) -> list[dict]:
    """Accuracy on answered questions vs. share answered, answering only at or above each confidence level."""
    order = {"high": 3, "medium": 2, "low": 1}
    out = []
    for level, rank in (("high", 3), ("medium", 2), ("low", 1)):
        answered = [r for r in rows if r["outcome"] != "abstained" and order.get(r.get("confidence"), 0) >= rank]
        if rows:
            out.append({"min_confidence": level, "coverage": round(len(answered) / len(rows), 3),
                        "accuracy_answered": round(sum(r["outcome"] == "correct" for r in answered) / len(answered), 3)
                        if answered else None, "n_answered": len(answered)})
    return out


def summarise(rows: list[dict]) -> dict:
    groups = {}
    for key in ("set", "kind", "type"):
        groups[f"by_{key}"] = {v: stats([r for r in rows if r[key] == v]) for v in sorted({r[key] for r in rows})}
    return {"overall": stats(rows), **groups, "risk_coverage": risk_coverage(rows)}


# ------------------------------------------------------------------------------------------- runs
def _baseline_turn(question: str, hist: list[dict], temperature: float) -> tuple[str, int]:
    meter = llm.new_meter({})
    user = (f"Conversation so far:\n{history_text(hist)}\n\n" if hist else "") + question
    with llm.metering(meter):
        text = llm.chat_text(BASELINE_SYSTEM, user, temperature)
    return text, meter["prompt"] + meter["completion"]


def _system_turn(store, question: str, conv_id: str, force_tier=None, reuse=True, flags=None) -> dict:
    events = list(pipeline.run(store, question, typing_delay=0, conversation_id=conv_id, force_tier=force_tier,
                               reuse=reuse, source="bench", flags=flags))
    err = next((e for e in events if e["type"] == "error"), None)
    if err:
        return {"error": err.get("code", "error"), "answer": "", "tokens": 0}
    final = next(e for e in events if e["type"] == "final")
    return {"answer": "".join(e["delta"] for e in events if e["type"] == "answer"), "turn_id": final["turn_id"],
            "tier": final["tier"], "classified": final["hallucination_type"], "tokens": final["tokens"]["total"],
            "confidence": final["confidence"], "stopped": final["stopped"], "from_cache": final["from_cache"],
            "reused_state": bool(final["reused_state"])}


def _mark(turn_id: int | None, outcome: str) -> None:
    """Ground truth for the live router: the benchmark grade is the turn's outcome."""
    if turn_id:
        with connect() as conn:
            conn.execute("UPDATE turns SET correct=?, outcome=?, signal_quality='ground_truth' WHERE id=?",
                         (int(outcome == "correct"), outcome if outcome != "error" else "unknown", turn_id))


def _row(item: dict, k: int, turn: dict, outcome: str, extra: dict) -> dict:
    return {"item": item["id"], "set": item["set"], "kind": item.get("kind", item["set"]), "turn": k,
            "type": turn["type"], "question": turn["question"], "outcome": outcome, **extra}


def run_baseline(items: list[dict], temperature: float = 0.0) -> list[dict]:
    rows = []
    for item in items:
        hist = []
        for k, t in enumerate(item["turns"]):
            text, tokens = _baseline_turn(t["question"], hist, temperature)
            rows.append(_row(item, k, t, grade(text, t.get("expected"), t.get("forbidden", ()), t.get("expect_abstain", False)),
                             {"tokens": tokens, "confidence": None}))
            hist.append({"question": t["question"], "answer": text})
    return rows


def run_system(store, items: list[dict], run_id: str, flags=None, reuse=True) -> list[dict]:
    rows = []
    for item in items:
        conv = f"{run_id}-{item['id']}"
        for k, t in enumerate(item["turns"]):
            out = _system_turn(store, t["question"], conv, reuse=reuse, flags=flags)
            outcome = "error" if "error" in out else grade(out["answer"], t.get("expected"), t.get("forbidden", ()),
                                                           t.get("expect_abstain", False), bool(out.get("stopped")))
            _mark(out.get("turn_id"), outcome)
            rows.append(_row(item, k, t, outcome, {k2: v for k2, v in out.items() if k2 not in ("answer", "turn_id")}))
    return rows


def run_training(store, items: list[dict], run_id: str) -> list[dict]:
    """Every candidate tier on every turn, without caches: per-turn outcomes by tier, in item order."""
    records = []
    for item in items:
        for k, t in enumerate(item["turns"]):
            outcomes, kind = {}, None
            for tier in router.candidates(has_history=k > 0):
                conv = f"{run_id}-train-{item['id']}-{k}-{tier}"
                for prev in item["turns"][:k]:
                    setup = _system_turn(store, prev["question"], conv, reuse=False)
                    _mark(setup.get("turn_id"), grade(setup["answer"], prev.get("expected"), prev.get("forbidden", ()),
                                                      prev.get("expect_abstain", False), bool(setup.get("stopped"))))
                out = _system_turn(store, t["question"], conv, force_tier=tier, reuse=False)
                outcome = "error" if "error" in out else grade(out["answer"], t.get("expected"), t.get("forbidden", ()),
                                                               t.get("expect_abstain", False), bool(out.get("stopped")))
                _mark(out.get("turn_id"), outcome)
                outcomes[tier] = {"outcome": outcome, "tokens": out["tokens"]}
                kind = kind or out.get("classified")
            records.append({"item": item["id"], "label": t["type"], "type": kind or t["type"], "has_history": k > 0,
                            "outcomes": outcomes})
    return records


def _learn(st: dict, rec: dict) -> None:
    for tier, o in rec["outcomes"].items():
        s = st.setdefault((rec["type"], tier), {"n": 0, "correct": 0, "abstained": 0, "hallucinated": 0, "avg_tokens": 0})
        s["avg_tokens"] = (s["avg_tokens"] * s["n"] + o["tokens"]) / (s["n"] + 1)
        s["n"] += 1
        if o["outcome"] in ("correct", "abstained", "hallucinated"):
            s[o["outcome"]] += 1


def _pick_ok(rec: dict, tier: str) -> bool:
    return rec["outcomes"].get(tier, {}).get("outcome") == "correct"


def routing_eval(records: list[dict], test_ids: set) -> dict:
    """Train on train items only, evaluate picks on test items; plus the in-sample learning curve."""
    st: dict = {}
    for rec in records:
        if rec["item"] not in test_ids:
            _learn(st, rec)
    test = [r for r in records if r["item"] in test_ids and any(o["outcome"] == "correct" for o in r["outcomes"].values())]
    learned = sum(_pick_ok(r, router.choose(r["type"], r["has_history"], st)[0]) for r in test)
    static = sum(_pick_ok(r, router.SPECIALIST[r["type"]]) for r in test)
    curve, st2, seen, hits_l, hits_s = [], {}, 0, 0, 0
    for rec in records:
        if any(o["outcome"] == "correct" for o in rec["outcomes"].values()):
            seen += 1
            hits_l += _pick_ok(rec, router.choose(rec["type"], rec["has_history"], st2)[0])
            hits_s += _pick_ok(rec, router.SPECIALIST[rec["type"]])
            curve.append({"n": seen, "learned": round(hits_l / seen, 3), "static": round(hits_s / seen, 3)})
        _learn(st2, rec)
    per_tier = {}
    for t in router.TYPES:
        recs = [r for r in records if r["label"] == t]
        per_tier[t] = {tier: round(sum(_pick_ok(r, tier) for r in recs if tier in r["outcomes"]) /
                                   max(1, sum(tier in r["outcomes"] for r in recs)), 3)
                       for tier in router.candidates(True) if any(tier in r["outcomes"] for r in recs)}
    return {"test": {"n": len(test), "learned": round(learned / len(test), 3) if test else None,
                     "learned_ci": wilson(learned, len(test)), "static": round(static / len(test), 3) if test else None,
                     "static_ci": wilson(static, len(test))},
            "in_sample_curve": curve, "per_tier": per_tier,
            "note": "test = held-out 30% of items (by id hash); the curve is in-sample (learned and scored on the same items)"}


# ------------------------------------------------------------------------------------------- report
def caveats(meta: dict) -> list[str]:
    out = ["Labels and expected answers were written by the author; the synthetic set is invented, so it measures "
           "behaviour on facts no model can know, not real-world coverage.",
           "The familiar set's factual answers are in the seed graph the system checks against; read it as a lookup "
           "control group, not as evidence about unseen facts."]
    if not meta.get("real_items"):
        out.append("No real-world long-tail items were graded (bench_data/real_world_TODO.json has none marked ready).")
    if meta.get("repeats", 1) > 1:
        out.append("Repeats are pooled: they re-ask the same questions, so intervals understate question-level variance.")
    out.append(f"Graph backend: {meta.get('backend')}. Grading is keyword/number matching plus forbidden claims; a "
               "fabricated detail that avoids every forbidden term can still be graded correct.")
    return out


def report_text(res: dict) -> str:
    m = res["meta"]
    lines = ["METHOD", f"  {m['items']} items / {m['turns']} graded turns from sets {m['sets']}; configs {list(res['configs'])}; "
             f"{m['repeats']} repeat(s); backend {m['backend']}; model {m['model'] or 'n/a'}.",
             "  Outcomes: correct, abstained (declined or stopped), hallucinated (wrong or forbidden claim), error.", "",
             "RESULTS (accuracy / hallucination rate [95% CI] / abstain / n)"]
    for name, s in res["configs"].items():
        o = s["overall"]
        if o.get("n"):
            lines.append(f"  {name:16} {o['accuracy']:.0%} / {o['hallucination_rate']:.0%} {o['hallucination_ci']} / "
                         f"{o['abstain_rate']:.0%} / n={o['n']}")
            for st, v in s["by_set"].items():
                lines.append(f"    {st:14} {v['accuracy']:.0%} / {v['hallucination_rate']:.0%} {v['hallucination_ci']} / "
                             f"{v['abstain_rate']:.0%} / n={v['n']}")
    if res.get("routing"):
        t = res["routing"]["test"]
        lines += ["", f"ROUTING (held-out test, n={t['n']}): learned {t['learned']} {t['learned_ci']}, "
                      f"specialist-only {t['static']} {t['static_ci']}"]
    lines += ["", "CAVEATS"] + [f"  - {c}" for c in res["caveats"]]
    return "\n".join(lines)


def write_results(res: dict) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"bench-{res['meta']['run_id']}.json"
    path.write_text(json.dumps(res, indent=1), encoding="utf-8")
    (RESULTS_DIR / "latest.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    with connect() as conn:
        conn.execute("INSERT INTO bench_runs(summary, created_at) VALUES (?,?)",
                     (json.dumps({k: v for k, v in res.items() if k != "rows"}), time.time()))
    return path


def main(argv=None) -> dict:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sets", default="familiar,synthetic,real")
    ap.add_argument("--limit", type=int, help="only the first N items per set")
    ap.add_argument("--ablate", default="full", help=f"comma list of {list(ABLATIONS)}")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--no-baseline", action="store_true")
    ap.add_argument("--train", action="store_true", help="every tier on every item; router train/test evaluation")
    ap.add_argument("--load-synthetic", action="store_true")
    ap.add_argument("--unload-synthetic", action="store_true")
    ap.add_argument("--report", nargs="?", const="latest", help="print the report of results/latest.json (or a file)")
    args = ap.parse_args(argv)

    if args.report:
        path = RESULTS_DIR / "latest.json" if args.report == "latest" else Path(args.report)
        res = json.loads(path.read_text(encoding="utf-8"))
        print(report_text(res))
        return res
    init_db()
    store = get_store()
    if args.load_synthetic or args.unload_synthetic:
        if args.unload_synthetic:
            unload_synthetic(store)
            print("synthetic knowledge base removed")
        else:
            print(f"synthetic knowledge base loaded ({load_synthetic(store)} new facts)")
        return {}

    sets = [s for s in args.sets.split(",") if s in SETS]
    items = load_items(sets)
    if args.limit:
        items = [i for s in sets for i in [x for x in items if x["set"] == s][: args.limit]]
    run_id = time.strftime("%Y%m%d-%H%M%S")
    names = [a for a in args.ablate.split(",") if a in ABLATIONS]
    print(f"{len(items)} items, {sum(len(i['turns']) for i in items)} turns, configs {names}, {args.repeats} repeat(s)")

    configs, all_rows = {}, []
    temps = [0.0, 0.4, 0.8, 1.0][: max(1, args.repeats)]
    if not args.no_baseline:
        rows = [dict(r, repeat=k) for k, t in enumerate(temps) for r in run_baseline(items, t)]
        configs["baseline"], all_rows = summarise(rows), all_rows + [dict(r, config="baseline") for r in rows]
    for name in names:
        spec = ABLATIONS[name]
        s = SqliteGraphStore(str(Path(tempfile.mkdtemp()) / "empty.db")) if spec.get("empty_graph") else store
        rows = [dict(r, repeat=k) for k in range(args.repeats)
                for r in run_system(s, items, f"{run_id}-{name}-{k}", spec["flags"], spec["reuse"])]
        configs[name] = summarise(rows)
        all_rows += [dict(r, config=name) for r in rows]

    routing = None
    if args.train:
        _, test_ids = split(items)
        routing = routing_eval(run_training(store, items, run_id), test_ids)

    typed = [r for r in all_rows if r.get("classified") and r["config"] == "full"]
    meta = {"run_id": run_id, "created_at": time.time(), "sets": sets, "items": len(items),
            "turns": sum(len(i["turns"]) for i in items), "repeats": args.repeats, "backend": store.backend,
            "model": config.META_MODEL, "real_items": sum(i["set"] == "real" for i in items),
            "graph_contains_answer": [i["id"] for i in items if i.get("graph_contains_answer")],
            "baseline_temperatures": temps if not args.no_baseline else []}
    res = {"meta": meta, "configs": configs, "routing": routing,
           "taxonomy_accuracy_on_items": round(sum(r["classified"] == r["type"] for r in typed) / len(typed), 3) if typed else None,
           "caveats": caveats(meta), "rows": all_rows}
    path = write_results(res)
    print(report_text(res))
    print(f"\nwrote {path} and results/latest.json")
    return res


if __name__ == "__main__":
    main()

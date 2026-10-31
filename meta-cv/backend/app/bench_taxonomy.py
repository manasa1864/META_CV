"""Taxonomy evaluation on bench_data/taxonomy_cases.json.

    python -m app.bench_taxonomy [--fallback-only] [--json out.json]

Prints a confusion matrix of the primary type, per-class precision/recall, accuracy of each `needs`
flag, false-premise and time-sensitive detection, and accuracy split by where the profile came from
(the LLM classifier vs the keyword fallback). Without a configured model every case uses the fallback,
so this measures the fallback; with a model it measures the classifier. Labels are the author's.
"""
import argparse
import json
import time
from collections import Counter

from . import config
from .engine import router

TYPES = router.TYPES


SETS = {  # file -> what its score means
    "taxonomy_cases.json": "development set (the fallback rules were written against it)",
    "taxonomy_holdout.json": "held-out set 1 (written before the rules were revised; its misses were then seen)",
    "taxonomy_holdout2.json": "held-out set 2 (written after the rules were frozen; the honest estimate for new questions)",
}
HEADLINE = "taxonomy_holdout2.json"


def load_cases(name: str = "taxonomy_cases.json") -> list[dict]:
    return json.loads((config.BENCH_DATA_DIR / name).read_text(encoding="utf-8"))["cases"]


def _history_text(case: dict) -> str:
    return "\n".join(f"Q{i}: {t['question']}\nA{i}: {t['answer']}" for i, t in enumerate(case.get("history") or [], 1))


def evaluate(cases: list[dict], fallback_only: bool = False) -> dict:
    rows = []
    for c in cases:
        hist = _history_text(c)
        p = router.fallback_profile(c["question"], bool(hist)) if fallback_only else router.classify(c["question"], hist)
        rows.append({"id": c["id"], "category": c["category"], "label": c["type"], "pred": p["type"], "source": p["source"],
                     "needs_ok": {k: p["needs"].get(k) == v for k, v in c["needs"].items()},
                     "false_premise": (bool(c.get("false_premise")), p["false_premise_suspected"]),
                     "time_sensitive": (bool(c.get("time_sensitive")), p["time_sensitive"])})
    confusion = {t: Counter() for t in TYPES}
    for r in rows:
        confusion[r["label"]][r["pred"]] += 1
    per_class = {}
    for t in TYPES:
        tp = confusion[t][t]
        pred_t = sum(confusion[x][t] for x in TYPES)
        label_t = sum(confusion[t].values())
        per_class[t] = {"precision": round(tp / pred_t, 3) if pred_t else None,
                        "recall": round(tp / label_t, 3) if label_t else None, "n": label_t}
    by_source = {}
    for src in ("llm", "fallback"):
        rs = [r for r in rows if r["source"] == src]
        by_source[src] = {"n": len(rs), "accuracy": round(sum(r["label"] == r["pred"] for r in rs) / len(rs), 3) if rs else None}
    by_category = {}
    for cat in sorted({r["category"] for r in rows}):
        rs = [r for r in rows if r["category"] == cat]
        by_category[cat] = {"n": len(rs), "accuracy": round(sum(r["label"] == r["pred"] for r in rs) / len(rs), 3)}

    def detect(key):
        pos = [r for r in rows if r[key][0]]
        neg = [r for r in rows if not r[key][0]]
        return {"positives": len(pos), "recall": round(sum(r[key][1] for r in pos) / len(pos), 3) if pos else None,
                "false_positive_rate": round(sum(r[key][1] for r in neg) / len(neg), 3) if neg else None}

    return {
        "n": len(rows), "accuracy": round(sum(r["label"] == r["pred"] for r in rows) / len(rows), 3),
        "confusion": {t: dict(confusion[t]) for t in TYPES}, "per_class": per_class,
        "needs_accuracy": {k: round(sum(r["needs_ok"].get(k, False) for r in rows) / len(rows), 3)
                           for k in ("facts", "calculation", "history")},
        "false_premise": detect("false_premise"), "time_sensitive": detect("time_sensitive"),
        "by_source": by_source, "by_category": by_category,
        "errors": [r["id"] + f": {r['label']} -> {r['pred']}" for r in rows if r["label"] != r["pred"]],
        "labels_note": "labels written by the author; review them before quoting these numbers",
        "created_at": time.time(),
    }


def main(argv=None) -> dict:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fallback-only", action="store_true", help="skip the model; measure the keyword fallback")
    ap.add_argument("--json")
    args = ap.parse_args(argv)
    sets = {name: evaluate(load_cases(name), args.fallback_only) for name in SETS
            if (config.BENCH_DATA_DIR / name).exists()}
    for name, r in sets.items():
        print(f"{name:26} n={r['n']:3}  accuracy {r['accuracy']:.0%}  - {SETS[name]}")
    res = dict(sets.get(HEADLINE) or next(iter(sets.values())))
    res["headline_set"] = HEADLINE if HEADLINE in sets else next(iter(sets))
    res["sets"] = {name: {"n": r["n"], "accuracy": r["accuracy"], "meaning": SETS[name]} for name, r in sets.items()}
    print(f"\nheadline ({res['headline_set']}): {res['n']} cases, accuracy {res['accuracy']:.0%}  "
          f"(llm {res['by_source']['llm']}, fallback {res['by_source']['fallback']})\n")
    print("confusion (rows = label, columns = predicted)")
    print(f"{'':14}" + "".join(f"{t:>13}" for t in TYPES))
    for t in TYPES:
        print(f"{t:14}" + "".join(f"{res['confusion'][t].get(p, 0):>13}" for p in TYPES))
    print("\nper class: " + ", ".join(f"{t} P={v['precision']} R={v['recall']} (n={v['n']})" for t, v in res["per_class"].items()))
    print("needs accuracy:", res["needs_accuracy"])
    print("false premise:", res["false_premise"], " time-sensitive:", res["time_sensitive"])
    print("by category:", {k: v["accuracy"] for k, v in res["by_category"].items()})
    if res["errors"]:
        print("misrouted:", "; ".join(res["errors"][:30]))
    res["mode"] = "fallback" if args.fallback_only else ("llm" if res["by_source"]["llm"]["n"] else "fallback")
    config.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = config.RESULTS_DIR / f"taxonomy-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(res, indent=1), encoding="utf-8")
    print(f"\nwrote {out}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(res, fh, indent=2)
    return res


if __name__ == "__main__":
    main()

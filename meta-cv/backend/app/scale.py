"""AuraDB scale test: load synthetic graphs of growing size and measure the app's own queries.

    python -m app.scale --backend neo4j --sizes 1000,10000,50000,100000 --seed 7 [--dry-run]
                        [--node-limit 200000 --rel-limit 400000] [--allow-dirty]

The generator is deterministic (--seed): power-law entity degrees, functional / transitive (acyclic)
/ plain relations, transitive chains of --depth, and a known share (--error-rate) of injected errors
(functional violations, incompatible pairs, cycles, near-duplicate names) so the audit's recall and
false positives are measured against known truth.

Per size it records load throughput (UNWIND batches of 1000), node/relationship counts, validate
latency p50/p95/p99 for supported / contradicted / unknown / transitive, facts_about (exact and
prefix fallback), relation_stats, the full audit runtime, and end-to-end factual-tier latency with a
scripted LLM at --llm-latency seconds per call. Everything is timed from this process, so the network
round trip to Aura is included. Results: results/scale-<timestamp>.json and .csv.

Safety on Aura Free: reads current counts first; refuses if the database holds non-scale-test data
(unless --allow-dirty) or if a size would pass 80% of the remaining plan limits (read the limits from
your Aura console and pass them); everything generated is labelled ScaleEntity / ScaleTest with this
run's run_id and deleted in a finally block; a limit or auth error stops the run and reports how far it got.
"""
import argparse
import csv
import json
import random
import tempfile
import time
from pathlib import Path

from . import config, llm
from .graph_common import GraphLimitError, GraphUnavailable

FUNCTIONAL = ("born_in", "capital_of")
TRANSITIVE = ("part_of",)
PLAIN = ("knows", "works_with", "cites", "mentions")
WORDS = ("amber aster birch cedar delta ember fable garnet harbor indigo juniper kestrel lumen maple nimbus onyx "
         "pebble quartz raven sable thistle umber vesper willow xenon yarrow zephyr alder bramble cinder dune "
         "fennel glade heron iris jasper koi larch moss nettle oriel plume quill rook sorrel tansy").split()


def _name(rng: random.Random, i: int) -> str:
    return f"{rng.choice(WORDS)} {rng.choice(WORDS)} {i}"


def generate(size: int, seed: int, depth: int, error_rate: float) -> dict:
    """Facts (dicts for add_facts), relation rules, sample queries and the injected-error truth."""
    rng = random.Random(seed)
    n_ent = max(50, size // 2)
    names = [_name(rng, i) for i in range(n_ent)]

    def pick():  # power-law: low indices are hubs
        return names[min(n_ent - 1, int(rng.paretovariate(1.2)) - 1 + int(n_ent * rng.random() ** 4))]

    facts, functional_subjects, chains = [], {}, []
    budget_errors = int(size * error_rate)
    n_func, n_chain = int(size * 0.15), int(size * 0.25)
    for i in range(n_func):
        rel = FUNCTIONAL[i % len(FUNCTIONAL)]
        s = names[i % n_ent]
        if (s, rel) in functional_subjects:
            continue
        o = names[rng.randrange(n_ent)]
        functional_subjects[(s, rel)] = o
        facts.append({"subject": s, "relation": rel, "object": o})
    made = 0
    while made < n_chain:
        chain = [f"part {len(chains)}-{k}" for k in range(depth + 1)]
        chains.append(chain)
        for a, b in zip(chain, chain[1:]):
            facts.append({"subject": a, "relation": "part_of", "object": b})
            made += 1
    while len(facts) < size - budget_errors:
        facts.append({"subject": pick(), "relation": rng.choice(PLAIN), "object": pick()})

    truth = {"functional_violation": set(), "incompatible_pair": set(), "cycle": set(), "near_duplicate": set()}
    kinds = list(truth)
    fs = list(functional_subjects.items())
    for e in range(budget_errors):
        kind = kinds[e % 4]
        if kind == "functional_violation" and fs:
            (s, rel), o = fs[e % len(fs)]
            other = names[(names.index(o) + 1) % n_ent]
            facts.append({"subject": s, "relation": rel, "object": other})
            truth[kind].add(s)
        elif kind == "incompatible_pair":
            x, y, s = f"style {e}a", f"style {e}b", pick()
            facts += [{"subject": x, "relation": "incompatible_with", "object": y},
                      {"subject": s, "relation": "mentions", "object": x},
                      {"subject": s, "relation": "mentions", "object": y}]
            truth[kind].add(s)
        elif kind == "cycle" and chains:
            chain = chains[e % len(chains)]
            facts.append({"subject": chain[-1], "relation": "part_of", "object": chain[-3]})
            truth[kind].add(chain[-1])
        elif kind == "near_duplicate":
            base = names[rng.randrange(n_ent)]
            dup = base[:-1] + ("x" if base[-1] != "x" else "y") + base[-1]
            facts.append({"subject": dup, "relation": "knows", "object": pick()})
            truth[kind].add(tuple(sorted((base, dup))))
    for f in facts:
        f.update(source="scale", added_by="scale")
    samples = {
        "supported": [(f["subject"], f["relation"], f["object"]) for f in rng.sample(facts, min(50, len(facts)))],
        "contradicted": [(s, rel, names[(names.index(o) + 7) % n_ent]) for (s, rel), o in fs[:50]],
        "unknown": [(pick(), rng.choice(PLAIN), pick()) for _ in range(50)],
        "transitive": [(c[0], "part_of", c[min(len(c) - 1, 3)]) for c in chains[:50]],
        "names": [pick() for _ in range(30)],
    }
    rules = [(r, True, False, False) for r in FUNCTIONAL] + [("part_of", False, True, True)]
    return {"facts": facts, "rules": rules, "samples": samples, "truth": truth, "entities": n_ent}


def _pct(xs: list[float]) -> dict:
    if not xs:
        return {}
    xs = sorted(xs)
    q = lambda p: xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))]  # noqa: E731
    return {"p50_ms": round(q(0.5) * 1000, 2), "p95_ms": round(q(0.95) * 1000, 2), "p99_ms": round(q(0.99) * 1000, 2),
            "n": len(xs)}


def _time(fn) -> float:
    t = time.perf_counter()
    fn()
    return time.perf_counter() - t


def _recall(audit_result: dict, truth: dict) -> dict:
    found = {k: set() for k in truth}
    for f in audit_result["findings"]:
        if f["check"] == "near_duplicate":
            found["near_duplicate"].add(tuple(sorted(f["entities"])[:2]))
        elif f["check"] in found:
            found[f["check"]].update(f["entities"])
    out = {}
    for k, t in truth.items():
        hit = len(t & found[k]) if k == "near_duplicate" else sum(1 for x in t if x in found[k])
        out[k] = {"injected": len(t), "detected": hit, "recall": round(hit / len(t), 3) if t else None,
                  "reported": sum(f["check"] == k for f in audit_result["findings"])}
    return out


def _factual_latency(store, samples: dict, llm_latency: float, n: int = 5) -> dict:
    """End-to-end factual tier with a scripted model: graph work is real, each model call sleeps llm_latency."""
    from .engine import pipeline

    def fake(system, user, temperature=0):
        time.sleep(llm_latency)
        if system.startswith("Profile"):
            return {"risks": [{"label": "factual.entity_error", "weight": 0.9}], "entities": [user.split(": ")[-1][:40]]}
        if system.startswith("Answer the question as a chain"):
            s, r, o = samples["supported"][hash(user) % len(samples["supported"])]
            return {"claims": [{"subject": s, "relation": r, "object": o}]}
        if system.startswith("List the atomic"):
            return {"claims": []}
        return "verified."

    real = llm.chat_json, llm.chat_text
    llm.chat_json = llm.chat_text = fake
    try:
        times = []
        for i in range(n):
            q = f"Tell me about {samples['names'][i % len(samples['names'])]}"
            times.append(_time(lambda: list(pipeline.run(store, q, typing_delay=0, reuse=False, source="scale",
                                                         limits={"calls": 0, "tokens": 0, "graph": 0}))))
    finally:
        llm.chat_json, llm.chat_text = real
    return _pct(times)


def measure(store, size: int, args) -> dict:
    from .audit import run_audit

    g = generate(size, args.seed, args.depth, args.error_rate)
    row = {"size": size, "facts_generated": len(g["facts"]), "entities_generated": g["entities"]}
    for name, f, t, a in g["rules"]:
        store.define_relation(name, f, t, a)
    t0 = time.perf_counter()
    created = 0
    for i in range(0, len(g["facts"]), 1000):
        created += store.add_facts(g["facts"][i:i + 1000])
    load = time.perf_counter() - t0
    row.update(load_s=round(load, 2), facts_per_s=round(len(g["facts"]) / load, 1), facts_created=created,
               counts=store.counts())
    for kind in ("supported", "contradicted", "unknown", "transitive"):
        row[f"validate_{kind}"] = _pct([_time(lambda q=q: store.validate(*q)) for q in g["samples"][kind]])
    row["facts_about_exact"] = _pct([_time(lambda n=n: store.facts_about(n)) for n in g["samples"]["names"]])
    row["facts_about_prefix"] = _pct([_time(lambda n=n: store.facts_about(n.rsplit(" ", 1)[0][:-1]))
                                      for n in g["samples"]["names"]])
    row["relation_stats"] = _pct([_time(store.relation_stats) for _ in range(3)])
    t0 = time.perf_counter()
    audit = run_audit(store, include_cache=False, remember=False)
    row["audit_s"] = round(time.perf_counter() - t0, 2)
    row["audit_recall"] = _recall(audit, g["truth"])
    row["factual_tier"] = _factual_latency(store, g["samples"], args.llm_latency)
    return row


def _report(rows: list[dict], stopped: str | None) -> dict:
    over_200 = next((r["size"] for r in rows if r["validate_supported"].get("p95_ms", 0) > 200), None)
    over_5m = next((r["size"] for r in rows if r["audit_s"] > 300), None)
    largest = max((r["size"] for r in rows), default=None)
    lines = [f"Largest size loaded: {largest}." if largest else "No size completed."]
    lines.append(f"p95 validate exceeded 200 ms at {over_200} facts." if over_200 else "p95 validate stayed under 200 ms.")
    lines.append(f"Full audit exceeded 5 minutes at {over_5m} facts." if over_5m else "Full audit stayed under 5 minutes.")
    if stopped:
        lines.append(f"Stopped early: {stopped}.")
    return {"largest_size": largest, "validate_p95_over_200ms_at": over_200, "audit_over_5min_at": over_5m,
            "stopped": stopped, "statement": " ".join(lines)}


def main(argv=None) -> dict:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend", choices=("neo4j", "sqlite"), default="neo4j")
    ap.add_argument("--sizes", default="1000,10000,50000,100000")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--depth", type=int, default=6)
    ap.add_argument("--error-rate", type=float, default=0.004)
    ap.add_argument("--llm-latency", type=float, default=0.3)
    ap.add_argument("--node-limit", type=int, default=200000, help="from your Aura console")
    ap.add_argument("--rel-limit", type=int, default=400000, help="from your Aura console")
    ap.add_argument("--allow-dirty", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    sizes = [int(s) for s in args.sizes.split(",")]
    run_id = f"scale-{int(time.time())}"

    if args.dry_run:
        for size in sizes:
            g = generate(size, args.seed, args.depth, args.error_rate)
            print(f"{size}: {len(g['facts'])} facts, ~{g['entities']} entities, injected "
                  + ", ".join(f"{k} {len(v)}" for k, v in g["truth"].items()))
        return {}

    from .db import init_db

    init_db()
    rows, stopped, store = [], None, None
    try:
        if args.backend == "neo4j":
            from .neo4j_store import Neo4jGraphStore

            store = Neo4jGraphStore(entity_label="ScaleEntity", relation_label="ScaleRelation", tag="ScaleTest", run_id=run_id)
            before = store.db_counts()
            other = store._run("MATCH (n) WHERE NOT n:ScaleTest RETURN count(n) AS n")[0]["n"]
            if other and not args.allow_dirty:
                raise SystemExit(f"refusing: the database holds {other} non-scale-test nodes (pass --allow-dirty)")
        else:
            store = __import__("app.graph_store", fromlist=["SqliteGraphStore"]).SqliteGraphStore(
                str(Path(tempfile.mkdtemp()) / "scale.db"))
            before = {"nodes": 0, "relationships": 0}
        for size in sizes:
            if args.backend == "neo4j":
                g_nodes, g_rels = size // 2 + size // 10, size
                now = store.db_counts()
                if now["nodes"] + g_nodes > 0.8 * args.node_limit or now["relationships"] + g_rels > 0.8 * args.rel_limit:
                    stopped = f"size {size} would pass 80% of the plan limits ({now} now)"
                    break
                store.delete_tagged()
            print(f"size {size} ...", flush=True)
            rows.append(measure(store, size, args))
            print(json.dumps({k: v for k, v in rows[-1].items() if k in ("size", "load_s", "audit_s")}), flush=True)
    except (GraphLimitError, GraphUnavailable) as e:
        stopped = f"{type(e).__name__}: {e}"
    finally:
        if store is not None and args.backend == "neo4j":
            try:
                store.delete_tagged()
                if store._run("MATCH (n:ScaleEntity) RETURN count(n) AS n")[0]["n"] == 0:
                    store.drop_namespace()
            except Exception as e:  # noqa: BLE001 - report, never mask the run's own result
                print(f"cleanup failed, delete nodes labelled ScaleTest with run_id={run_id}: {e}")

    result = {"run_id": run_id, "backend": args.backend, "seed": args.seed, "depth": args.depth,
              "error_rate": args.error_rate, "llm_latency_s": args.llm_latency, "path_max_depth": config.PATH_MAX_DEPTH,
              "db_before": before, "rows": rows, "report": _report(rows, stopped), "created_at": time.time()}
    config.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    base = config.RESULTS_DIR / run_id
    base.with_suffix(".json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    with base.with_suffix(".csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["size", "load_s", "facts_per_s", "validate_supported_p95_ms", "validate_transitive_p95_ms",
                    "facts_about_exact_p95_ms", "relation_stats_p95_ms", "audit_s", "factual_tier_p95_ms"])
        for r in rows:
            w.writerow([r["size"], r["load_s"], r["facts_per_s"], r["validate_supported"].get("p95_ms"),
                        r["validate_transitive"].get("p95_ms"), r["facts_about_exact"].get("p95_ms"),
                        r["relation_stats"].get("p95_ms"), r["audit_s"], r["factual_tier"].get("p95_ms")])
    print(result["report"]["statement"])
    print(f"wrote {base}.json and .csv")
    return result


if __name__ == "__main__":
    main()

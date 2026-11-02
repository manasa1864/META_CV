"""Dashboard numbers. Live numbers are computed from the logged turns; benchmark numbers come only
from results/latest.json (written by python -m app.bench), so nothing on the dashboard is illustrative."""
import json
from collections import Counter

from . import audit, config
from .db import connect
from .engine import router
from .engine.health import health

TIERS = ("factual", "reasoning", "consistency")
WINDOW = 10
ACTIONS = ("corrected", "dropped", "flagged")


def _avg(xs):
    xs = list(xs)
    return round(sum(xs) / len(xs), 3) if xs else None


def _read(path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _newest(pattern: str) -> dict | None:
    files = sorted(config.RESULTS_DIR.glob(pattern)) if config.RESULTS_DIR.exists() else []
    return _read(files[-1]) if files else None


def latest_results() -> dict | None:
    """results/latest.json (benchmark) plus the newest scale-, taxonomy- and safety-*.json, without the raw rows."""
    bench = _read(config.RESULTS_DIR / "latest.json") if config.RESULTS_DIR.exists() else None
    scale, taxonomy, safety = _newest("scale-*.json"), _newest("taxonomy-*.json"), _newest("safety-*.json")
    if not (bench or scale or taxonomy or safety):
        return None
    out = {k: v for k, v in (bench or {}).items() if k != "rows"}
    out["scale"] = scale
    out["taxonomy"] = taxonomy
    out["safety"] = safety
    out["audit"] = (lambda a: {k: a[k] for k in ("ran_at", "counts", "by_check", "backend")} if a else None)(audit.last_result())
    return out


def _safety(rows: list[dict]) -> dict:
    """Live safety numbers: what the checks saw in answered questions, facts the write gate held back for
    instruction-like text, and refusals that never became a turn (rate limit, other-site posts, ...)."""
    seen = [json.loads(r["safety"]) for r in rows if r.get("safety")]
    patterns: Counter = Counter(p for s in seen for p in s.get("injection", []))
    kinds: Counter = Counter(k for s in seen for k in s.get("private_data", []))
    with connect() as conn:
        quarantined = conn.execute("SELECT COUNT(*) FROM quarantine WHERE reason LIKE '%instruction-like%'").fetchone()[0]
        events = {r["kind"]: r["n"] for r in conn.execute("SELECT kind, SUM(count) AS n FROM safety_events GROUP BY kind")}
    from . import safety as safety_mod, source_trust  # noqa: PLC0415

    with connect() as conn:
        held = conn.execute("SELECT COUNT(*) FROM review_queue WHERE reason LIKE 'source ''%'' is often contradicted%'"
                            ).fetchone()[0]
    return {
        "answers_steered": sum(bool(s.get("steered")) for s in seen),
        "tainted_claims": sum(s.get("tainted_claims", 0) for s in seen),
        "facts_held_low_trust": held,
        "adversarial_policy": router.adversarial_policy(),
        "sources": source_trust.table(10),
        "questions_checked": len(seen),
        "injection_questions": sum(bool(s.get("injection")) for s in seen),
        "injection_patterns": dict(patterns.most_common()),
        "private_data_questions": sum(bool(s.get("private_data")) for s in seen),
        "private_data_kinds": dict(kinds.most_common()),
        "facts_ignored": sum(s.get("ignored_facts", 0) for s in seen),
        "facts_quarantined": quarantined,
        "answers_withheld": sum(bool(s.get("withheld")) for s in seen),
        "refused": {k: events.get(k, 0) for k in safety_mod.EVENTS},
    }


def summary() -> dict:
    with connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, hallucination_type, tier, from_cache, reused_state, caught, prompt_tokens, completion_tokens, "
            "saved_tokens, source, outcome, signal_quality, correct, feedback, confidence, stopped, reuse_kind, "
            "compression_context, safety FROM turns "
            "WHERE source NOT IN ('scale', 'loadtest', 'replay') ORDER BY id"
        )]
    for r in rows:
        r["caught"] = json.loads(r["caught"])
        r["tokens"] = r["prompt_tokens"] + r["completion_tokens"]
        r["result"], r["signal"] = router._row_outcome(r)
        r["success"] = 1 if r["result"] == "correct" else 0
    fresh = [r for r in rows if not r["from_cache"]]

    by_family = {t: {"type": t, **{a: 0 for a in ACTIONS}} for t in router.TYPES}
    by_risk: Counter = Counter()
    for r in rows:
        for c in r["caught"]:
            fam = c.get("type") or router.family(c.get("risk", ""))
            if fam in by_family and c["action"] in ACTIONS and not str(c.get("risk", "")).startswith("adversarial."):
                by_family[fam][c["action"]] += 1
            by_risk[c.get("risk") or fam] += 1

    by_tier = []
    for t in TIERS:
        rs = [r for r in fresh if r["tier"] == t]
        by_tier.append({
            "tier": t, "queries": len(rs),
            "success_rate": _avg(r["success"] for r in rs),
            "avg_tokens": _avg(r["tokens"] for r in rs),
            "avg_tokens_reused": _avg(r["tokens"] for r in rs if r["reused_state"]),
            "avg_tokens_fresh": _avg(r["tokens"] for r in rs if not r["reused_state"]),
        })

    timeline = []
    for i, r in enumerate(rows):
        win = rows[max(0, i - WINDOW + 1): i + 1]
        timeline.append({"n": r["id"], "tokens": r["tokens"], "success": r["success"], "tier": r["tier"],
                         "type": r["hallucination_type"], "cached": bool(r["from_cache"]),
                         "rolling_success": _avg(w["success"] for w in win),
                         "rolling_tokens": _avg(w["tokens"] for w in win)})

    by_reuse = []
    for kind in ("none", "exact_repeat", "state_answer", "template", "similar_state"):
        rs = [r for r in rows if (r["reuse_kind"] or "none") == kind]
        by_reuse.append({"kind": kind, "turns": len(rs), "avg_tokens": _avg(r["tokens"] for r in rs),
                         "verified_rate": _avg(r["success"] for r in rs)})

    contexts = [json.loads(r["compression_context"]) for r in rows if r["compression_context"]]
    trimmed = [c for c in contexts if c.get("facts_cut") or c.get("relations_cut") or c.get("history_trimmed")
               or c.get("history_expanded")]
    context_check = {
        "trimmed_answers": len(trimmed),
        "redrafted": sum(bool(c.get("redrafted")) for c in trimmed),
        "history_expanded": sum(bool(c.get("history_expanded")) for c in trimmed),
        "facts_cut": sum(c.get("facts_cut", 0) for c in trimmed),
        "facts_kept": sum(c.get("facts_kept", 0) for c in trimmed),
    }

    outcomes = Counter(r["result"] or "unknown" for r in rows)
    signals = Counter(r["signal"] or "none" for r in rows)
    stops = Counter(json.loads(r["stopped"])["reason"] for r in rows if r["stopped"])
    graph = audit.last_result()
    return {
        "safety": _safety(rows),
        "totals": {
            "queries": len(rows),
            "cache_hits": sum(r["from_cache"] for r in rows),
            "reused_states": sum(r["reused_state"] for r in rows),
            "success_rate": _avg(r["success"] for r in rows),
            "avg_tokens": _avg(r["tokens"] for r in fresh),
            "tokens_spent": sum(r["tokens"] for r in rows),
            "tokens_saved": sum(r["saved_tokens"] for r in rows),
            "caught": sum(len(r["caught"]) for r in rows),
            "stopped": sum(stops.values()),
        },
        "outcomes": {k: outcomes.get(k, 0) for k in ("correct", "abstained", "hallucinated", "unknown")},
        "signals": dict(signals),
        "stops": dict(stops),
        "caught_by_type": list(by_family.values()),
        "caught_by_risk": [{"risk": k, "label": router.RISKS.get(k, k), "count": v} for k, v in by_risk.most_common()],
        "by_tier": by_tier,
        "by_reuse": by_reuse,
        "context_check": context_check,
        "router": router.table(),
        "router_frozen": config.ROUTER_FROZEN,
        "timeline": timeline[-500:],
        "health": health.state(),
        "graph_health": {k: graph[k] for k in ("ran_at", "counts", "by_check", "backend")} if graph else None,
        "bench": latest_results(),
    }

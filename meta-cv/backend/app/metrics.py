"""Dashboard numbers, computed from the logged turns and the latest benchmark run."""
import json

from .db import connect
from .engine import router

TIERS = ("factual", "reasoning", "consistency")
WINDOW = 10


def _avg(xs):
    xs = list(xs)
    return round(sum(xs) / len(xs), 1) if xs else None


def summary() -> dict:
    with connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, hallucination_type, tier, from_cache, reused_state, caught, prompt_tokens, completion_tokens, "
            f"saved_tokens, source, {router.SUCCESS_SQL} AS success FROM turns ORDER BY id"
        )]
        bench = conn.execute("SELECT summary, created_at FROM bench_runs ORDER BY id DESC LIMIT 1").fetchone()
    for r in rows:
        r["caught"] = json.loads(r["caught"])
        r["tokens"] = r["prompt_tokens"] + r["completion_tokens"]
    fresh = [r for r in rows if not r["from_cache"]]

    caught = {t: {"type": t, "corrected": 0, "dropped": 0, "flagged": 0} for t in router.TYPES}
    for r in rows:
        for c in r["caught"]:
            if c["type"] in caught and c["action"] in caught[c["type"]]:
                caught[c["type"]][c["action"]] += 1

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

    return {
        "totals": {
            "queries": len(rows),
            "cache_hits": sum(r["from_cache"] for r in rows),
            "reused_states": sum(r["reused_state"] for r in rows),
            "success_rate": _avg(r["success"] for r in rows),
            "avg_tokens": _avg(r["tokens"] for r in fresh),
            "tokens_spent": sum(r["tokens"] for r in rows),
            "tokens_saved": sum(r["saved_tokens"] for r in rows),
            "caught": sum(len(r["caught"]) for r in rows),
        },
        "caught_by_type": list(caught.values()),
        "by_tier": by_tier,
        "router": router.table(),
        "timeline": timeline[-500:],
        "bench": {**json.loads(bench["summary"]), "created_at": bench["created_at"]} if bench else None,
    }

"""Re-check loop: re-validate everything cached against the current graph and delete what no longer holds.

    python -m app.recheck [--dry-run]

- cached factual paths: every graph-backed node must still be supported, every model-verified node
  must still be inside its TTL;
- cached reasoning paths: arithmetic re-run;
- reasoning_states: their graph-backed triples / premises must still be supported;
- model-verified claims past CLAIM_TTL_HOURS are dropped (state_cache expiry);
- then prune: reasoning_states capped at STATE_MAX_ROWS (least-hit first), state_cache rows older than
  STATE_CACHE_MAX_AGE_DAYS, turns older than TURNS_RETENTION_DAYS (if set).
Cheap to run on a schedule or before a demo; graph writes already invalidate dependent entries.
"""
import argparse
import json
import time
import zlib

from . import config, state_cache
from .db import connect, init_db
from .engine import reasoning, states
from .engine.factual import still_holds


def _graph_claims_hold(store, triples: list[dict]) -> str | None:
    for c in triples:
        if c.get("source") in ("graph", "graph-corrected") and \
                store.validate(c["subject"], c["relation"], c["object"])["verdict"] != "supported":
            return f"graph no longer supports {c['subject']} {c['relation']} {c['object']}"
    return None


def run(store, dry_run: bool = False) -> dict:
    report = {"paths_checked": 0, "paths_removed": 0, "states_checked": 0, "states_removed": 0,
              "expired_removed": 0, "states_pruned": 0, "turns_pruned": 0, "reasons": []}
    now = time.time()
    with connect() as conn:
        rows = conn.execute("SELECT key, value, expires_at FROM state_cache").fetchall()
    for r in rows:
        if r["expires_at"] is not None and r["expires_at"] < now:
            report["expired_removed"] += 1
            if not dry_run:
                state_cache.delete(r["key"])
            continue
        try:
            v = json.loads(zlib.decompress(r["value"]))
        except (zlib.error, ValueError):
            continue
        if not isinstance(v, dict) or "path" not in v or "tier" not in v:
            continue
        report["paths_checked"] += 1
        if v["tier"] == "factual":
            problem = still_holds(store, v["path"])
        else:
            bad = reasoning.first_arithmetic_error(v["path"])
            problem = f"step {bad[0] + 1}: {bad[1]}" if bad else None
        if problem:
            report["paths_removed"] += 1
            report["reasons"].append(problem)
            if not dry_run:
                state_cache.delete(r["key"])

    with connect() as conn:
        st = conn.execute("SELECT id, state FROM reasoning_states").fetchall()
    for r in st:
        report["states_checked"] += 1
        v = json.loads(zlib.decompress(r["state"]))
        problem = _graph_claims_hold(store, v.get("triples", []) + v.get("premises", []))
        if problem:
            report["states_removed"] += 1
            report["reasons"].append(problem)
            if not dry_run:
                with connect() as conn:
                    conn.execute("DELETE FROM reasoning_states WHERE id=?", (r["id"],))

    if not dry_run:
        report["states_pruned"] = states.prune(config.STATE_MAX_ROWS)
        report["expired_removed"] += state_cache.prune(config.STATE_CACHE_MAX_AGE_DAYS)
        if config.TURNS_RETENTION_DAYS > 0:
            with connect() as conn:
                report["turns_pruned"] = conn.execute("DELETE FROM turns WHERE created_at < ?",
                                                      (now - config.TURNS_RETENTION_DAYS * 86400,)).rowcount
    return report


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Re-validate cached paths and states against the current graph.")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    init_db()
    from .graph_store import get_store

    report = run(get_store(), args.dry_run)
    for k, v in report.items():
        if k != "reasons":
            print(f"{k:16} {v}")
    for reason in report["reasons"][:20]:
        print(" -", reason)


if __name__ == "__main__":
    main()

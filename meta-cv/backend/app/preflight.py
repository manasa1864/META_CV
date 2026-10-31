"""Pre-demo checks. Prints PASS/FAIL per check and exits non-zero on any FAIL.

    python -m app.preflight                     # local: graph reachable, seed present, audit clean
    python -m app.preflight --seed              # also (re)load the seed graph (idempotent)
    python -m app.preflight --url https://...   # a deployment: health, readiness, examples, results, judge mode

Aura Free pauses after a few days without traffic (and may be deleted after a long pause). This script
cannot resume it: if the graph is unreachable, resume the instance in the Aura console and run again.
Keep it awake with a scheduled ping to /api/readyz (e.g. every 12 hours) from an external scheduler.
"""
import argparse
import json
import sys

import httpx


class Checks:
    def __init__(self):
        self.failed = 0

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  - {detail}" if detail else ""))
        self.failed += not ok
        return ok


def local(args, c: Checks) -> None:
    from . import audit, seed
    from .db import init_db
    from .graph_common import GraphUnavailable
    from .graph_store import get_store

    init_db()
    try:
        store = get_store()
        degraded = getattr(store, "degraded", None)
        if not c.check("graph reachable", not degraded, f"degraded={degraded}; resume the Aura instance in the console"
                       if degraded else store.backend):
            return
        store.ping()
    except (GraphUnavailable, RuntimeError) as e:
        c.check("graph reachable", False, f"{e} - if Aura is paused, resume it in the console")
        return
    if args.seed:
        counts = seed.main(store)
        c.check("seed loaded", counts["quarantined"] == 0, json.dumps(counts))
    facts = store.counts()["facts"]
    c.check("graph has facts", facts > 0, f"{facts} facts")
    result = audit.run_audit(store)
    c.check("audit has no errors", result["counts"]["error"] == 0, json.dumps(result["counts"]))


def remote(url: str, c: Checks) -> None:
    from .examples import EXAMPLES

    api = url.rstrip("/") + "/api"
    with httpx.Client(timeout=120) as h:
        r = h.get(f"{api}/healthz")
        c.check("/healthz", r.status_code == 200)
        r = h.get(f"{api}/readyz")
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        c.check("/readyz ready", r.status_code == 200, json.dumps(body.get("checks")))
        c.check("graph not degraded", not body.get("degraded"), str(body.get("degraded")))
        judge = bool(body.get("judge_mode"))
        for ex in EXAMPLES:
            conv = f"preflight-{ex['id']}"
            ok, last = True, ""
            for k, q in enumerate(ex["turns"]):
                if ex.get("separate"):
                    conv = f"preflight-{ex['id']}-{k}"
                r = h.post(f"{api}/chat", json={"question": q, "conversation_id": conv})
                ok = ok and r.status_code == 200 and "tier" in r.json()
                last = f"HTTP {r.status_code}"
            c.check(f"example '{ex['id']}' returns a final answer", ok, last)
        r = h.get(url.rstrip("/") + "/results")
        c.check("/results page loads", r.status_code == 200 and "<html" in r.text.lower())
        r = h.get(f"{api}/results/latest")
        try:
            parsed = r.status_code == 200 and isinstance(r.json(), dict)
        except ValueError:
            parsed = False
        c.check("results/latest.json parses", parsed, f"HTTP {r.status_code}")
        if judge:
            r = h.post(f"{api}/facts", json={"subject": "preflight", "relation": "likes", "object": "x"})
            c.check("writes blocked in judge mode", r.status_code == 403, f"HTTP {r.status_code}")
        r = h.get(f"{api}/audit/summary")
        errors = r.json().get("counts", {}).get("error") if r.status_code == 200 else None
        c.check("audit clean", errors == 0, f"errors={errors}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url")
    ap.add_argument("--seed", action="store_true")
    args = ap.parse_args(argv)
    c = Checks()
    remote(args.url, c) if args.url else local(args, c)
    print(f"\n{'ALL PASS' if not c.failed else f'{c.failed} FAILED'}")
    return 1 if c.failed else 0


if __name__ == "__main__":
    sys.exit(main())

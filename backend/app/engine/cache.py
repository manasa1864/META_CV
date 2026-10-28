"""Cache tier: reuse answer paths that were fully verified before.

A cached path is not trusted blindly either: graph-backed nodes are re-validated
against the current graph and arithmetic steps are re-run before it is reused,
and a reasoning path is reused only if it was verified in a mode at least as
strict as the one requested.
"""
import time

from .. import state_cache
from . import factual, reasoning
from .trace import Trace


def path_key(question: str) -> str:
    return state_cache.make_key(f"path:{question}")


def save(question: str, result: dict) -> None:
    state_cache.put(path_key(question), {**result, "cached_at": time.time()})


def _still_valid(store, hit: dict) -> str | None:
    """None if the path still holds; otherwise why not."""
    if hit["tier"] == "factual":
        for c in hit["path"]:
            if c["status"] != "accepted":
                continue
            if c["source"] in ("graph", "graph-corrected"):
                if store.validate(c["subject"], c["relation"], c["object"])["verdict"] != "supported":
                    return f"graph no longer supports {c['subject']} {c['relation']} {c['object']}"
            elif state_cache.get(factual.claim_key(c)) is None:
                return f"verification of {c['subject']} {c['relation']} {c['object']} expired"
        return None
    bad = reasoning.first_arithmetic_error(hit["path"])
    return f"step {bad[0] + 1}: {bad[1]}" if bad else None


def lookup(store, question: str, mode: str | None, trace: Trace):
    """Generator: yields trace events, returns a reusable result or None."""
    ev = trace.start("cache", "Looking for a verified path for this question")
    yield ev
    key = path_key(question)
    hit = state_cache.get(key)
    if hit is None:
        yield trace.settle(ev, "info", "No verified path yet - running the full pipeline.")
        return None
    if hit["tier"] == "reasoning" and mode and reasoning.STRICTNESS[hit["mode"]] < reasoning.STRICTNESS[mode]:
        yield trace.settle(ev, "info", f"Found a path verified in {hit['mode']} mode, but {mode} was asked for - "
                                       "re-verifying.")
        return None
    yield trace.settle(ev, "running", f"Found a {hit['tier']} path ({len(hit['path'])} nodes). Re-checking it...")
    problem = _still_valid(store, hit)
    if problem:
        state_cache.delete(key)
        yield trace.settle(ev, "warn", f"Cached path no longer holds ({problem}). Discarded - recomputing.")
        return None
    yield trace.settle(ev, "ok", "Every node still checks out. Reusing the verified path.")
    return {**hit, "from_cache": True}

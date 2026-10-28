"""Runs a question through the tiers and yields the event stream the chat UI renders.

  1. exact-repeat cache (re-validated)          4. tier: factual (graph) / reasoning / consistency,
  2. classify the hallucination risk               with a similar question's verified state injected
  3. learned router picks the tier              5. log the turn -> the router learns from it

Event types, in order:
  step           thinking-trace step (sent again with the same id when it settles)
  thinking_done  all checks finished ("Processing complete")
  answer         a chunk of answer text
  final          summary: type, tier, confidence, hallucinations caught, tokens, router scores, path
  error          the run failed
"""
import json
import re
import time

from .. import llm
from ..db import connect
from . import cache, factual, memory, reasoning, router, states
from .trace import Trace

MODES = ("auto", *reasoning.MODES)
TIER_DESC = {
    "factual": "factual tier - verify every claim against the knowledge graph",
    "reasoning": "reasoning tier - check steps, backtrack on contradictions, cross-check solutions",
    "consistency": "consistency tier - answer from what this conversation already verified",
}


def _chunks(text: str):
    for part in re.findall(r"\S+\s*", text):
        yield part


def _route_detail(r: dict) -> str:
    risk = f"Risk: {r['type']} hallucination" + (f" ({r['why']})" if r["why"] else "")
    if r["forced"]:
        return f"{risk}. Forced: {TIER_DESC[r['tier']]}."
    scores = ", ".join(f"{t} {s:.2f}" for t, s in sorted(r["scores"].items(), key=lambda x: -x[1]))
    why = (f"learned from {r['evidence']} past {r['type']} turn(s), this tier beats the usual specialist"
           if r["learned"] else f"{r['evidence']} past {r['type']} turn(s) on this tier")
    return f"{risk}. Using the {TIER_DESC[r['tier']]} ({why}; scores: {scores})."


def _solve(store, question: str, mode: str, conversation_id: str | None, force_tier: str | None, reuse: bool,
           meter: dict, trace: Trace):
    reasoning_mode = None if mode == "auto" else mode
    hist = memory.history(conversation_id)

    # Without history a repeat can be served before classifying (zero tokens). With history the
    # question may refer back ("where was he born?"), so classify first and skip the cache for follow-ups.
    if reuse and not hist:
        hit = yield from cache.lookup(store, question, reasoning_mode, trace)
        if hit:
            return {**hit, "route": None}

    ev = trace.start("router", "Classifying the hallucination risk and choosing a tier")
    yield ev
    r = router.route(question, memory.history_text(hist), force_tier)
    yield trace.settle(ev, "ok", _route_detail(r), tier=r["tier"])

    if reuse and hist and r["type"] != "consistency":
        hit = yield from cache.lookup(store, question, reasoning_mode, trace)
        if hit:
            return {**hit, "route": r}

    tier, q, result = r["tier"], question, None
    if tier == "consistency":
        res = yield from memory.answer(store, question, hist, trace)
        if "delegate" in res:
            q, tier = res["delegate"]["question"], res["delegate"]["type"]
        else:
            result = res

    prior = None
    if result is None:
        prior = states.find(tier, q) if reuse else None
        if tier == "factual":
            result = yield from factual.answer(store, q, r["entities"], trace, prior, memory.claims(hist))
        else:
            result = yield from reasoning.answer(q, reasoning_mode or "balanced", trace, prior, memory.answers(hist))

    cacheable = result.pop("cacheable") and reuse and r["type"] != "consistency" and q == question
    tokens = meter["prompt"] + meter["completion"]
    if reuse and (state := states.state_of(result)):
        states.save(result["tier"], q, state, tokens)
    if cacheable:
        cache.save(question, {**result, "hallucination_type": r["type"], "tokens": tokens})
        yield trace.note("cache", "Saved this verified path for reuse")
    return {**result, "hallucination_type": r["type"], "route": r, "standalone": q if q != question else None,
            "reused_state": {"question": prior["question"], "similarity": prior["similarity"]} if prior else None}


def _log(conversation_id, question, result, meter, source) -> int:
    saved = result.get("tokens", 0) if result.get("from_cache") else 0
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO turns(conversation_id, question, hallucination_type, tier, mode, confidence, from_cache, "
            "reused_state, caught, prompt_tokens, completion_tokens, saved_tokens, answer, commitments, source, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (conversation_id, question, result["hallucination_type"], result["tier"], result.get("mode"),
             result["confidence"], int(bool(result.get("from_cache"))), int(bool(result.get("reused_state"))),
             json.dumps(result.get("caught", [])), meter["prompt"], meter["completion"], saved, result["answer"],
             json.dumps(memory.commitments_of(result.get("standalone") or question, result)), source, time.time()),
        )
        return cur.lastrowid


def run(store, question: str, mode: str = "auto", typing_delay: float = 0.015, conversation_id: str | None = None,
        force_tier: str | None = None, reuse: bool = True, source: str = "chat"):
    """force_tier/reuse=False are for benchmarking a tier on its own, without caches."""
    question = question.strip()
    trace = Trace()
    meter = llm.new_meter()
    try:
        result = yield from llm.metered(
            _solve(store, question, mode, conversation_id, force_tier, reuse, meter, trace), meter)
        if result.get("from_cache"):
            result = {**result, "hallucination_type": result.get("hallucination_type", result["tier"]),
                      "caught": [], "withheld": 0}
        turn_id = _log(conversation_id, question, result, meter, source)
    except llm.LLMNotConfigured as e:
        yield {"type": "error", "message": str(e)}
        return
    except Exception as e:  # surface to the UI instead of a silently dropped stream
        yield {"type": "error", "message": f"{type(e).__name__}: {e}"}
        return

    yield {"type": "thinking_done", "message": "Processing complete"}
    for chunk in _chunks(result["answer"]):
        yield {"type": "answer", "delta": chunk}
        if typing_delay:
            time.sleep(typing_delay)
    r = result.get("route")
    yield {
        "type": "final",
        "turn_id": turn_id,
        "hallucination_type": result["hallucination_type"],
        "tier": result["tier"],
        "mode": result.get("mode"),
        "confidence": result["confidence"],
        "from_cache": bool(result.get("from_cache")),
        "reused_state": result.get("reused_state"),
        "standalone": result.get("standalone"),
        "caught": result.get("caught", []),
        "withheld": result.get("withheld", 0),
        "tokens": {"prompt": meter["prompt"], "completion": meter["completion"],
                   "total": meter["prompt"] + meter["completion"], "calls": meter["calls"],
                   "saved": result.get("tokens", 0) if result.get("from_cache") else 0},
        "router": {"scores": r["scores"], "learned": r["learned"], "evidence": r["evidence"]} if r else None,
        "path": result["path"],
    }

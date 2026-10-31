"""Runs a question through the tiers and yields the event stream the chat UI renders.

  1. exact-repeat cache (re-validated)          5. tier: factual (graph) / reasoning / consistency,
  2. risk profile (taxonomy v2)                    or factual-then-reasoning for mixed questions
  3. learned router picks the tier              6. stop conditions + faithfulness guard
  4. similar verified state injected            7. log the turn -> the router learns from its outcome

Event types, in order (fields are only ever added, never removed):
  step           thinking-trace step (sent again with the same id when it settles)
  thinking_done  all checks finished ("Processing complete")
  answer         a chunk of answer text
  final          summary: type, risks, tier, confidence, caught, stopped, degraded, tokens, router, path
  error          the run failed: {code, message} with a short, safe message
"""
import json
import logging
import re
import time

from .. import config, llm, meter
from ..db import connect
from ..graph_common import GraphLimitError, GraphUnavailable, ReadOnlyGraph
from . import cache, factual, guardrails, memory, planner, reasoning, router, states
from .health import health
from .router import caught
from .trace import Trace

log = logging.getLogger(__name__)
MODES = ("auto", *reasoning.MODES)
TIER_DESC = {
    "factual": "factual tier - verify every claim against the knowledge graph",
    "reasoning": "reasoning tier - check steps, backtrack on contradictions, cross-check solutions",
    "consistency": "consistency tier - answer from what this conversation already verified",
}
ERRORS = {
    "llm_not_configured": "The language model is not configured on this server yet.",
    "llm_unavailable": "The language model is not responding right now. Try again shortly, or use the recorded examples.",
    "graph_unavailable": "The knowledge graph cannot be reached right now.",
    "graph_limit": "The knowledge graph is full or refused the write.",
    "read_only": "This deployment is read-only.",
    "internal": "Something went wrong while answering. The details were logged on the server.",
}


def _chunks(text: str):
    for part in re.findall(r"\S+\s*", text):
        yield part


def _route_detail(r: dict) -> str:
    risks = ", ".join(f"{x['label']} {x['weight']:.1f}" for x in r["risks"][:3])
    risk = f"Risk: {r['type']} ({risks})" + (f" - {r['why']}" if r["why"] else "") + \
        (" [keyword fallback]" if r.get("source") == "fallback" else "")
    if r["forced"]:
        return f"{risk}. Forced: {TIER_DESC[r['tier']]}."
    scores = ", ".join(f"{t} {s:.2f}" for t, s in sorted(r["scores"].items(), key=lambda x: -x[1]))
    why = (f"learned from {r['evidence']} weighted outcome(s), this tier beats the usual specialist"
           if r["learned"] else f"{r['evidence']} weighted outcome(s) for this tier")
    return f"{risk}. Using the {TIER_DESC[r['tier']]} ({why}; scores: {scores})" + \
        (" Router frozen: this turn will not change it." if r.get("frozen") else ".")


def _stop(r: dict | None, reason: str, detail: str, text: str, risk: str | None = None) -> dict:
    tier = r["tier"] if r else "factual"
    return {"tier": tier, "hallucination_type": r["type"] if r else "factual", "answer": text, "confidence": "low",
            "path": [], "withheld": 0, "cacheable": False,
            "caught": [caught(risk, tier, "dropped", detail)] if risk else [],
            "stopped": {"reason": reason, "detail": detail}}


def _unverified(question: str, tier: str, mode: str, trace: Trace):
    """Ablation 'no_verification': the same tiers with every check switched off."""
    yield trace.note("ablation", "Verification disabled", "Answering without any checks (ablation run).", "warn")
    if tier == "reasoning":
        sol = reasoning._parse_solution(llm.chat_json(reasoning.SOLVE_SYSTEM, f"Problem: {question}"))
        return {"tier": "reasoning", "mode": mode, "answer": f"**Answer: {sol['final_answer'] or 'not determined'}**",
                "confidence": "medium", "path": sol["steps"], "final_answer": sol["final_answer"],
                "caught": [], "withheld": 0, "cacheable": False, "stopped": None}
    claims = [{**c, "status": "accepted", "source": "unchecked"}
              for c in factual._parse_claims(llm.chat_json(factual.DRAFT_SYSTEM, f"KNOWN FACTS:\n(none)\n\nQuestion: {question}"))]
    facts = "\n".join(f"- {c['subject']} {c['relation'].replace('_', ' ')} {c['object']}" for c in claims) or "(none)"
    text = llm.chat_text(factual.COMPOSE_SYSTEM, f"Question: {question}\n\nVerified facts:\n{facts}\n\nCould not verify:\n(none)")
    return {"tier": "factual", "answer": text.strip(), "confidence": "medium", "path": claims, "caught": [],
            "withheld": 0, "cacheable": False, "stopped": None}


def _false_premise(r: dict, result: dict) -> dict | None:
    """Stop condition 3: the question presupposes something the graph contradicts."""
    if not r.get("false_premise_suspected") or result["tier"] != "factual":
        return None
    hits = [c for c in result["path"] if c.get("source") == "graph-corrected"
            or (c.get("status") == "rejected" and c.get("source") == "graph")]
    if not hits:
        return None
    lines = []
    for c in hits:
        if c.get("source") == "graph-corrected":
            lines.append(f"{c['subject']} {c['relation'].replace('_', ' ')} {c['object']}, not {c['original_object']}")
        else:
            lines.append(f"not {c['subject']} {c['relation'].replace('_', ' ')} {c['object']} ({c['reason']})")
    detail = "; ".join(lines)
    accepted = [c for c in result["path"] if c.get("status") == "accepted"]
    text = "The question assumes something the knowledge graph contradicts: " + detail + "."
    if accepted:
        text += "\n\n" + guardrails.render_verified(accepted, [])
    return {**result, "answer": text, "cacheable": False, "stopped": {"reason": "false_premise", "detail": detail},
            "caught": result["caught"] + [caught("factual.overclaim", "factual", "corrected", f"false premise: {detail}")]}


def _solve(store, question: str, mode: str, conversation_id: str | None, force_tier: str | None, reuse: bool,
           flags: dict, meter_: dict, trace: Trace):
    reasoning_mode = None if mode == "auto" else mode
    hist = memory.history(conversation_id)
    breaker = health.state()
    verified_only = bool(flags.get("verified_only") or breaker["verified_only"])
    if breaker["verified_only"]:
        yield trace.note("health", "Verified-only mode", f"{breaker['reason']}: only graph-supported claims are used.", "warn")

    # Without history a repeat can be served before classifying (zero tokens). With history the
    # question may refer back ("where was he born?"), so classify first and skip the cache for follow-ups.
    if reuse and not hist:
        hit = yield from cache.lookup(store, question, reasoning_mode, trace)
        if hit:
            return {**hit, "route": None}

    ev = trace.start("router", "Classifying the hallucination risk and choosing a tier")
    yield ev
    r = router.route(question, memory.history_text(hist, question), force_tier)
    if flags.get("static_router") and not force_tier:
        r = {**r, "tier": router.SPECIALIST[r["type"]], "learned": False}
    yield trace.settle(ev, "ok", _route_detail(r), tier=r["tier"])

    from .. import audit  # noqa: PLC0415  (audit imports the stores; keep the engine import-light)

    conflict = audit.cached_conflict(store, r["entities"])
    if conflict:
        yield trace.note("audit", "The graph holds a conflict about this", conflict, "fail")
        return {**_stop(r, "graph_conflict", conflict,
                        f"I can't answer this reliably: the knowledge graph currently holds conflicting facts ({conflict}). "
                        "It needs to be fixed before I answer.", "factual.entity_error"), "route": r}

    if reuse and hist and r["type"] != "consistency":
        hit = yield from cache.lookup(store, question, reasoning_mode, trace)
        if hit:
            return {**hit, "route": r}

    tier, q, result = r["tier"], question, None
    if flags.get("no_verification"):
        result = yield from _unverified(question, "reasoning" if tier == "reasoning" else "factual",
                                        reasoning_mode or "balanced", trace)
        return {**result, "hallucination_type": r["type"], "route": r, "standalone": None, "reused_state": None}

    if tier == "consistency":
        res = yield from memory.answer(store, question, hist, trace)
        if "delegate" in res:
            q, tier = res["delegate"]["question"], res["delegate"]["type"]
        else:
            result = res

    prior = None
    current = bool(r.get("time_sensitive"))  # asks about the present: facts must still be current (temporal.py)
    if result is None:
        prior = states.find(tier, q) if reuse and not current else None
        mixed = r["needs"]["facts"] and r["needs"]["calculation"] and bool(r["entities"]) and not force_tier
        if mixed:
            result = yield from planner.answer(store, q, r["entities"], reasoning_mode or "balanced", trace, prior,
                                               memory.claims(hist), memory.answers(hist), verified_only, current)
        elif tier == "factual":
            result = yield from factual.answer(store, q, r["entities"], trace, prior, memory.claims(hist), verified_only,
                                               current)
        else:
            result = yield from reasoning.answer(q, reasoning_mode or "balanced", trace, prior, memory.answers(hist))

    result = _false_premise(r, result) or result
    cacheable = result.pop("cacheable") and reuse and r["type"] != "consistency" and q == question \
        and not result.get("stopped") and not current
    tokens = meter_["prompt"] + meter_["completion"]
    if reuse and not result.get("stopped") and not current and (state := states.state_of(result)):
        states.save(result["tier"], q, state, tokens)
    if cacheable:
        cache.save(question, {**result, "hallucination_type": r["type"], "tokens": tokens})
        yield trace.note("cache", "Saved this verified path for reuse")
    return {**result, "hallucination_type": r["type"], "route": r, "standalone": q if q != question else None,
            "reused_state": {"question": prior["question"], "similarity": prior["similarity"]} if prior else None}


def _outcome(result: dict) -> str:
    if result.get("stopped"):
        return "abstained"
    if result["confidence"] == "high":
        return "correct"
    return "unknown"


def _reuse_kind(result: dict) -> str | None:
    if result.get("from_cache"):
        return "exact_repeat"
    return result.get("compressed") or ("similar_state" if result.get("reused_state") else None)


def _log(conversation_id, question, result, meter_, source, degraded) -> int:
    saved = result.get("tokens", 0) if result.get("from_cache") else 0
    r = result.get("route") or {}
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO turns(conversation_id, question, hallucination_type, tier, mode, confidence, from_cache, "
            "reused_state, caught, prompt_tokens, completion_tokens, saved_tokens, answer, commitments, source, "
            "outcome, signal_quality, risks, stopped, degraded, scope, reuse_kind, compression_context, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (conversation_id, question, result["hallucination_type"], result["tier"], result.get("mode"),
             result["confidence"], int(bool(result.get("from_cache"))), int(bool(result.get("reused_state"))),
             json.dumps(result.get("caught", [])), meter_["prompt"], meter_["completion"], saved, result["answer"],
             json.dumps(memory.commitments_of(result.get("standalone") or question, result)), source,
             _outcome(result), "self", router.dumps(r) if r else None,
             json.dumps(result.get("stopped")) if result.get("stopped") else None, degraded,
             "frozen" if config.ROUTER_FROZEN else None, _reuse_kind(result),
             json.dumps(meter_.get("compression")) if meter_.get("compression") else None, time.time()),
        )
        today = time.strftime("%Y-%m-%d")
        conn.execute("INSERT INTO usage_daily(day, tokens) VALUES (?, ?) ON CONFLICT(day) DO UPDATE SET "
                     "tokens = tokens + excluded.tokens", (today, meter_["prompt"] + meter_["completion"]))
        return cur.lastrowid


def _error(code: str) -> dict:
    return {"type": "error", "code": code, "message": ERRORS[code]}


def run(store, question: str, mode: str = "auto", typing_delay: float = 0.015, conversation_id: str | None = None,
        force_tier: str | None = None, reuse: bool = True, source: str = "chat", flags: dict | None = None,
        limits: dict | None = None):
    """force_tier / reuse=False / flags are for benchmarking (ablations: no_verification, static_router,
    no_guard, verified_only); limits overrides the per-question budget."""
    question = question.strip()
    trace = Trace()
    flags = flags or {}
    meter_ = meter.new_meter(limits)
    meter_["flags"] = flags
    try:
        try:
            result = yield from meter.metered(
                _solve(store, question, mode, conversation_id, force_tier, reuse, flags, meter_, trace), meter_)
        except meter.BudgetExceeded as e:
            yield trace.note("budget", "Per-question budget used up", str(e), "fail")
            result = {**_stop(None, "budget", str(e), "This question needed more work than one answer is allowed "
                                                      f"({e}), so I stopped instead of guessing."),
                      "hallucination_type": "factual", "route": None}
        if result.get("from_cache"):
            result = {**result, "hallucination_type": result.get("hallucination_type", result["tier"]),
                      "caught": [], "withheld": 0}
        degraded = getattr(store, "degraded", None)
        turn_id = _log(conversation_id, question, result, meter_, source, degraded)
    except llm.LLMNotConfigured:
        yield _error("llm_not_configured")
        return
    except llm.LLMUnavailable:
        log.warning("LLM unavailable", exc_info=True)
        health.record(error=True)
        yield _error("llm_unavailable")
        return
    except GraphUnavailable:
        log.warning("graph unavailable", exc_info=True)
        health.record(error=True)
        yield _error("graph_unavailable")
        return
    except GraphLimitError:
        log.warning("graph limit", exc_info=True)
        yield _error("graph_limit")
        return
    except ReadOnlyGraph:
        yield _error("read_only")
        return
    except Exception:  # surface a safe message to the UI; the details stay in the server log
        log.exception("answer pipeline failed")
        health.record(error=True)
        yield _error("internal")
        return

    health.record(rejected=bool(result.get("stopped")) or any(c["action"] == "dropped" for c in result.get("caught", [])))
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
        "risks": r["risks"] if r else [],
        "needs": r["needs"] if r else None,
        "classifier": r.get("source") if r else None,
        "tier": result["tier"],
        "plan": result.get("plan") or [result["tier"]],
        "mode": result.get("mode"),
        "confidence": result["confidence"],
        "from_cache": bool(result.get("from_cache")),
        "reused_state": result.get("reused_state"),
        "standalone": result.get("standalone"),
        "caught": result.get("caught", []),
        "withheld": result.get("withheld", 0),
        "stopped": result.get("stopped"),
        "degraded": degraded,
        "verified_only": health.state()["verified_only"],
        "tokens": {"prompt": meter_["prompt"], "completion": meter_["completion"],
                   "total": meter_["prompt"] + meter_["completion"], "calls": meter_["calls"],
                   "graph_calls": meter_["graph"],
                   "saved": result.get("tokens", 0) if result.get("from_cache") else 0},
        "router": {"scores": r["scores"], "learned": r["learned"], "evidence": r["evidence"],
                   "frozen": r.get("frozen", False)} if r else None,
        "compression": {"mode": _reuse_kind(result),
                        "prompt_trim": config.COMPRESSION and not flags.get("no_compression"),
                        "context": meter_.get("compression", {})},
        "premises": result.get("premises") or [],
        "path": result["path"],
    }

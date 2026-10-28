"""Runs a question through the tiers and yields the event stream the chat UI renders.

Event types, in order:
  step           thinking-trace step (sent again with the same id when it settles)
  thinking_done  all checks finished ("Processing complete")
  answer         a chunk of answer text
  final          summary: tier, mode, confidence, verified path
  error          the run failed
"""
import re
import time

from .. import llm
from . import cache, factual, reasoning, router
from .trace import Trace

MODES = ("auto", *reasoning.MODES)


def _chunks(text: str):
    for part in re.findall(r"\S+\s*", text):
        yield part


def run(store, question: str, mode: str = "auto", typing_delay: float = 0.015):
    question = question.strip()
    trace = Trace()
    try:
        reasoning_mode = None if mode == "auto" else mode
        result = yield from cache.lookup(store, question, reasoning_mode, trace)
        if result is None:
            ev = trace.start("router", "Deciding how to answer")
            yield ev
            r = router.route(question)
            tier_desc = "factual - verify every claim against the knowledge graph" if r["tier"] == "factual" \
                else "reasoning - check steps, backtrack on contradictions, cross-check solutions"
            yield trace.settle(ev, "ok", f"{tier_desc} ({r['why']})" if r["why"] else tier_desc, tier=r["tier"])

            if r["tier"] == "factual":
                result = yield from factual.answer(store, question, r["entities"], trace)
            else:
                result = yield from reasoning.answer(question, reasoning_mode or "balanced", trace)

            if result.pop("cacheable"):
                cache.save(question, result)
                yield trace.note("cache", "Saved this verified path for reuse")
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
    yield {
        "type": "final",
        "tier": result["tier"],
        "mode": result.get("mode"),
        "confidence": result["confidence"],
        "from_cache": bool(result.get("from_cache")),
        "path": result["path"],
    }

"""Decide which tier answers a question."""
import re

from .. import llm

ROUTER_SYSTEM = """Classify a user's question for a verification pipeline.
- "factual": asks about real-world facts - people and what they did, places, books, characters, plots, events, organisations, dates.
- "reasoning": needs calculation, maths, a logic puzzle, multi-step deduction, or working something out.
Also list the named entities the question is about (people, works, places), as written in the question.
Return only JSON: {"tier": "factual", "entities": ["..."], "why": "short phrase"}"""

_MATHY = re.compile(
    r"\d\s*[-+*/^%x×÷]\s*\d|\b(calculate|compute|solve|how many|how much|probability|percent|average|equation)\b",
    re.I,
)


def route(question: str) -> dict:
    try:
        d = llm.chat_json(ROUTER_SYSTEM, question)
    except llm.LLMBadOutput:
        d = {}
    if not isinstance(d, dict):
        d = {}
    tier = d.get("tier")
    why = str(d.get("why", ""))[:120]
    if tier not in ("factual", "reasoning"):
        tier = "reasoning" if _MATHY.search(question) else "factual"
        why = "keyword fallback"
    entities = [str(e).strip() for e in d.get("entities") or [] if str(e).strip()][:6]
    return {"tier": tier, "entities": entities, "why": why}

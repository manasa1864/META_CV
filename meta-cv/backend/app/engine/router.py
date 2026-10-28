"""Hallucination taxonomy + learned routing.

1. Classify which hallucination a question is most at risk of: factual, reasoning or consistency.
2. Pick the tier that has worked best for that type so far. Every answered turn is logged
   (see pipeline) and its outcome - benchmark ground truth, else user feedback, else whether
   the answer came out fully verified - updates the per (type, tier) success rate.
   A prior keeps the obvious specialist in front until real evidence says otherwise.
"""
import re

from .. import llm
from ..db import connect

TYPES = ("factual", "reasoning", "consistency")
SPECIALIST = {"factual": "factual", "reasoning": "reasoning", "consistency": "consistency"}

PRIOR_WEIGHT = 4      # pseudo-observations behind the prior
PRIOR_SPECIALIST = 0.8
PRIOR_OTHER = 0.3
TOKEN_PENALTY = 0.02  # score lost per 1k tokens, so the cheaper tier wins a near tie

CLASSIFY_SYSTEM = """Classify which kind of hallucination answering this question is most at risk of.
- "factual": asks about real-world facts - people and what they did, places, books, characters, events, organisations, dates. Risk: invented or wrong facts.
- "reasoning": needs calculation, maths, a logic puzzle or multi-step deduction. Risk: an illogical or miscalculated step.
- "consistency": follows up on the CONVERSATION SO FAR - refers back to earlier answers ("you said", "that", "he", "then what about..."), or asks again about something already answered. Risk: contradicting what was said earlier. Only possible when there is a conversation so far.
Also list the named entities the question is about (people, works, places), resolving pronouns from the conversation.
Return only JSON: {"type": "factual", "entities": ["..."], "why": "short phrase"}"""

_MATHY = re.compile(
    r"\d\s*[-+*/^%x×÷]\s*\d|\b(calculate|compute|solve|how many|how much|probability|percent|average|equation)\b",
    re.I,
)
_FOLLOW_UP = re.compile(r"\b(you said|earlier|before|previous|again|that result|that answer|remind me)\b", re.I)

SUCCESS_SQL = "COALESCE(correct, feedback, CASE WHEN confidence = 'high' THEN 1 ELSE 0 END)"


def classify(question: str, history_txt: str = "") -> dict:
    user = f"Conversation so far:\n{history_txt}\n\nQuestion: {question}" if history_txt else f"Question: {question}"
    try:
        d = llm.chat_json(CLASSIFY_SYSTEM, user)
    except llm.LLMBadOutput:
        d = {}
    if not isinstance(d, dict):
        d = {}
    kind, why = d.get("type"), str(d.get("why", ""))[:120]
    if kind == "consistency" and not history_txt:
        kind = None
    if kind not in TYPES:
        if history_txt and _FOLLOW_UP.search(question):
            kind = "consistency"
        else:
            kind = "reasoning" if _MATHY.search(question) else "factual"
        why = "keyword fallback"
    entities = [str(e).strip() for e in d.get("entities") or [] if str(e).strip()][:6]
    return {"type": kind, "entities": entities, "why": why}


def stats() -> dict:
    """{(type, tier): {"n", "successes", "avg_tokens"}} over every logged, non-cached turn."""
    with connect() as conn:
        rows = conn.execute(
            f"SELECT hallucination_type AS type, tier, COUNT(*) AS n, SUM({SUCCESS_SQL}) AS successes, "
            "AVG(prompt_tokens + completion_tokens) AS avg_tokens FROM turns WHERE from_cache = 0 "
            "GROUP BY hallucination_type, tier"
        ).fetchall()
    return {(r["type"], r["tier"]): {"n": r["n"], "successes": r["successes"] or 0, "avg_tokens": r["avg_tokens"] or 0}
            for r in rows}


def score(kind: str, tier: str, st: dict) -> float:
    s = st.get((kind, tier), {"n": 0, "successes": 0, "avg_tokens": 0})
    prior = PRIOR_SPECIALIST if SPECIALIST[kind] == tier else PRIOR_OTHER
    rate = (s["successes"] + prior * PRIOR_WEIGHT) / (s["n"] + PRIOR_WEIGHT)
    return rate - TOKEN_PENALTY * s["avg_tokens"] / 1000


def candidates(has_history: bool) -> tuple[str, ...]:
    return ("factual", "reasoning", "consistency") if has_history else ("factual", "reasoning")


def choose(kind: str, has_history: bool, st: dict) -> tuple[str, dict]:
    scores = {t: round(score(kind, t, st), 3) for t in candidates(has_history)}
    return max(scores, key=lambda t: (scores[t], t == SPECIALIST[kind])), scores


def route(question: str, history_txt: str = "", force_tier: str | None = None) -> dict:
    c = classify(question, history_txt)
    st = stats()
    tier, scores = choose(c["type"], bool(history_txt), st)
    if force_tier:
        tier = force_tier
    evidence = st.get((c["type"], tier), {}).get("n", 0)
    return {**c, "tier": tier, "scores": scores, "evidence": evidence,
            "learned": tier != SPECIALIST[c["type"]] and not force_tier, "forced": bool(force_tier)}


def table() -> list[dict]:
    """Current router state for the dashboard: per type, each tier's observed record and score."""
    st = stats()
    out = []
    for kind in TYPES:
        for tier in candidates(True):
            s = st.get((kind, tier), {"n": 0, "successes": 0, "avg_tokens": 0})
            out.append({"type": kind, "tier": tier, "n": s["n"], "successes": s["successes"],
                        "success_rate": s["successes"] / s["n"] if s["n"] else None,
                        "avg_tokens": round(s["avg_tokens"]), "score": round(score(kind, tier, st), 3)})
    return out

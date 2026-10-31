"""Hallucination taxonomy v2 + learned routing.

Risk codes, adapted from the factuality / faithfulness split in Huang et al., "A Survey on
Hallucination in Large Language Models" (arXiv:2311.05232), restricted to what this system can check.
A question gets a *profile* (several weighted risks + what it needs), not one label; `type` is kept
as the primary risk family for backward compatibility. COVERS states which risks each tier verifies,
so the router, the dashboard and the benchmark share one definition.

Routing: per (primary type, tier), score = P(correct) - ROUTER_LAMBDA * P(hallucinated)
- ABSTAIN_COST * P(abstained) - a small token cost, with a prior that favours the specialist.
Outcomes are weighted by signal quality (ground truth > user feedback > the system's own confidence,
which is excluded by default because it is circular). ROUTER_FROZEN reads the trained state but
never changes it.
"""
import json
import re

from .. import config, llm
from ..db import connect

RISKS = {
    "factual.entity_error": "wrong entity, date or number",
    "factual.relation_error": "right entities, wrong relation",
    "factual.fabrication": "unverifiable invented detail",
    "factual.overclaim": "certainty beyond the evidence, or a false premise accepted",
    "faithful.logic": "bad arithmetic or inference",
    "faithful.context": "contradicts earlier turns",
    "faithful.premise": "calculation built on an unverified knowledge premise",
    "temporal.stale": "the answer may have changed since the fact was verified",
}
TYPES = ("factual", "reasoning", "consistency")
SPECIALIST = {"factual": "factual", "reasoning": "reasoning", "consistency": "consistency"}
COVERS = {
    "factual": {"factual.entity_error", "factual.relation_error", "factual.fabrication", "factual.overclaim",
                "temporal.stale"},
    "reasoning": {"faithful.logic", "faithful.premise"},
    "consistency": {"faithful.context"},
}
DEFAULT_RISK = {"factual": "factual.entity_error", "reasoning": "faithful.logic", "consistency": "faithful.context"}


def family(code: str) -> str:
    """The primary type a risk code belongs to (also the legacy `type` of a caught entry)."""
    for tier, codes in COVERS.items():
        if code in codes:
            return tier
    return "factual"


def caught(risk: str, origin_tier: str, action: str, detail: str) -> dict:
    """One hallucination caught before output. `type` (legacy) is the risk's family."""
    return {"risk": risk, "type": family(risk), "origin_tier": origin_tier, "action": action, "detail": detail}


PRIOR_WEIGHT = 4
PRIOR = {True: {"correct": 0.8, "hallucinated": 0.1, "abstained": 0.1},
         False: {"correct": 0.3, "hallucinated": 0.3, "abstained": 0.4}}
TOKEN_PENALTY = 0.02  # score lost per 1k tokens, so the cheaper tier wins a near tie
OUTCOMES = ("correct", "abstained", "hallucinated")

CLASSIFY_SYSTEM = """Profile the hallucination risks of answering this question. Risk codes:
- factual.entity_error: could state a wrong entity, date or number
- factual.relation_error: right entities, wrong relation between them
- factual.fabrication: could invent an unverifiable detail
- factual.overclaim: the question may carry a false premise, or invite certainty beyond the evidence
- faithful.logic: needs arithmetic or multi-step inference that could go wrong
- faithful.premise: a calculation that depends on a real-world fact
- faithful.context: follows up on the CONVERSATION SO FAR (refers back, "he", "that result"); only possible when there is a conversation
- temporal.stale: the answer may change over time (current holders, latest, now)
needs: facts = real-world facts are required; calculation = arithmetic, logic or a proof is required; history = earlier turns are required.
false_premise_suspected: the question presupposes something that may be untrue (e.g. "When did X win Y?" when X may not have).
List the named entities (people, works, places), resolving pronouns from the conversation.
Return only JSON: {"risks": [{"label": "factual.entity_error", "weight": 0.8}], "needs": {"facts": true, "calculation": false, "history": false},
"false_premise_suspected": false, "time_sensitive": false, "entities": ["..."], "why": "short phrase"}"""

_RANGE = re.compile(r"\b\d{1,4}(?:-|\s*–\s*)\d{1,4}\b")           # 1643-1715, 9–5: ranges, not subtraction
_BARE_SUM = re.compile(r"^\s*(?:what(?:'s| is)\s+)?[\d\s.+\-−*/×÷^()]+\??\s*$", re.I)
_DATE = re.compile(r"\b\d{1,4}[/.]\d{1,2}[/.]\d{1,4}\b")
_OP = re.compile(r"\d\s*(?:\*\*|[+*/^×÷])\s*\d|\d\s+[-−]\s+\d|\d\s*x\s*\d")
_PROOF = re.compile(r"\b(prove|show that|derive|demonstrate that)\b", re.I)
_TASK = re.compile(
    r"\b(calculate|compute|solve|simplify|evaluate)\b"
    r"|\bhow (long|far|fast) (will|would|does|do|did)\b"
    r"|\bhow many \w+ (?:are |is )?(?:there |are )?(?:left|remain|in total|altogether)\b"
    r"|\bhow many \w+ (?:are )?(?:there )?in \d"
    r"|\bhow many (?:years|days|months|hours|minutes) (?:after|before|between|since|until|from)\b"
    r"|\bhow old was .+ when\b"
    r"|\bwhat is the (?:sum|product|difference|total|average|mean|ratio) of\b"
    r"|\d+(?:\.\d+)?\s*(?:%|percent)\s+of\s+\$?\d",
    re.I)
_NUMBER = re.compile(r"(?<![\w.])\$?\d+(?:[.,]\d+)?")
_UNIT = re.compile(r"\d\s*(?:km/h|mph|m/s|km|miles?|m|cm|metres?|meters?|kg|g|litres?|liters?|hours?|minutes?|"
                   r"seconds?|days?|weeks?|months?|years?|dollars?|cents?|%|percent|per)\b|\$\s*\d", re.I)
_FOLLOW_UP = re.compile(r"\b(you said|earlier|before that|previous|again|that result|that answer|remind me)\b"
                        r"|^(and|so|then|also|what about|how about)\b|\b(he|she|him|her|his|they|them|their|it|that|those)\b", re.I)
_TIME = re.compile(r"\b(current(ly)?|now|today|latest|this year|still|recent(ly)?|as of)\b", re.I)
_PROPER = re.compile(r"(?<!^)(?<![.?!]\s)\b[A-Z][a-z]+")
_WH_FACT = re.compile(r"\b(who|where|which|when|what year)\b", re.I)


def looks_computational(text: str) -> bool:
    """Shared by the keyword fallback and the consistency tier. Ranges and dates are not arithmetic;
    'how many / average / percent' alone are not enough; word problems with rates and units, and
    proofs, are."""
    if _BARE_SUM.match(text) and re.search(r"\d\s*[-−+*/×÷^]\s*\d", text):
        return True
    t = _DATE.sub(" ", _RANGE.sub(" ", text))
    if _PROOF.search(t) or _OP.search(t) or _TASK.search(t):
        return True
    numbers = len(_NUMBER.findall(t))
    return numbers >= 3 or (numbers >= 2 and bool(_UNIT.search(t)))


def _needs_facts(text: str) -> bool:
    return bool(_PROPER.search(text) or _WH_FACT.search(text))


def coerce_entities(value) -> list[str]:
    out = []

    def walk(v):
        if v is None:
            return
        if isinstance(v, (list, tuple, set)):
            for x in v:
                walk(x)
        elif isinstance(v, dict):
            walk(v.get("name") or v.get("entity"))
        else:
            s = str(v).strip()
            if s and s not in out:
                out.append(s)

    walk(value)
    return out[:6]


def _profile(risks: list[dict], needs: dict, false_premise: bool, time_sensitive: bool, entities: list[str],
             why: str, source: str) -> dict:
    risks = sorted(risks, key=lambda r: -r["weight"])
    return {"type": family(risks[0]["label"]), "risks": risks, "needs": needs,
            "false_premise_suspected": false_premise, "time_sensitive": time_sensitive,
            "entities": entities, "why": why[:120], "source": source}


def fallback_profile(question: str, has_history: bool) -> dict:
    calc, facts = looks_computational(question), _needs_facts(question)
    follow = has_history and bool(_FOLLOW_UP.search(question))
    timely = bool(_TIME.search(question))
    risks = []
    if follow:
        risks.append({"label": "faithful.context", "weight": 0.9})
    if calc:
        risks.append({"label": "faithful.logic", "weight": 0.8})
        if facts:
            risks.append({"label": "faithful.premise", "weight": 0.6})
    if facts or not calc:
        risks.append({"label": "factual.entity_error", "weight": 0.7 if not calc else 0.5})
    if timely:
        risks.append({"label": "temporal.stale", "weight": 0.4})
    return _profile(risks, {"facts": facts or not calc, "calculation": calc, "history": follow}, False, timely,
                    [], "keyword fallback", "fallback")


def _parse(d, has_history: bool) -> dict | None:
    if not isinstance(d, dict):
        return None
    raw = d.get("risks")
    if isinstance(raw, dict):
        raw = [{"label": k, "weight": v} for k, v in raw.items()]
    risks = []
    for r in raw if isinstance(raw, list) else []:
        code, weight = (r, 1.0) if isinstance(r, str) else (r.get("label"), r.get("weight", 1.0)) if isinstance(r, dict) else (None, 0)
        try:
            weight = min(1.0, max(0.0, float(weight)))
        except (TypeError, ValueError):
            weight = 0.5
        if code in RISKS and not (code == "faithful.context" and not has_history) and code not in {x["label"] for x in risks}:
            risks.append({"label": code, "weight": weight})
    legacy = d.get("type") or d.get("tier")
    if not risks and legacy in TYPES and not (legacy == "consistency" and not has_history):
        risks = [{"label": DEFAULT_RISK[legacy], "weight": 1.0}]
    if not risks:
        return None
    needs = d.get("needs") if isinstance(d.get("needs"), dict) else {}
    labels = {r["label"] for r in risks}
    needs = {
        "facts": bool(needs.get("facts", any(family(c) == "factual" for c in labels) or "faithful.premise" in labels)),
        "calculation": bool(needs.get("calculation", bool(labels & COVERS["reasoning"]))),
        "history": bool(needs.get("history", "faithful.context" in labels)) and has_history,
    }
    return _profile(risks, needs, bool(d.get("false_premise_suspected")), bool(d.get("time_sensitive")),
                    coerce_entities(d.get("entities")), str(d.get("why", "")), "llm")


def classify(question: str, history_txt: str = "", attempts: int = 2) -> dict:
    """Never raises for a model problem: bounded retry, then the keyword fallback."""
    user = f"Conversation so far:\n{history_txt}\n\nQuestion: {question}" if history_txt else f"Question: {question}"
    for _ in range(attempts):
        try:
            p = _parse(llm.chat_json(CLASSIFY_SYSTEM, user), bool(history_txt))
        except (llm.LLMBadOutput, llm.LLMUnavailable, llm.LLMNotConfigured, TypeError, ValueError, AttributeError):
            p = None
        if p:
            return p
    return fallback_profile(question, bool(history_txt))


def _signal_weight(quality: str | None) -> float:
    return {"ground_truth": 1.0, "user": config.USER_SIGNAL_WEIGHT, "self": config.SELF_SIGNAL_WEIGHT}.get(quality or "", 0.0)


def _row_outcome(r) -> tuple[str | None, str | None]:
    """(outcome, signal quality), reading legacy rows too."""
    if r["outcome"] in OUTCOMES and r["signal_quality"]:
        return r["outcome"], r["signal_quality"]
    if r["correct"] is not None:
        return ("correct" if r["correct"] else "hallucinated"), "ground_truth"
    if r["feedback"] is not None:
        return ("correct" if r["feedback"] else "hallucinated"), "user"
    if r["confidence"] == "high":
        return "correct", "self"
    return None, None


def stats() -> dict:
    """{(type, tier): {"n", "correct", "abstained", "hallucinated", "avg_tokens"}} over weighted outcomes."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT hallucination_type AS type, tier, outcome, signal_quality, correct, feedback, confidence, "
            "prompt_tokens + completion_tokens AS tokens FROM turns "
            "WHERE from_cache = 0 AND COALESCE(scope, '') != 'frozen' AND source NOT IN ('scale', 'loadtest', 'replay') "
            "ORDER BY id DESC LIMIT 5000"
        ).fetchall()
    out: dict = {}
    for r in rows:
        outcome, quality = _row_outcome(r)
        w = _signal_weight(quality)
        if not outcome or w <= 0:
            continue
        s = out.setdefault((r["type"], r["tier"]), {"n": 0.0, "correct": 0.0, "abstained": 0.0, "hallucinated": 0.0,
                                                     "tokens": 0.0, "count": 0})
        s["n"] += w
        s[outcome] += w
        s["tokens"] += r["tokens"] or 0
        s["count"] += 1
    for s in out.values():
        s["avg_tokens"] = s.pop("tokens") / max(1, s["count"])
    return out


def score(kind: str, tier: str, st: dict) -> float:
    s = st.get((kind, tier), {"n": 0, "correct": 0, "abstained": 0, "hallucinated": 0, "avg_tokens": 0})
    prior = PRIOR[SPECIALIST[kind] == tier]
    rate = {o: (s.get(o, 0) + prior[o] * PRIOR_WEIGHT) / (s["n"] + PRIOR_WEIGHT) for o in OUTCOMES}
    return (rate["correct"] - config.ROUTER_LAMBDA * rate["hallucinated"] - config.ABSTAIN_COST * rate["abstained"]
            - TOKEN_PENALTY * s.get("avg_tokens", 0) / 1000)


def candidates(has_history: bool) -> tuple[str, ...]:
    return ("factual", "reasoning", "consistency") if has_history else ("factual", "reasoning")


def choose(kind: str, has_history: bool, st: dict) -> tuple[str, dict]:
    scores = {t: round(score(kind, t, st), 3) for t in candidates(has_history)}
    return max(scores, key=lambda t: (scores[t], t == SPECIALIST[kind])), scores


def route(question: str, history_txt: str = "", force_tier: str | None = None, profile: dict | None = None) -> dict:
    c = profile or classify(question, history_txt)
    st = stats()
    tier, scores = choose(c["type"], bool(history_txt), st)
    if force_tier:
        tier = force_tier
    evidence = round(st.get((c["type"], tier), {}).get("n", 0), 1)
    return {**c, "tier": tier, "scores": scores, "evidence": evidence,
            "learned": tier != SPECIALIST[c["type"]] and not force_tier, "forced": bool(force_tier),
            "frozen": config.ROUTER_FROZEN}


def table() -> list[dict]:
    """Current router state for the dashboard: per type, each tier's weighted record and score."""
    st = stats()
    out = []
    for kind in TYPES:
        for tier in candidates(True):
            s = st.get((kind, tier), {"n": 0, "correct": 0, "abstained": 0, "hallucinated": 0, "avg_tokens": 0})
            n = s["n"]
            out.append({"type": kind, "tier": tier, "n": round(n, 1), "successes": round(s["correct"], 1),
                        "success_rate": s["correct"] / n if n else None,
                        "abstain_rate": s["abstained"] / n if n else None,
                        "hallucination_rate": s["hallucinated"] / n if n else None,
                        "avg_tokens": round(s["avg_tokens"]), "score": round(score(kind, tier, st), 3)})
    return out


def dumps(profile: dict) -> str:
    return json.dumps({k: profile.get(k) for k in ("risks", "needs", "false_premise_suspected", "time_sensitive", "source")})

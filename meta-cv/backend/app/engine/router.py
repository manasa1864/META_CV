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

# ----------------------------------------------------------------------------- keyword fallback
# Used only when the model's classification fails. Each cue below is a general class of wording, not a
# fix for a particular question; bench_data/taxonomy_holdout*.json measure whether that generalises.
_NUMWORD = (r"(?:two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|"
            r"seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|"
            r"million|half|twice|double|triple|quarter|dozen)")
_N = rf"(?:\$?\d+(?:[.,]\d+)*(?!\d)(?!st|nd|rd|th)|{_NUMWORD})"
_NUMBER = re.compile(rf"(?<![\w.]){_N}\b", re.I)
_RANGE = re.compile(r"\b\d{1,4}(?:-|\s*–\s*)\d{1,4}\b")           # 1643-1715, 9–5: ranges, not subtraction
_DATE = re.compile(r"\b\d{1,4}[/.]\d{1,2}[/.]\d{1,4}\b")
# a word glued to a number is a name, not a quantity: "Boeing 747", "Python 3.12", "COVID-19", "Super Bowl 50"
_NAMED_NUMBER = re.compile(r"\b(?:[A-Z][A-Za-z]*|[a-z]+[A-Z][A-Za-z]*) ?\d+(?:\.\d+)*\b(?!\s*(?:km|kg|m|cm|mm|%|"
                           r"percent|years?|hours?|minutes?|days?|litres?|liters?|people|cups?|dollars?))"
                           r"|\b[A-Za-z]+-\d+\b")


def _strip_names(t: str) -> str:
    """Drop numbers that are part of a name. A capitalised word starting a sentence is not a name."""
    def repl(m):
        before = t[max(0, m.start() - 2):m.start()]
        sentence_start = m.start() == 0 or before.rstrip() in (".", "?", "!") or before.endswith((". ", "? ", "! "))
        return m.group(0) if sentence_start and "-" not in m.group(0) else " "
    return _NAMED_NUMBER.sub(repl, t)


_BARE_SUM = re.compile(r"^\s*(?:what(?:'s| is)\s+)?[\d\s.,+\-−*/×÷^()x]+\??\s*$", re.I)
_SYMBOL_OP = re.compile(r"\d\s*(?:\*\*|[+*/^×÷])\s*\d|\d\s+[-−]\s+\d|\d\s*x\s*\d")

_CUES = {
    # explicit arithmetic between numbers: "45 times 12", "1,200 divided by 16", "18 percent of 450"
    "arithmetic": rf"{_N}\s+(?:times|multiplied by|divided by|plus|minus|over)\s+{_N}"
                  rf"|{_N}\s*(?:%|percent)\s+of\s+{_N}",
    # multiplicative comparisons: "three times as old", "twice as many"
    "multiplier": rf"\b(?:{_NUMWORD}|\d+)\s+times\s+as\b|\b(?:twice|half|triple)\s+as\b",
    # instructions to compute: "divide ...", "convert ...", "find the median ...", "double that"
    "imperative": r"^(?:please\s+|can you\s+|now\s+|then\s+)*(?:calculate|compute|solve|simplify|evaluate|divide|multiply|"
                  r"add up|subtract|convert|double|halve|triple|work out|find (?:the )?(?:sum|product|difference|median|"
                  r"mean|average|remainder|area|volume|perimeter|value|total))\b",
    "verbs": r"\b(?:calculate|compute|solve for|simplify|evaluate)\b"
             r"|\b(?:divided by|multiplied by|subtracted from)\b"
             r"|\bwhat(?:'s| is| are)? the (?:sum|product|difference|total|average|mean|ratio|quotient|remainder) of\b",
    # proofs and derivations
    "proof": r"\b(?:prove|show that|derive|demonstrate that)\b",
    # syllogisms and logic puzzles: "if all A are B ..."
    "logic": r"\b(?:if|suppose|given that)\b[^?]*\b(?:all|no|some|every)\s+\w+\s+(?:are|is)\b",
    "sequence": r"\bnext (?:number|term|value)\b|\bin the (?:sequence|series|pattern)\b",
    # durations and differences that must be derived: "how long did X last", "how many years apart"
    "duration": r"\bhow (?:long|many (?:years|days|months|decades|centuries))\s+(?:did|does|do|was|were|is|has|have)\b"
                r"[^?]*\b(?:last|reign|rule|serve|live|take|run|exist|go on)\b"
                r"|\bhow many (?:years|days|months|weeks|hours|minutes|decades|centuries)\b[^?]{0,40}"
                r"\b(?:apart|after|before|between|since|until|passed|separate|elapsed|ago|later|earlier)\b"
                r"|\bhow long (?:after|before|between|ago|will|would|until)\b"
                r"|\bhow old (?:was|were|is|will|would)\b[^?]*\b(?:when|in|at|by|after)\b",
    # quantity comparisons: "by how many years", "how much taller", "difference in height between"
    "comparison": r"\bby how (?:many|much)\b"
                  r"|\bhow much (?:older|younger|taller|shorter|longer|heavier|lighter|bigger|smaller|more|less|"
                  r"cheaper|faster|slower|further|farther)\b"
                  r"|\bdifference (?:in|of) (?:height|age|weight|length|size|population|price|cost|speed|time|years|"
                  r"distance|area)\b",
    # totals over several things: "combined, how many legs"
    "total": r"\b(?:combined|in total|altogether|put together|added together)\b[^?]*\b(?:how (?:many|much)|number)\b"
             r"|\b(?:how (?:many|much)|number)\b[^?]*\b(?:combined|in total|altogether)\b"
             r"|\btotal number of\b",
    # derived counts: "how many apples are left", "how many minutes in 3.5 hours"
    "derived_count": rf"\bhow many \w+ (?:are |is )?(?:there |are )?(?:left|remain|remaining)\b"
                     rf"|\bhow many \w+ (?:are )?(?:there )?in (?:a |an )?{_N}",
    # statistics and geometry on given numbers
    "stat_words": r"\b(?:remainder|median|quotient|square root|squared|cubed|factorial)\b|\b(?:average|mean) of\s+\d",
    "measure": r"\b(?:area|perimeter|volume|circumference|diagonal|hypotenuse)\b",
    "probability": r"\b(?:probability|odds|chance)\b",
    # conditional change of a quantity: "if a phone loses half its value every year"
    "change": r"\bif\b[^?]*\b(?:loses|gains|grows|doubles|halves|triples|increases|decreases|drops|rises|falls)\b",
    # word problems: rates and units next to numbers
    "unit": rf"{_N}\s*(?:km/h|mph|m/s|km|miles?|m|cm|mm|metres?|meters?|kg|g|litres?|liters?|cups?|hours?|minutes?|"
            rf"seconds?|days?|weeks?|months?|years?|dollars?|cents?|euros?|%|percent)\b|\$\s*\d"
            r"|\b(?:per|each|every|a|an) (?:minute|hour|day|week|month|year|litre|liter|km|mile|person|item)\b",
}
_C = {k: re.compile(v, re.I) for k, v in _CUES.items()}
_NEEDS_NUMBER = ("stat_words", "measure", "probability", "change")   # only computational when numbers are given
_PROBABILITY_OBJECTS = re.compile(r"\b(?:dice|die|coins?|cards?|draw|roll|flip|toss)\b", re.I)

_FOLLOW_UP = re.compile(
    r"^(?:and|so|then|also|now|but|wait|ok|okay|hmm|what about|how about)\b"
    r"|\b(?:the same|same for|instead|didn't you|did you|you (?:said|say|mentioned|told)|earlier|before|previous|"
    r"again|remind me|that result|that answer|that one)\b"
    r"|\b(?:he|she|him|her|his|hers|they|them|their|it|its|that|those|this)\b", re.I)
_TIME = re.compile(r"\b(current(ly)?|now|today|latest|this year|still|recent(ly)?|as of)\b", re.I)
_PROPER = re.compile(r"(?<!^)(?<![.?!]\s)\b[A-Z][a-z]+")
_WH_FACT = re.compile(r"\b(who|where|which|when|what year)\b", re.I)


_LEAD_IN = re.compile(r"^(?:(?:could|can|would|will) you(?: please)?(?: (?:tell|show|help) me(?: with)?| work out| figure out)?"
                      r"|please|i (?:want|need|would like) to know|tell me|quick question|hey|hi)[\s,:;-]*", re.I)


def clean(text: str) -> str:
    """Collapse whitespace and drop polite lead-ins, so wording around a question does not change its type."""
    t = " ".join(str(text).split())
    while True:
        stripped = _LEAD_IN.sub("", t, count=1)
        if stripped == t or not stripped:
            return t
        t = stripped


def _numbers(text: str) -> int:
    t = _strip_names(_DATE.sub(" ", _RANGE.sub(" ", clean(text))))
    return len(_NUMBER.findall(t))


def computational_cues(text: str) -> list[str]:
    """Which cue groups fire (for tests and the trace)."""
    text = clean(text)
    t = _DATE.sub(" ", _RANGE.sub(" ", text))
    n = _numbers(text)
    fired = [k for k, rx in _C.items() if k != "unit" and rx.search(t)]
    fired = [k for k in fired if k not in _NEEDS_NUMBER or n > 0
             or (k == "probability" and _PROBABILITY_OBJECTS.search(t))]
    if _BARE_SUM.match(text) and re.search(r"\d\s*[-−+*/×÷^x]\s*\d", text):
        fired.append("bare_sum")
    if _SYMBOL_OP.search(_strip_names(t)):
        fired.append("symbols")
    if n >= 3 or (n >= 2 and _C["unit"].search(t)):
        fired.append("word_problem")
    return fired


def looks_computational(text: str) -> bool:
    """Shared by the keyword fallback and the consistency tier."""
    return bool(computational_cues(text))


def _needs_facts(text: str, calc: bool) -> bool:
    text = clean(text)
    if _PROPER.search(text) or _WH_FACT.search(text):
        return True
    # a calculation with no numbers given must take them from the world ("how long did the war last")
    pure = set(computational_cues(text)) & {"proof", "logic", "sequence", "bare_sum", "symbols", "arithmetic"}
    return calc and _numbers(text) == 0 and not pure


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
    calc = looks_computational(question)
    facts = _needs_facts(question, calc)
    follow = has_history and bool(_FOLLOW_UP.search(clean(question)))
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

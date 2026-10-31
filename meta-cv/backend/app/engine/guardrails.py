"""Final-answer faithfulness checks, applied before an answer streams.

factual      every number, year and proper noun in the composed text must appear in the accepted
             triples (or the question), and every atomic claim the text asserts must match an accepted
             triple. On failure: regenerate once with a stricter instruction; if that fails too,
             return a deterministic rendering of the accepted triples and record factual.fabrication.
reasoning    the final numeric answer must equal the result of the last machine-checked step.
consistency  names and numbers must come from the earlier verified statements it cites (or from
             machine-checked calculations), and every number a calculation uses must trace to
             the question or those statements.
"""
import re

from .. import llm, meter
from . import mathcheck
from .entities import normalize
from .router import caught
from .trace import Trace

_NUM = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")
_CAP = re.compile(r"\b[A-Z][a-zA-Z'’-]+\b")
COMMON = set("""a an the it its he she they his her their them this that these those in on at by for and but or i we
you yes no however also both there here not note verified could unfortunately according while although as after
before when which who what where why how so if then than is was were be been one two first finally overall in
based from with of to my our your answer question facts fact sorry nothing""".split())

EXTRACT_SYSTEM = """List the atomic factual claims the text asserts as true, as subject-relation-object triples.
Ignore statements that something could not be verified, and ignore hedges or questions.
Return only JSON: {"claims": [{"subject": "...", "relation": "...", "object": "..."}]}"""


def disabled() -> bool:
    """Ablation switch (bench --ablate no_guard), carried on the per-question meter."""
    m = meter.current()
    return bool(m and m.get("flags", {}).get("no_guard"))


def _blob(*parts: str) -> str:
    return " " + normalize(" ".join(parts)) + " "


def unsupported_terms(text: str, allowed: str) -> list[str]:
    """Numbers and proper nouns in `text` that do not occur in the normalised `allowed` blob."""
    bad = []
    for n in _NUM.findall(text):
        v = n.replace(",", "")
        if f" {v} " not in allowed and f" {normalize(n)} " not in allowed:
            bad.append(n)
    for w in _CAP.findall(text):
        lw = normalize(w)
        if lw in COMMON or len(lw) < 2:
            continue
        if f" {lw} " not in allowed and not any(f" {p} " in allowed for p in lw.split()):
            bad.append(w)
    return sorted(set(bad))


def _claim_matches(c: dict, t: dict) -> bool:
    from .factual import same_entity, same_value

    s, o = str(c.get("subject", "")), str(c.get("object", ""))
    return (same_entity(s, t["subject"]) and same_value(o, t["object"])) or \
           (same_entity(s, t["object"]) and same_value(o, t["subject"]))


def factual_problems(question: str, text: str, accepted: list[dict], missing: list[dict]) -> list[str]:
    allowed = _blob(question, *(f"{c['subject']} {c['relation'].replace('_', ' ')} {c['object']}" for c in accepted),
                    *(f"{c['subject']} {c['relation'].replace('_', ' ')}" for c in missing),
                    *(f"{c['corrects']['subject']} {c['corrects']['object']}" for c in accepted if c.get("corrects")))
    problems = [f"'{t}' is not in the verified facts" for t in unsupported_terms(text, allowed)]
    try:
        d = llm.chat_json(EXTRACT_SYSTEM, text)
    except llm.LLMBadOutput:
        d = {}
    claims = d.get("claims") if isinstance(d, dict) else d
    for c in claims if isinstance(claims, list) else []:
        if isinstance(c, dict) and c.get("subject") and c.get("object") and not any(_claim_matches(c, t) for t in accepted):
            problems.append(f"claim '{c['subject']} {c.get('relation', '')} {c['object']}' matches no verified fact")
    return problems


def render_verified(accepted: list[dict], missing: list[dict]) -> str:
    lines = ["Verified: " + "; ".join(f"{c['subject']} {c['relation'].replace('_', ' ')} {c['object']}" for c in accepted) + "."]
    if missing:
        lines.append("Could not verify: " + "; ".join(f"{c['subject']} {c['relation'].replace('_', ' ')}" for c in missing) + ".")
    return "\n".join(lines)


def guard_factual(question: str, text: str, accepted: list[dict], missing: list[dict], prompt: str, strict_system: str,
                  trace: Trace, found: list[dict]):
    """Generator: returns faithful text, regenerating once and then falling back to a deterministic rendering."""
    if disabled():
        return text
    ev = trace.start("guard", "Checking the answer says only what was verified")
    yield ev
    problems = factual_problems(question, text, accepted, missing)
    if not problems:
        yield trace.settle(ev, "ok", "Every name, number and claim in the answer is backed by a verified node.")
        return text
    yield trace.settle(ev, "warn", f"The composed answer added unverified detail ({'; '.join(problems[:3])}). Regenerating.")
    retry = llm.chat_text(strict_system, prompt)
    again = factual_problems(question, retry, accepted, missing)
    if not again:
        found.append(caught("factual.fabrication", "factual", "corrected", "; ".join(problems[:3])))
        yield trace.note("guard", "Regenerated answer passes", "Only verified details remain.", "ok")
        return retry
    found.append(caught("factual.fabrication", "factual", "dropped", "; ".join(again[:3])))
    yield trace.note("guard", "Falling back to the verified facts verbatim",
                     f"The regenerated answer still added unverified detail ({'; '.join(again[:3])}).", "fail")
    return render_verified(accepted, missing)


def reasoning_problem(steps: list[dict], final_answer: str) -> str | None:
    """None if the final answer equals the last machine-checked step (or nothing can be checked)."""
    checked = [s for s in steps if s.get("expression")]
    if disabled() or not checked or mathcheck.to_number(final_answer) is None:
        return None
    try:
        value = mathcheck.evaluate(checked[-1]["expression"])
    except mathcheck.NotArithmetic:
        return None
    if mathcheck.matches(value, final_answer):
        return None
    return f"final answer {final_answer} differs from the last checked step ({checked[-1]['expression']} = {mathcheck.fmt(value)})"


def consistency_problems(question: str, text: str, used: list[dict], calculations: list[dict]) -> list[str]:
    if disabled():
        return []
    parts = [question]
    for c in used:
        parts.append(f"{c['subject']} {c['relation'].replace('_', ' ')} {c['object']}" if c.get("kind", "claim") == "claim"
                     else f"{c['question']} {c['final_answer']}")
    sources = "\n".join(parts)
    for k in calculations:
        parts.append(f"{k.get('expression', '')} {k.get('result', '')}")
    problems = [f"'{t}' is not in the cited statements" for t in unsupported_terms(text, _blob(*parts))]
    for k in calculations:
        err = mathcheck.check_step(k)
        if err:
            problems.append(f"calculation wrong: {err}")
    untraced = mathcheck.untraced_number([{"text": "", **k} for k in calculations], sources)
    if untraced:
        problems.append(f"calculation {untraced[0] + 1} uses a number with no source: {untraced[1]}")
    return problems

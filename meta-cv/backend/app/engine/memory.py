"""Conversation memory and the consistency tier.

Each answered turn records its *commitments*: the verified claims of a factual answer, or
the final answer of a verified reasoning answer. Later turns are checked against them
(factual.py, reasoning.py), and the consistency tier answers follow-ups straight from them
- re-validated, not regenerated. Any number it derives must come back as an expression that
passes the arithmetic check, and every name and number must come from the statements it cites.
A follow-up they do not cover is rewritten as a standalone question and handed to the factual
or reasoning tier.
"""
import json

from .. import llm
from ..db import connect
from . import compression, factual, guardrails
from .router import looks_computational
from .trace import Trace

HISTORY_TURNS = 6

CONSISTENCY_SYSTEM = """The user asks a follow-up in a conversation. You get the conversation so far and numbered statements that earlier answers already VERIFIED.
If the verified statements alone fully answer the follow-up, answer from them only and list the numbers you used.
If answering needs any arithmetic, list each calculation with the pure arithmetic in "expression" (digits, + - * / ** % and parentheses only) and its value in "result".
Otherwise set answerable to false and rewrite the follow-up as a standalone question (resolve pronouns and references from the conversation, include any numbers it refers to), and say whether it needs facts ("factual") or calculation/logic ("reasoning").
Return only JSON: {"answerable": true, "answer": "...", "used": [1], "calculations": [{"expression": "", "result": ""}], "standalone": "...", "standalone_type": "factual"}"""


def history(conversation_id: str | None, limit: int = HISTORY_TURNS) -> list[dict]:
    if not conversation_id:
        return []
    with connect() as conn:
        rows = conn.execute(
            "SELECT question, answer, tier, commitments FROM turns WHERE conversation_id=? ORDER BY id DESC LIMIT ?",
            (conversation_id, limit),
        ).fetchall()
    return [{**dict(r), "commitments": json.loads(r["commitments"])} for r in reversed(rows)]


def history_text(hist: list[dict], question: str = "") -> str:
    """The conversation for prompts. Compressed: the last few turns with short answers (the verified statements
    themselves travel separately), unless the question mentions something only the cut part contains - then
    the full history is used (compression.compact_history)."""
    shown, size, _ = compression.compact_history(hist, question)
    return "\n".join(f"Q{i}: {t['question']}\nA{i}: {t['answer'][:size]}" for i, t in enumerate(shown, 1))


def commitments_of(question: str, result: dict) -> list[dict]:
    if result["tier"] in ("factual", "consistency"):
        return [{"kind": "claim", **{k: c[k] for k in ("subject", "relation", "object", "source")}}
                for c in result["path"] if c.get("kind", "claim") == "claim" and c.get("status", "accepted") == "accepted"] \
            + [c for c in result["path"] if c.get("kind") == "answer"]
    if result["tier"] == "reasoning" and result["confidence"] != "low" and result.get("final_answer"):
        premises = [{"kind": "claim", **{k: p[k] for k in ("subject", "relation", "object", "source")}}
                    for p in result.get("premises") or []]
        return premises + [{"kind": "answer", "question": question, "final_answer": result["final_answer"]}]
    return []


def claims(hist: list[dict]) -> list[dict]:
    return [c for t in hist for c in t["commitments"] if c["kind"] == "claim"]


def answers(hist: list[dict]) -> list[dict]:
    return [c for t in hist for c in t["commitments"] if c["kind"] == "answer"]


def _fmt(c: dict) -> str:
    if c["kind"] == "claim":
        return f"{c['subject']} {c['relation'].replace('_', ' ')} {c['object']}"
    return f"the answer to “{c['question']}” is {c['final_answer']}"


def _calculations(d: dict) -> list[dict]:
    out = []
    for k in d.get("calculations") or []:
        if isinstance(k, dict) and str(k.get("expression") or "").strip():
            out.append({"expression": str(k["expression"]).strip(), "result": str(k.get("result") or "").strip()})
    return out


def answer(store, question: str, hist: list[dict], trace: Trace):
    """Generator: yields trace events; returns a tier result, or {"delegate": {...}} when the
    conversation's verified statements do not cover the follow-up (or its answer fails the checks)."""
    ev = trace.start("consistency", "Checking what this conversation already established")
    yield ev
    commits = [c for t in hist for c in t["commitments"]]
    numbered = "\n".join(f"{i}. {_fmt(c)}" for i, c in enumerate(commits, 1)) or "(none)"
    d = llm.chat_json(CONSISTENCY_SYSTEM, f"Conversation so far:\n{history_text(hist, question)}\n\n"
                                          f"Verified statements:\n{numbered}\n\nFollow-up: {question}")
    d = d if isinstance(d, dict) else {}
    used = []
    for i in d.get("used") or []:
        if isinstance(i, int) and 1 <= i <= len(commits) and commits[i - 1] not in used:
            used.append(commits[i - 1])
    text = str(d.get("answer") or "").strip()
    calcs = _calculations(d)

    if d.get("answerable") is True and used and text:
        stale = factual.still_holds(store, [c for c in used if c["kind"] == "claim"])
        problems = guardrails.consistency_problems(question, text, used, calcs)
        if not stale and not problems:
            checked = f", {len(calcs)} calculation(s) machine-checked" if calcs else ""
            yield trace.settle(ev, "ok", f"Answered from {len(used)} statement(s) verified earlier in this conversation"
                                         f"{checked} - re-checked, nothing regenerated.")
            return {"tier": "consistency", "answer": text, "confidence": "high", "path": used, "calculations": calcs,
                    "caught": [], "withheld": 0, "cacheable": False, "stopped": None}
        why = stale or "; ".join(problems[:2])
        yield trace.settle(ev, "warn", f"Could not answer from memory alone ({why}). Re-deriving instead.")
    else:
        yield trace.settle(ev, "info", "Earlier answers do not cover this on their own.")

    standalone = str(d.get("standalone") or "").strip() or question
    kind = d.get("standalone_type")
    if kind not in ("factual", "reasoning"):
        kind = "reasoning" if looks_computational(standalone) else "factual"
    if calcs and kind == "factual":
        kind = "reasoning"  # a calculation is never left to unchecked prose
    yield trace.note("consistency", "Re-asking as a standalone question", f"“{standalone}” → {kind} tier")
    return {"delegate": {"question": standalone, "type": kind}}

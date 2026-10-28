"""Intermediate compression: verified reasoning states reused across *similar* questions.

The exact-repeat cache (cache.py) only helps when the same question comes back. A state
here is the symbolic core of a verified answer - its accepted triples, or its
machine-checked steps - stored zlib-compressed. A later question that is similar but not
identical gets it injected as prior verified reasoning to adapt instead of rebuilding,
and triples it already verified skip their model checks (claim cache).

Similarity is lexical (content words, numbers ignored so "12 boxes of 7" matches
"15 boxes of 6"), which needs no model download and is cheap enough to scan.
"""
import json
import re
import time
import zlib

from ..db import connect

SIMILAR = 0.5
MAX_SCAN = 500

_STOP = set("""a an the of in on at to for and or is are was were be been being by with from as that this these
those it its what which who whom whose how why when where does do did can could would should will shall about into
than then there their they he she his her him them i you me my your we our us has have had not no if so such
many much each per tell give""".split())
_WORD = re.compile(r"[a-z][a-z']+")


def terms(text: str) -> set[str]:
    out = set()
    for w in _WORD.findall(text.lower()):
        w = w.strip("'")
        if len(w) > 2 and w not in _STOP:
            out.add(w[:-1] if len(w) > 4 and w.endswith("s") else w)
    return out


def similarity(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def save(tier: str, question: str, state: dict, tokens: int) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO reasoning_states(tier, question, terms, state, tokens, created_at) VALUES (?,?,?,?,?,?)",
            (tier, question, " ".join(sorted(terms(question))), zlib.compress(json.dumps(state).encode()),
             tokens, time.time()),
        )


def find(tier: str, question: str) -> dict | None:
    q = terms(question)
    if not q:
        return None
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, question, terms, tokens FROM reasoning_states WHERE tier=? ORDER BY id DESC LIMIT ?",
            (tier, MAX_SCAN),
        ).fetchall()
        best, best_sim = None, SIMILAR - 1e-9
        for r in rows:  # newest first, so the newest wins a tie
            sim = similarity(q, set(r["terms"].split()))
            if sim > best_sim:
                best, best_sim = r, sim
        if best is None:
            return None
        conn.execute("UPDATE reasoning_states SET hits = hits + 1 WHERE id=?", (best["id"],))
        blob = conn.execute("SELECT state FROM reasoning_states WHERE id=?", (best["id"],)).fetchone()["state"]
    return {"id": best["id"], "question": best["question"], "similarity": round(best_sim, 2),
            "tokens": best["tokens"], "state": json.loads(zlib.decompress(blob))}


def state_of(result: dict) -> dict | None:
    """The symbolic, reusable core of a tier result, or None if nothing in it is verified."""
    if result["tier"] == "factual":
        triples = [{k: c[k] for k in ("subject", "relation", "object", "source")}
                   for c in result["path"] if c["status"] == "accepted"]
        return {"triples": triples} if triples else None
    if result["tier"] == "reasoning" and result["confidence"] == "high":
        return {"steps": result["path"], "final_answer": result.get("final_answer", "")}
    return None

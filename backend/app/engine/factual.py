"""Factual tier: answer as a path of atomic claims and walk it node by node.

A node is accepted only after it is checked, and the walk moves on only from
accepted nodes:
  1. graph     - the knowledge graph supports it (directly or by transitivity),
                 or contradicts it and holds the single correct value (corrected)
  2. cache     - it was model-verified before
  3. model     - not in the graph: an independent true/false judgment AND a blind
                 probe (asked without seeing the claimed object) must agree
Anything else is rejected or left unverified and kept out of the answer.
Nodes that depend on a rejected node are skipped.
"""
import difflib
import re
import time

from .. import llm, state_cache
from ..db import connect
from .trace import Trace

MAX_CLAIMS = 12

DRAFT_SYSTEM = """Answer the question as a chain of atomic facts - a path through a knowledge graph.
Each node is one subject-relation-object triple. Order nodes so each builds on earlier ones; "depends_on" lists the
0-based indexes of earlier nodes it relies on (e.g. a character's role depends on the node saying the character is in the book).
Rules:
- Prefer the KNOWN FACTS. When you use one, copy its subject, relation and object exactly.
- Outside KNOWN FACTS, only state facts you are highly confident are true. Fewer true facts beat more doubtful ones.
- subject and object are short entity names; relation is snake_case (written_by, born_in, character_in, won, ...). Reuse listed relations.
- No opinions or speculation. At most 12 nodes.
Return only JSON: {"claims": [{"subject": "...", "relation": "...", "object": "...", "depends_on": []}]}"""

JUDGE_SYSTEM = """Judge whether ONE claim is true in the real world. Someone else wrote it; do not assume it is right.
Return only JSON: {"verdict": "true" | "false" | "unsure", "confidence": 0.0}"""

PROBE_SYSTEM = """Answer from your own knowledge. Give every correct value for the missing object, or none if you do not know.
Return only JSON: {"answers": ["..."]}"""

COMPOSE_SYSTEM = """Write the answer to the question using ONLY the verified facts listed.
Do not add any other fact, name, date, number or detail, even if you believe it is true.
If the verified facts do not fully answer the question, say briefly what could not be verified.
Plain prose, concise."""


def _norm(s: str) -> str:
    s = re.sub(r"[^a-z0-9 ]+", " ", s.lower())
    s = re.sub(r"\b(the|a|an)\b", " ", s)
    return " ".join(s.split())


def same_entity(a: str, b: str) -> bool:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    if a == b or (min(len(a), len(b)) >= 4 and (a in b or b in a)):
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.85


def _fmt(c: dict) -> str:
    return f"({c['subject']}) -[{c['relation']}]-> ({c['object']})"


def claim_key(c: dict) -> str:
    return state_cache.make_key(f"claim:{c['subject']}|{c['relation']}|{c['object']}")


def _queue_for_review(c: dict) -> None:
    """Model-verified facts are never written to the graph directly; a human accepts them via /review."""
    with connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM review_queue WHERE subject=? AND relation=? AND object=? AND status='pending'",
            (c["subject"], c["relation"], c["object"]),
        ).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO review_queue(subject, relation, object, source, reason, created_at) VALUES (?,?,?,?,?,?)",
                (c["subject"], c["relation"], c["object"], "chat", "model-verified, not in graph", time.time()),
            )


def _parse_claims(data) -> list[dict]:
    raw = data.get("claims") if isinstance(data, dict) else data
    out = []
    for i, c in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(c, dict):
            continue
        s, r, o = (str(c.get(k, "")).strip() for k in ("subject", "relation", "object"))
        rel = llm.normalize_relation(r)
        if not (s and o and rel):
            continue
        deps = [d for d in c.get("depends_on") or [] if isinstance(d, int) and 0 <= d < i]
        out.append({"subject": s.lower(), "relation": rel, "object": o.lower(), "depends_on": deps, "index": i})
        if len(out) >= MAX_CLAIMS:
            break
    # depends_on refers to the model's indexes; remap to positions in `out`
    pos = {c["index"]: n for n, c in enumerate(out)}
    for c in out:
        c["depends_on"] = [pos[d] for d in c["depends_on"] if d in pos]
        del c["index"]
    return out


def _stored_values(store, subject: str, relation: str) -> list[str]:
    return sorted({f["object"] for f in store.facts_about(subject) if f["subject"] == subject and f["relation"] == relation})


def _model_check(c: dict) -> dict:
    judge = llm.chat_json(JUDGE_SYSTEM, f"Claim: {c['subject']} {c['relation'].replace('_', ' ')} {c['object']}")
    verdict = str(judge.get("verdict", "unsure")).lower() if isinstance(judge, dict) else "unsure"
    try:
        conf = float(judge.get("confidence", 0)) if isinstance(judge, dict) else 0.0
    except (TypeError, ValueError):
        conf = 0.0
    probe = llm.chat_json(PROBE_SYSTEM, f"subject: {c['subject']}\nrelation: {c['relation']}\nobject: ?")
    answers = [str(a) for a in (probe.get("answers") or [])] if isinstance(probe, dict) else []
    matched = any(same_entity(a, c["object"]) for a in answers)
    return {"verdict": verdict, "confidence": conf, "probe_answers": answers[:5], "probe_matched": matched}


def verify_node(store, c: dict, trace: Trace, label: str):
    """Generator: yields trace events, returns the claim with status/source/reason set."""
    ev = trace.start("factual", f"{label}: {_fmt(c)}", "checking against the knowledge graph")
    yield ev

    try:
        v = store.validate(c["subject"], c["relation"], c["object"])
    except ValueError as e:  # e.g. relation name the graph backend refuses
        v = {"verdict": "unknown", "reason": str(e)}

    if v["verdict"] == "supported":
        how = " via " + " -> ".join(v["path"]) if v.get("path") else ""
        yield trace.settle(ev, "ok", f"Graph supports it ({v['reason']}{how}). Moving to the next node.")
        return {**c, "status": "accepted", "source": "graph", "reason": v["reason"]}

    if v["verdict"] == "contradicted":
        stored = _stored_values(store, c["subject"], c["relation"])
        if len(stored) == 1:
            fixed = {**c, "object": stored[0]}
            yield trace.settle(ev, "warn", f"Graph contradicts it: {v['reason']}. Corrected to {_fmt(fixed)}.")
            return {**fixed, "status": "accepted", "source": "graph-corrected", "reason": v["reason"],
                    "original_object": c["object"]}
        yield trace.settle(ev, "fail", f"Graph contradicts it: {v['reason']}. Dropped.")
        return {**c, "status": "rejected", "source": "graph", "reason": v["reason"]}

    cached = state_cache.get(claim_key(c))
    if cached and cached.get("status") == "accepted":
        yield trace.settle(ev, "ok", "Not in the graph, but this exact node was verified before (cache). Moving on.")
        return {**c, "status": "accepted", "source": "cache", "reason": "previously model-verified"}

    yield trace.settle(ev, "running", "Not in the graph. Running an independent check and a blind probe...")
    m = _model_check(c)
    probe = ", ".join(m["probe_answers"]) or "no answer"
    if m["verdict"] == "false" and m["confidence"] >= 0.5:
        yield trace.settle(ev, "fail", f"Independent check says false ({m['confidence']:.2f}). Dropped.")
        return {**c, "status": "rejected", "source": "model", "reason": "independent check: false"}
    if m["verdict"] == "true" and m["confidence"] >= 0.7 and m["probe_matched"]:
        accepted = {**c, "status": "accepted", "source": "model", "reason": "independent check and blind probe agree"}
        state_cache.put(claim_key(c), {"status": "accepted", "checked_at": time.time()})
        _queue_for_review(c)
        yield trace.settle(ev, "ok", f"Not in graph; check says true ({m['confidence']:.2f}) and blind probe "
                                     f"independently gave '{probe}'. Accepted and queued for graph review.")
        return accepted
    yield trace.settle(ev, "warn", f"Could not confirm (check: {m['verdict']} {m['confidence']:.2f}; blind probe: "
                                   f"{probe}). Kept out of the answer.")
    return {**c, "status": "unverified", "source": "model", "reason": "checks disagree"}


def answer(store, question: str, entities: list[str], trace: Trace):
    """Generator: yields trace events, returns the tier result."""
    ev = trace.start("graph", "Looking up known facts in the knowledge graph",
                     f"entities: {', '.join(entities) or '(none detected)'}")
    yield ev
    known, seen = [], set()
    for e in entities:
        for f in store.facts_about(e):
            k = (f["subject"], f["relation"], f["object"])
            if k not in seen:
                seen.add(k)
                known.append(f)
    yield trace.settle(ev, "ok" if known else "warn",
                       f"{len(known)} stored fact(s) found" if known else "No stored facts; every node will need independent checks")

    ev = trace.start("factual", "Drafting the answer as a path of claims")
    yield ev
    relations = sorted({f["relation"] for f in known} | {r["relation"] for r in getattr(store, "relation_stats", list)()})
    known_txt = "\n".join(f"- {f['subject']} | {f['relation']} | {f['object']}" for f in known[:80]) or "(none)"
    claims = _parse_claims(llm.chat_json(
        DRAFT_SYSTEM, f"KNOWN FACTS:\n{known_txt}\n\nRelations in use: {relations}\n\nQuestion: {question}"
    ))
    yield trace.settle(ev, "ok" if claims else "fail", f"{len(claims)} node(s) to verify, in order")

    checked: list[dict] = []
    for i, c in enumerate(claims):
        label = f"Node {i + 1}/{len(claims)}"
        broken = [d for d in c["depends_on"] if checked[d]["status"] != "accepted"]
        if broken:
            yield trace.note("factual", f"{label}: {_fmt(c)}",
                             f"Skipped: depends on node {broken[0] + 1}, which did not pass.", "fail")
            checked.append({**c, "status": "skipped", "source": None, "reason": f"depends on node {broken[0] + 1}"})
            continue
        checked.append((yield from verify_node(store, c, trace, label)))

    accepted = [c for c in checked if c["status"] == "accepted"]
    missing = [c for c in checked if c["status"] != "accepted"]

    ev = trace.start("compose", "Writing the answer from verified nodes only",
                     f"{len(accepted)} verified, {len(missing)} left out")
    yield ev
    if accepted:
        facts = "\n".join(f"- {c['subject']} {c['relation'].replace('_', ' ')} {c['object']}" for c in accepted)
        gaps = "\n".join(f"- {c['subject']} {c['relation'].replace('_', ' ')} ?" for c in missing) or "(none)"
        text = llm.chat_text(COMPOSE_SYSTEM, f"Question: {question}\n\nVerified facts:\n{facts}\n\nCould not verify:\n{gaps}")
        yield trace.settle(ev, "ok")
    else:
        text = "I couldn't verify any facts to answer this, so I'd rather not guess."
        yield trace.settle(ev, "warn", "Nothing verified - declining to answer")

    graph_share = sum(c["source"] in ("graph", "graph-corrected") for c in accepted) / max(len(accepted), 1)
    confidence = "high" if accepted and not missing and graph_share >= 0.5 else "medium" if accepted else "low"
    return {
        "tier": "factual",
        "answer": text.strip(),
        "confidence": confidence,
        "path": checked,
        "cacheable": bool(accepted) and not missing,
    }

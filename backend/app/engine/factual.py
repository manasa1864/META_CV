"""Factual tier: answer as a path of atomic claims and walk it node by node.

A node is accepted only after it is checked, and the walk moves on only from
accepted nodes:
  1. graph     - the knowledge graph supports it (directly or by transitivity),
                 or contradicts it and holds the single correct value (corrected)
  2. cache     - it was model-verified before
  3. model     - not in the graph: an independent true/false judgment AND a blind
                 probe (asked without seeing the claimed object) must agree
An accepted node must also not clash with an earlier node of the answer or with what
the conversation already established: same subject and a single-valued relation with a
different object, or objects the graph marks incompatible_with / contradicts each other.
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
If corrections to earlier answers are listed, point them out.
Plain prose, concise."""

EXCLUSIVE_RELATIONS = ("incompatible_with", "contradicts")


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


def still_holds(store, claims: list[dict]) -> str | None:
    """None if every accepted claim still checks out; otherwise why not."""
    for c in claims:
        if c.get("status", "accepted") != "accepted":
            continue
        if c.get("source") in ("graph", "graph-corrected"):
            if store.validate(c["subject"], c["relation"], c["object"])["verdict"] != "supported":
                return f"graph no longer supports {c['subject']} {c['relation']} {c['object']}"
        elif c.get("source") in ("model", "cache") and state_cache.get(claim_key(c)) is None:
            return f"verification of {c['subject']} {c['relation']} {c['object']} expired"
    return None


def _graph_backed(c: dict) -> bool:
    return c.get("source") in ("graph", "graph-corrected")


def _exclusive(store, a: str, b: str) -> bool:
    return any(f["relation"] in EXCLUSIVE_RELATIONS and {f["subject"], f["object"]} == {a, b}
               for f in store.facts_about(a))


def _same_value(a: str, b: str) -> bool:
    """Stricter than same_entity: near-spellings can be opposites (utopian vs dystopian)."""
    a, b = _norm(a), _norm(b)
    return a == b or (min(len(a), len(b)) >= 4 and (a in b or b in a))


def _clash(store, c: dict, other: dict) -> str | None:
    if not same_entity(c["subject"], other["subject"]) or _same_value(c["object"], other["object"]):
        return None
    if c["relation"] == other["relation"] and store.is_functional(c["relation"]):
        return f"{c['relation']} is single-valued"
    if _exclusive(store, c["object"], other["object"]):
        return f"the graph marks {c['object']} and {other['object']} as incompatible"
    return None


def _check_conflicts(store, node: dict, checked: list[dict], earlier: list[dict], trace: Trace, label: str,
                     caught: list[dict]):
    """Generator: an accepted node that clashes with an earlier node or an earlier turn is dropped,
    unless the graph backs it and the other side does not."""
    pool = [(e, "consistency", "established earlier in this conversation") for e in earlier]
    pool += [(x, "reasoning", f"node {i + 1} of this answer") for i, x in enumerate(checked) if x["status"] == "accepted"]
    for other, kind, where in pool:
        why = _clash(store, node, other)
        if not why:
            continue
        clash = f"{_fmt(node)} clashes with {_fmt(other)}, {where} ({why})"
        stage = "consistency" if kind == "consistency" else "factual"
        if _graph_backed(node) and not _graph_backed(other):
            caught.append({"type": kind, "action": "corrected", "detail": f"{clash}; the graph backs the new node"})
            yield trace.note(stage, f"{label}: conflict", f"{clash}. Kept - the graph backs this node.", "warn")
            return {**node, "corrects": other}
        caught.append({"type": kind, "action": "dropped", "detail": clash})
        yield trace.note(stage, f"{label}: conflict", f"{clash}. Dropped.", "fail")
        return {**node, "status": "rejected", "source": kind, "reason": clash}
    return node


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


def _caught_by_checks(checked: list[dict]) -> list[dict]:
    out = []
    for c in checked:
        if c["source"] == "graph-corrected":
            out.append({"type": "factual", "action": "corrected",
                        "detail": f"{c['subject']} {c['relation']} {c['original_object']} -> {c['object']} ({c['reason']})"})
        elif c["status"] == "rejected" and c["source"] in ("graph", "model"):
            out.append({"type": "factual", "action": "dropped", "detail": f"{_fmt(c)}: {c['reason']}"})
    return out


def answer(store, question: str, entities: list[str], trace: Trace, prior: dict | None = None,
           earlier: list[dict] | None = None):
    """Generator: yields trace events, returns the tier result.

    prior: a similar question's verified state (states.find); earlier: claims this conversation established.
    """
    earlier = earlier or []
    ev = trace.start("graph", "Looking up known facts in the knowledge graph",
                     f"entities: {', '.join(entities) or '(none detected)'}")
    yield ev
    known, seen = [], set()

    def add(f):
        k = (f["subject"], f["relation"], f["object"])
        if k not in seen:
            seen.add(k)
            known.append(f)

    for e in entities:
        for f in store.facts_about(e):
            add(f)
    yield trace.settle(ev, "ok" if known else "warn",
                       f"{len(known)} stored fact(s) found" if known else "No stored facts; every node will need independent checks")
    n_graph = len(known)
    for f in (prior or {}).get("state", {}).get("triples", []) + earlier:
        add(f)
    if len(known) > n_graph:
        yield trace.note("states", "Reusing verified reasoning",
                         f"{len(known) - n_graph} verified fact(s) from "
                         + (f"a similar question (“{prior['question']}”)" if prior else "")
                         + (" and " if prior and earlier else "") + ("earlier in this conversation" if earlier else "")
                         + " - already-verified nodes skip their checks.", "ok")

    ev = trace.start("factual", "Drafting the answer as a path of claims")
    yield ev
    relations = sorted({f["relation"] for f in known} | {r["relation"] for r in getattr(store, "relation_stats", list)()})
    known_txt = "\n".join(f"- {f['subject']} | {f['relation']} | {f['object']}" for f in known[:80]) or "(none)"
    claims = _parse_claims(llm.chat_json(
        DRAFT_SYSTEM, f"KNOWN FACTS:\n{known_txt}\n\nRelations in use: {relations}\n\nQuestion: {question}"
    ))
    yield trace.settle(ev, "ok" if claims else "fail", f"{len(claims)} node(s) to verify, in order")

    checked: list[dict] = []
    caught: list[dict] = []
    for i, c in enumerate(claims):
        label = f"Node {i + 1}/{len(claims)}"
        broken = [d for d in c["depends_on"] if checked[d]["status"] != "accepted"]
        if broken:
            yield trace.note("factual", f"{label}: {_fmt(c)}",
                             f"Skipped: depends on node {broken[0] + 1}, which did not pass.", "fail")
            checked.append({**c, "status": "skipped", "source": None, "reason": f"depends on node {broken[0] + 1}"})
            continue
        node = yield from verify_node(store, c, trace, label)
        if node["status"] == "accepted":
            node = yield from _check_conflicts(store, node, checked, earlier, trace, label, caught)
        checked.append(node)
    caught = _caught_by_checks(checked) + caught

    accepted = [c for c in checked if c["status"] == "accepted"]
    missing = [c for c in checked if c["status"] != "accepted"]

    ev = trace.start("compose", "Writing the answer from verified nodes only",
                     f"{len(accepted)} verified, {len(missing)} left out")
    yield ev
    if accepted:
        facts = "\n".join(f"- {c['subject']} {c['relation'].replace('_', ' ')} {c['object']}" for c in accepted)
        gaps = "\n".join(f"- {c['subject']} {c['relation'].replace('_', ' ')} ?" for c in missing) or "(none)"
        fixes = "\n".join(f"- earlier: {_fmt(c['corrects'])}; now verified: {_fmt(c)}" for c in accepted if c.get("corrects"))
        text = llm.chat_text(COMPOSE_SYSTEM, f"Question: {question}\n\nVerified facts:\n{facts}\n\nCould not verify:\n{gaps}"
                                             + (f"\n\nCorrections to earlier answers:\n{fixes}" if fixes else ""))
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
        "caught": caught,
        "withheld": sum(c["status"] == "unverified" for c in checked),
        "cacheable": bool(accepted) and not missing,
    }

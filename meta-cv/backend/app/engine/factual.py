"""Factual tier: answer as a path of atomic claims and walk it node by node.

Graph first (decompose.py): the question is split into the parts it asks for, and every part the graph
can answer is answered from the graph alone - every stored value, nothing left to the model. Only the
remaining parts are drafted, and a part that still has no verified answer is reported as a gap.

A node is accepted only after it is checked, and the walk moves on only from
accepted nodes:
  1. graph     - the knowledge graph supports it (directly or by transitivity),
                 or contradicts it and holds the single correct value (corrected)
     types     - not in the graph, and its subject or object is the wrong kind of thing for the
                 relation (entity_types.py): dropped at once, with no model call
  2. cache     - it was model-verified recently (claims expire after CLAIM_TTL_HOURS)
  3. model     - not in the graph: an independent true/false judgment AND a blind
                 probe (asked without seeing the claimed object) must agree.
                 Skipped in verified-only mode: then only the graph counts.
An accepted node must also not clash with an earlier node of the answer or with what
the conversation already established: same subject and a single-valued relation with a
different object, or objects the graph marks incompatible_with / contradicts each other.
Anything else is rejected or left unverified and kept out of the answer.
Nodes that depend on a rejected node are skipped. Accepted facts then pass the freshness review
(temporal.py): a validity period travels with its fact, and for a question about the present an ended fact
is dropped and an old or model-only one is kept with a date note. The composed prose then passes the
faithfulness guard (guardrails.py) before it is returned.
"""
import difflib
import re
import time

from .. import config, llm, state_cache
from ..db import connect
from ..graph_common import EXCLUSIVE_RELATIONS
from . import compression, decompose, entity_types, guardrails, temporal
from .router import caught
from .trace import Trace

MAX_CLAIMS = 12

DRAFT_SYSTEM = """Answer the question as a chain of atomic facts - a path through a knowledge graph.
Each node is one subject-relation-object triple. Order nodes so each builds on earlier ones; "depends_on" lists the
0-based indexes of earlier nodes it relies on (e.g. a character's role depends on the node saying the character is in the book).
Rules:
- Prefer the KNOWN FACTS. When you use one, copy its subject, relation and object exactly.
- Outside KNOWN FACTS, only state facts you are highly confident are true. Fewer true facts beat more doubtful ones.
- If the question assumes something false, state the true facts instead of the assumed one.
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
A fact may carry a time note in brackets: keep it with the fact ("as of ...", "since ...", "until ..."), and
never present a fact as current beyond what its note says. Facts listed as no longer current must not be
presented as current; you may say they ended.
Plain prose, concise."""

COMPOSE_STRICT = COMPOSE_SYSTEM + """
Your previous answer added details that are not in the verified facts. Use only names, numbers and
dates that appear in the verified facts or the question. Prefer one short sentence per verified fact."""


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


def same_value(a: str, b: str) -> bool:
    """Stricter than same_entity: near-spellings can be opposites (utopian vs dystopian)."""
    a, b = _norm(a), _norm(b)
    return a == b or (min(len(a), len(b)) >= 4 and (a in b or b in a))


def _fmt(c: dict) -> str:
    return f"({c['subject']}) -[{c['relation']}]-> ({c['object']})"


def triple(c: dict) -> str:
    return f"{c['subject']}|{c['relation']}|{c['object']}"


def claim_key(c: dict) -> str:
    return state_cache.make_key(f"claim:{triple(c)}")


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


def _clash(store, c: dict, other: dict) -> str | None:
    if not same_entity(c["subject"], other["subject"]) or same_value(c["object"], other["object"]):
        return None
    if c["relation"] == other["relation"] and store.is_functional(c["relation"]):
        return f"{c['relation']} is single-valued"
    if _exclusive(store, c["object"], other["object"]):
        return f"the graph marks {c['object']} and {other['object']} as incompatible"
    return None


def _check_conflicts(store, node: dict, checked: list[dict], earlier: list[dict], trace: Trace, label: str,
                     found: list[dict], answered: list[dict] = ()):
    """Generator: an accepted node that clashes with an earlier node, an earlier turn or a part answered
    from the graph is dropped, unless the graph backs it and the other side does not."""
    pool = [(e, "faithful.context", "established earlier in this conversation") for e in earlier]
    pool += [(a, "factual.relation_error", f"answered from the graph (part {a['part'] + 1})") for a in answered]
    pool += [(x, "factual.relation_error", f"node {i + 1} of this answer") for i, x in enumerate(checked)
             if x["status"] == "accepted"]
    for other, risk, where in pool:
        why = _clash(store, node, other)
        if not why:
            continue
        clash = f"{_fmt(node)} clashes with {_fmt(other)}, {where} ({why})"
        stage = "consistency" if risk == "faithful.context" else "factual"
        if _graph_backed(node) and not _graph_backed(other):
            found.append(caught(risk, "factual", "corrected", f"{clash}; the graph backs the new node"))
            yield trace.note(stage, f"{label}: conflict", f"{clash}. Kept - the graph backs this node.", "warn")
            return {**node, "corrects": other}
        found.append(caught(risk, "factual", "dropped", clash))
        yield trace.note(stage, f"{label}: conflict", f"{clash}. Dropped.", "fail")
        return {**node, "status": "rejected", "source": "consistency" if risk == "faithful.context" else "conflict",
                "reason": clash}
    return node


def _stored_values(store, subject: str, relation: str) -> list[str]:
    s = store.resolve(subject) if hasattr(store, "resolve") else subject
    return sorted({f["object"] for f in store.facts_about(subject) if f["subject"] == s and f["relation"] == relation})


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


def verify_node(store, c: dict, trace: Trace, label: str, verified_only: bool = False):
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

    wrong_type = entity_types.check(store, c["subject"], c["relation"], c["object"])
    if wrong_type:
        yield trace.settle(ev, "fail", f"Type check: {wrong_type}. Dropped without a model call.")
        return {**c, "status": "rejected", "source": "types", "reason": wrong_type}

    if verified_only:
        yield trace.settle(ev, "warn", "Not in the graph, and verified-only mode accepts graph facts only. Kept out.")
        return {**c, "status": "unverified", "source": "graph", "reason": "verified-only mode"}

    cached = state_cache.get(claim_key(c))
    if cached and cached.get("status") == "accepted":
        yield trace.settle(ev, "ok", "Not in the graph, but this exact node was verified recently (cache). Moving on.")
        return {**c, "status": "accepted", "source": "cache", "reason": "previously model-verified"}

    yield trace.settle(ev, "running", "Not in the graph. Running an independent check and a blind probe...")
    m = _model_check(c)
    probe = ", ".join(m["probe_answers"]) or "no answer"
    if m["verdict"] == "false" and m["confidence"] >= 0.5:
        yield trace.settle(ev, "fail", f"Independent check says false ({m['confidence']:.2f}). Dropped.")
        return {**c, "status": "rejected", "source": "model", "reason": "independent check: false"}
    if m["verdict"] == "true" and m["confidence"] >= 0.7 and m["probe_matched"]:
        accepted = {**c, "status": "accepted", "source": "model", "reason": "independent check and blind probe agree"}
        state_cache.put(claim_key(c), {"status": "accepted", "checked_at": time.time()},
                        ttl=config.CLAIM_TTL_HOURS * 3600, triples=[triple(c)])
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
            out.append(caught("factual.entity_error", "factual", "corrected",
                              f"{c['subject']} {c['relation']} {c['original_object']} -> {c['object']} ({c['reason']})"))
        elif c["status"] == "rejected" and c["source"] in ("graph", "model"):
            out.append(caught("factual.entity_error", "factual", "dropped", f"{_fmt(c)}: {c['reason']}"))
        elif c["status"] == "rejected" and c["source"] == "types":
            out.append(caught("factual.relation_error", "factual", "dropped", f"{_fmt(c)}: {c['reason']}"))
    return out


def walk(store, question: str, entities: list[str], trace: Trace, prior: dict | None = None,
         earlier: list[dict] | None = None, verified_only: bool = False, time_sensitive: bool = False):
    """Generator: draft the claim path and verify it node by node. Returns (checked, found)."""
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

    parts, answered, open_parts = [], [], []
    if decompose.enabled():
        parts = yield from decompose.plan(store, question, entities, trace)
        if parts:
            answered, open_parts = yield from decompose.fill(store, parts, trace)
    if parts and not open_parts:
        yield trace.note("graph", "Every part answered from the graph",
                         f"{len(answered)} stored fact(s) answer all {len(parts)} part(s) - nothing was left to the model, "
                         "so there is no draft to check.", "ok")
        return (yield from _finish(store, parts, answered, [], [], time_sensitive, trace))
    focus = decompose.focus_text(parts, open_parts, answered) if parts else None

    all_relations = store.relation_names()
    trim = compression.enabled()
    if trim:
        shown, cut = compression.split_facts(known, question, entities, must_keep=earlier)
        relations, cut_relations = compression.split_relations(all_relations, shown, question)
        compression.record(facts_kept=len(shown), facts_cut=len(cut), relations_kept=len(relations),
                           relations_cut=len(cut_relations))
    else:
        shown, cut, relations, cut_relations = known[:80], [], all_relations, []

    checked, found = yield from _draft_and_check(store, question, shown, relations, earlier, trace, verified_only,
                                                 answered, focus)
    lost = compression.context_lost(checked, cut, cut_relations) if trim else None
    if lost:
        yield trace.note("compression", "Context check: trimming may have hidden something",
                         f"{lost}. Redrafting with every known fact and relation.", "warn")
        compression.record(redrafted=True, redraft_reason=lost)
        checked, found = yield from _draft_and_check(store, question, known[:80], all_relations, earlier, trace,
                                                     verified_only, answered, focus)
    elif trim and (cut or cut_relations):
        yield trace.note("compression", "Context check passed",
                         f"{len(shown)} of {len(known)} known facts and {len(relations)} of {len(all_relations)} relation "
                         "names were shown; every claim that relies on the cut ones was verified against the full graph.",
                         "ok")

    return (yield from _finish(store, parts, answered, checked, found, time_sensitive, trace))


def _finish(store, parts: list[dict], answered: list[dict], drafted: list[dict], found: list[dict],
            time_sensitive: bool, trace: Trace):
    """Generator: merge graph answers with the verified draft, review them for currency, then report every part
    left without a verified answer. Returns (checked, found)."""
    merged, seen = list(answered), {triple(a) for a in answered}
    for c in drafted:  # a drafted claim the graph corrected can land on a graph answer: keep each fact once
        if c.get("dup") or (c["status"] == "accepted" and triple(c) in seen):
            continue
        if c["status"] == "accepted":
            seen.add(triple(c))
        merged.append(c)
    checked, stale = yield from temporal.review(store, merged, time_sensitive, trace)
    found = found + stale
    if parts:
        missing_parts = decompose.gaps(parts, checked)
        if missing_parts:
            yield trace.note("graph", "Coverage check: some parts have no verified answer",
                             f"{len(parts) - len(missing_parts)} of {len(parts)} part(s) answered; not answered: "
                             + "; ".join(g["part_text"] for g in missing_parts) + ". The answer will say so.", "warn")
        else:
            yield trace.note("graph", "Coverage check passed",
                             f"Every one of the {len(parts)} part(s) has a verified answer.", "ok")
        checked += missing_parts
    return checked, _caught_by_checks(answered) + found


def _draft_and_check(store, question: str, shown: list[dict], relations: list[str], earlier: list[dict],
                     trace: Trace, verified_only: bool, answered: list[dict] = (), focus: str | None = None):
    """Generator: draft the claim path from the shown context and verify it node by node against the full graph.

    answered / focus: parts the graph already answered, and the parts still needed (graph-first)."""
    ev = trace.start("factual", "Drafting the parts the graph could not answer" if focus
                     else "Drafting the answer as a path of claims")
    yield ev
    known_txt = "\n".join(f"- {f['subject']} | {f['relation']} | {f['object']}" for f in shown) or "(none)"
    claims = _parse_claims(llm.chat_json(
        DRAFT_SYSTEM, f"KNOWN FACTS:\n{known_txt}\n\nRelations in use: {relations}\n\nQuestion: {question}"
        + (f"\n\n{focus}" if focus else "")
    ))
    yield trace.settle(ev, "ok" if claims else "fail", f"{len(claims)} node(s) to verify, in order")

    by_triple = {triple(a): a for a in answered}
    checked: list[dict] = []
    found: list[dict] = []
    for i, c in enumerate(claims):
        label = f"Node {i + 1}/{len(claims)}"
        if triple(c) in by_triple:  # the draft repeated a graph answer: already verified, kept once
            checked.append({**by_triple[triple(c)], "depends_on": c["depends_on"], "dup": True})
            continue
        broken = [d for d in c["depends_on"] if checked[d]["status"] != "accepted"]
        if broken:
            yield trace.note("factual", f"{label}: {_fmt(c)}",
                             f"Skipped: depends on node {broken[0] + 1}, which did not pass.", "fail")
            checked.append({**c, "status": "skipped", "source": None, "reason": f"depends on node {broken[0] + 1}"})
            continue
        node = yield from verify_node(store, c, trace, label, verified_only)
        if node["status"] == "accepted":
            node = yield from _check_conflicts(store, node, checked, earlier, trace, label, found, answered)
        checked.append(node)
    return checked, _caught_by_checks(checked) + found


STATE_ANSWER_SYSTEM = """Answer the question using ONLY the numbered verified facts.
If they fully answer it, answer in plain, concise prose and list the numbers of the facts you used.
If they do not fully answer every part of the question, set answerable to false and do not answer.
Return only JSON: {"answerable": true, "answer": "...", "used": [1]}"""


def answer_from_state(store, question: str, prior: dict, trace: Trace):
    """Generator (compression c): answer from a similar question's verified facts, re-validated against the graph,
    without drafting and checking a new claim chain. Returns a tier result, or None to run the full procedure."""
    triples = [t for t in prior["state"].get("triples", []) if t.get("subject") and t.get("object")]
    if not triples:
        return None
    ev = trace.start("states", "Trying the verified facts of a similar question first",
                     f"“{prior['question']}” (similarity {prior['similarity']}), {len(triples)} verified fact(s)")
    yield ev
    stale = still_holds(store, triples)
    if stale:
        yield trace.settle(ev, "warn", f"Those facts no longer all hold ({stale}). Running the full check.")
        return None
    numbered = "\n".join(f"{i}. {t['subject']} {t['relation'].replace('_', ' ')} {t['object']}"
                         for i, t in enumerate(triples, 1))
    d = llm.chat_json(STATE_ANSWER_SYSTEM, f"Question: {question}\n\nVerified facts:\n{numbered}")
    d = d if isinstance(d, dict) else {}
    used = []
    for i in d.get("used") or []:
        if isinstance(i, int) and 1 <= i <= len(triples) and triples[i - 1] not in used:
            used.append(triples[i - 1])
    text = str(d.get("answer") or "").strip()
    if d.get("answerable") is not True or not used or not text:
        yield trace.settle(ev, "info", "They do not answer every part of this question. Running the full check.")
        return None
    accepted = [{**t, "status": "accepted", "reason": "verified for a similar question, re-checked now",
                 "depends_on": []} for t in used]
    problems = [] if guardrails.disabled() else guardrails.factual_problems(question, text, accepted, [])
    if problems:
        yield trace.settle(ev, "warn", f"The answer added unverified detail ({'; '.join(problems[:2])}). "
                                       "Running the full check.")
        return None
    yield trace.settle(ev, "ok", f"Answered from {len(used)} fact(s) verified for the similar question and re-checked "
                                 "against the graph - no new claim chain needed.")
    graph_share = sum(c.get("source") in ("graph", "graph-corrected") for c in accepted) / len(accepted)
    return {"tier": "factual", "answer": text, "confidence": "high" if graph_share >= 0.5 else "medium",
            "path": accepted, "caught": [], "withheld": 0, "cacheable": False, "stopped": None,
            "compressed": "state_answer"}


def answer(store, question: str, entities: list[str], trace: Trace, prior: dict | None = None,
           earlier: list[dict] | None = None, verified_only: bool = False, time_sensitive: bool = False):
    """Generator: yields trace events, returns the tier result.

    prior: a similar question's verified state (states.find); earlier: claims this conversation established.
    time_sensitive: the question asks about the present, so facts must still be current (temporal.py).
    """
    if compression.similar_enough(prior) and not verified_only and not time_sensitive:
        reused = yield from answer_from_state(store, question, prior, trace)
        if reused:
            return reused
    checked, found = yield from walk(store, question, entities, trace, prior, earlier, verified_only, time_sensitive)
    accepted = [c for c in checked if c["status"] == "accepted"]
    ended = [c for c in checked if c.get("ended")]
    missing = [c for c in checked if c["status"] != "accepted" and not c.get("ended")]

    ev = trace.start("compose", "Writing the answer from verified nodes only",
                     f"{len(accepted)} verified, {len(missing)} left out")
    yield ev
    if accepted:
        facts = "\n".join(f"- {c['subject']} {c['relation'].replace('_', ' ')} {c['object']}"
                          + (f" ({c['time_note']})" if c.get("time_note") else "") for c in accepted)
        gaps = "\n".join(f"- {c.get('part_text') or c['subject'] + ' ' + c['relation'].replace('_', ' ') + ' ?'}"
                         for c in missing) or "(none)"
        fixes = "\n".join(f"- earlier: {_fmt(c['corrects'])}; now verified: {_fmt(c)}" for c in accepted if c.get("corrects"))
        past = "\n".join(f"- {c['subject']} {c['relation'].replace('_', ' ')} {c['object']} ({c['time_note']})"
                         for c in ended)
        prompt = (f"Question: {question}\n\nVerified facts:\n{facts}\n\nCould not verify:\n{gaps}"
                  + (f"\n\nNo longer current:\n{past}" if past else "")
                  + (f"\n\nCorrections to earlier answers:\n{fixes}" if fixes else ""))
        text = llm.chat_text(COMPOSE_SYSTEM, prompt)
        yield trace.settle(ev, "ok")
        text = yield from guardrails.guard_factual(question, text, accepted, missing + ended, prompt, COMPOSE_STRICT,
                                                   trace, found)
    else:
        text = "I couldn't verify any facts to answer this, so I'd rather not guess."
        yield trace.settle(ev, "warn", "Nothing verified - declining to answer")

    graph_share = sum(c["source"] in ("graph", "graph-corrected") for c in accepted) / max(len(accepted), 1)
    confidence = "high" if accepted and not missing and graph_share >= 0.5 else "medium" if accepted else "low"
    if confidence == "high" and any(c.get("stale") for c in accepted):
        confidence = "medium"  # true as of an older check, or only as far as the model knows
    return {
        "tier": "factual",
        "answer": text.strip(),
        "confidence": confidence,
        "path": checked,
        "caught": found,
        "withheld": sum(c["status"] == "unverified" for c in checked),
        "cacheable": bool(accepted) and not missing and not time_sensitive,
        "stopped": None if accepted else {"reason": "nothing_verified", "detail": "no claim passed verification"},
    }

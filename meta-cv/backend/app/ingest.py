"""Text -> validated facts. LLM output is never trusted blindly:

- proposed relation rules are enforced only once approved (or auto-approved above a threshold)
- triples that are low-confidence or contradict an approved rule go to a review queue
"""
import time

from . import config, llm
from .db import connect


def known_relations(store) -> list[str]:
    with connect() as conn:
        names = {r["relation"] for r in conn.execute("SELECT relation FROM rule_proposals")}
    names |= {r["relation"] for r in store.relation_stats()}
    return sorted(names)


def record_manual_rule(relation: str, functional: bool, transitive: bool) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO rule_proposals(relation, functional, transitive, confidence, rationale, status, created_at) "
            "VALUES (?,?,?,1.0,'set manually','approved',?) "
            "ON CONFLICT(relation) DO UPDATE SET functional=excluded.functional, transitive=excluded.transitive, "
            "confidence=1.0, rationale='set manually', status='approved'",
            (relation, int(functional), int(transitive), time.time()),
        )


def ensure_rule(store, relation: str) -> str | None:
    """Propose rules for a relation the first time it is seen. Returns the new status, if any."""
    with connect() as conn:
        if conn.execute("SELECT 1 FROM rule_proposals WHERE relation=?", (relation,)).fetchone():
            return None
    p = llm.propose_relation_rules(relation)
    if not p["functional"] and not p["transitive"]:
        status = "approved"  # nothing to enforce, nothing to review
    elif config.RULE_AUTO_THRESHOLD is not None and p["confidence"] >= config.RULE_AUTO_THRESHOLD:
        status = "auto"
    else:
        status = "pending"
    with connect() as conn:
        conn.execute(
            "INSERT INTO rule_proposals(relation, functional, transitive, confidence, rationale, status, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (relation, int(p["functional"]), int(p["transitive"]), p["confidence"], p["rationale"], status, time.time()),
        )
    if status in ("approved", "auto") and (p["functional"] or p["transitive"]):
        store.define_relation(relation, p["functional"], p["transitive"])
    return status


def _queue(subject, relation, obj, source, reason) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO review_queue(subject, relation, object, source, reason, created_at) VALUES (?,?,?,?,?,?)",
            (subject, relation, obj, source, reason, time.time()),
        )


def ingest(store, text: str, source: str | None) -> dict:
    triples = llm.extract_triples(text, known_relations(store))
    result = {"stored": [], "queued": [], "rules_proposed": []}
    for t in triples:
        s, r, o = t["subject"], t["relation"], t["object"]
        fact = {"subject": s, "relation": r, "object": o}
        if t["confidence"] < config.MIN_TRIPLE_CONFIDENCE:
            _queue(s, r, o, source, f"low confidence ({t['confidence']:.2f})")
            result["queued"].append({**fact, "reason": "low_confidence"})
            continue
        status = ensure_rule(store, r)
        if status:
            result["rules_proposed"].append({"relation": r, "status": status})
        verdict = store.validate(s, r, o)
        if verdict["verdict"] == "contradicted":
            _queue(s, r, o, source, verdict["reason"])
            result["queued"].append({**fact, "reason": verdict["reason"]})
            continue
        store.add_fact(s, r, o, source)
        result["stored"].append({**fact, "verdict": verdict["verdict"]})
    return result

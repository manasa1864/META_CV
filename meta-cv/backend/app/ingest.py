"""Text -> validated facts. LLM output is never trusted blindly:

- proposed relation rules are enforced only once approved (or auto-approved above a threshold)
- triples that are low-confidence, or that the graph cannot support (verdict unknown) and whose
  source is not in TRUSTED_SOURCES, go to the review queue instead of becoming ground truth
- everything written passes the write gate (graph_guard), which quarantines contradictions,
  incompatible pairs, cycles and likely duplicate entities
"""
import time

from . import config, graph_guard, llm
from .db import connect


def known_relations(store) -> list[str]:
    with connect() as conn:
        names = {r["relation"] for r in conn.execute("SELECT relation FROM rule_proposals")}
    names |= set(store.relation_names())
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
    result = {"stored": [], "queued": [], "quarantined": [], "rules_proposed": []}
    trusted = source in config.TRUSTED_SOURCES
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
        if verdict["verdict"] == "unknown" and not trusted:
            _queue(s, r, o, source, "not supported by the graph and the source is not trusted")
            result["queued"].append({**fact, "reason": "unverified_source"})
            continue
        res = graph_guard.add_fact_checked(store, s, r, o, source, added_by="ingest")
        if res["status"] == "quarantined":
            result["quarantined"].append({**fact, "reason": res["reason"]})
            continue
        result["stored"].append({**fact, "verdict": verdict["verdict"]})
    return result

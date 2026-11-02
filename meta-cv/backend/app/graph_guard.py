"""Write gate: every write to the fact graph goes through add_fact_checked().

A fact is written only if it passes: validate (no contradiction of a single-valued relation),
the relation's type signature (engine/entity_types.py: no year where a place belongs, no place where a
person belongs),
no incompatible_with / contradicts clash for the same subject, no cycle or self-loop in an acyclic
relation, no new name that looks like a near-duplicate of an existing entity, no instruction-like
text (safety.injection_signals: a fact must not carry a prompt injection into the model), and, for an
untrusted source and a relation Wikidata records, no contradiction by Wikidata. Anything else
is quarantined with its reason (a table in the state database, never nodes in the fact graph),
listed at GET /quarantine. Writes invalidate the cache entries that depended on the changed triple.

Each checked fact also updates its source's record (source_trust.py): backed by the graph or Wikidata
counts as confirmed, a contradiction / wrong type / injected instruction as contradicted. A source that is
often contradicted has its new facts sent to the review queue instead of the graph.
"""
import difflib
import time

from . import config, safety, source_trust, state_cache
from .db import connect
from .engine import entity_types, wikidata
from .engine.entities import alias_candidates, normalize
from .graph_common import EXCLUSIVE_RELATIONS, rel_name


def _quarantine(s: str, r: str, o: str, source: str | None, reason: str) -> int:
    with connect() as conn:
        row = conn.execute("SELECT id FROM quarantine WHERE subject=? AND relation=? AND object=?", (s, r, o)).fetchone()
        if row:
            return row["id"]
        return conn.execute("INSERT INTO quarantine(subject, relation, object, source, reason, created_at) "
                            "VALUES (?,?,?,?,?,?)", (s, r, o, source, reason, time.time())).lastrowid


def _queue(s: str, r: str, o: str, source: str | None, reason: str) -> int:
    with connect() as conn:
        row = conn.execute("SELECT id FROM review_queue WHERE subject=? AND relation=? AND object=? AND status='pending'",
                           (s, r, o)).fetchone()
        if row:
            return row["id"]
        return conn.execute("INSERT INTO review_queue(subject, relation, object, source, reason, created_at) "
                            "VALUES (?,?,?,?,?,?)", (s, r, o, source, reason, time.time())).lastrowid


def _duplicate_of(store, name: str) -> str | None:
    n = store.resolve(name)
    candidates = store.similar_names(n)
    if n in candidates:
        return None
    for c in candidates:
        if difflib.SequenceMatcher(None, n, c).ratio() >= 0.85:
            return c
    initials = alias_candidates(n, candidates)
    return initials[0] if initials else None


def check(store, s: str, r: str, o: str, source: str | None = None) -> tuple[list[tuple[str, str]], bool]:
    """([(kind, reason)] for why this fact must not be written, whether evidence backs it).
    kind is injection / contradicted / type / cycle / clash / duplicate; all but duplicate count against the source."""
    signals = safety.injection_signals(f"{s}\n{r}\n{o}")
    if signals:  # checked first: a supported-looking fact must not smuggle instructions into prompts
        return [("injection", f"the fact contains instruction-like text ({', '.join(signals)}), a prompt-injection risk")], False
    out = []
    v = store.validate(s, r, o)
    if v["verdict"] == "contradicted":
        out.append(("contradicted", v["reason"]))
    if v["verdict"] == "supported":
        return [], True
    confirmed = False
    if not out and source not in config.TRUSTED_SOURCES and wikidata.enabled() and wikidata.covers(r):
        ev = wikidata.check(s, r, o)
        if ev["verdict"] == "contradicted":
            out.append(("contradicted", ev["reason"]))
        confirmed = ev["verdict"] == "supported"
    wrong_type = entity_types.check(store, s, r, o)
    if wrong_type:
        out.append(("type", wrong_type))
    flags = store.relation_flags(r)
    rs, ro = store.resolve(s), store.resolve(o)
    if flags.get("acyclic"):
        if rs == ro:
            out.append(("cycle", f"{r} is acyclic; a self-loop is not allowed"))
        elif store.path_exists(o, r, s):
            out.append(("cycle", f"{r} is acyclic; {o} already leads back to {s}"))
    excl = {f["object"] if f["subject"] == ro else f["subject"]
            for f in store.facts_about(o) if f["relation"] in EXCLUSIVE_RELATIONS and ro in (f["subject"], f["object"])}
    clash = [f["object"] for f in store.facts_about(s) if f["subject"] == rs and f["relation"] == r and f["object"] in excl]
    if clash:
        out.append(("clash", f"{s} {r} {clash[0]}, which the graph marks incompatible with {o}"))
    for name in (s, o):
        dup = _duplicate_of(store, name)
        if dup:
            out.append(("duplicate", f"'{normalize(name)}' may be the same entity as existing '{dup}' (add an alias or rename)"))
    return out, confirmed and not out


def problems(store, s: str, r: str, o: str) -> list[str]:
    """Why this fact must not be written (empty list = it may be)."""
    return [reason for _, reason in check(store, s, r, o)[0]]


def add_fact_checked(store, subject: str, relation: str, obj: str, source: str | None = None, *,
                     added_by: str | None = None, batch_id: str | None = None, force: bool = False,
                     valid_from: str | None = None, valid_to: str | None = None, reviewed: bool = False) -> dict:
    """force: write as is (an admin released it from quarantine). reviewed: a person accepted it from the review
    queue, so the source-trust hold does not apply (the other checks do)."""
    r = rel_name(relation)
    if not force:
        issues, confirmed = check(store, subject, r, obj, source)
        if any(kind != "duplicate" for kind, _ in issues):
            source_trust.record(source, "contradicted")
        elif confirmed:
            source_trust.record(source, "confirmed")
        if issues:
            reason = "; ".join(why for _, why in issues)
            qid = _quarantine(normalize(subject), r, normalize(obj), source, reason)
            return {"status": "quarantined", "reason": reason, "quarantine_id": qid}
        held = None if reviewed or confirmed else source_trust.held_back(source)
        if held:
            rid = _queue(normalize(subject), r, normalize(obj), source, held)
            return {"status": "queued", "reason": held, "review_id": rid}
    created = store.add_fact(subject, r, obj, source, added_by=added_by, batch_id=batch_id,
                             valid_from=valid_from, valid_to=valid_to)
    state_cache.invalidate_triples([f"{normalize(subject)}|{r}|{normalize(obj)}"])
    return {"status": "accepted", "created": created}


def release(store, quarantine_id: int, added_by: str = "admin") -> dict:
    """An admin accepts a quarantined fact as it is."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM quarantine WHERE id=?", (quarantine_id,)).fetchone()
    if row is None:
        raise KeyError(quarantine_id)
    res = add_fact_checked(store, row["subject"], row["relation"], row["object"], row["source"], added_by=added_by,
                           force=True)
    with connect() as conn:
        conn.execute("DELETE FROM quarantine WHERE id=?", (quarantine_id,))
    return res

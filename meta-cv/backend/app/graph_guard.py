"""Write gate: every write to the fact graph goes through add_fact_checked().

A fact is written only if it passes: validate (no contradiction of a single-valued relation),
no incompatible_with / contradicts clash for the same subject, no cycle or self-loop in an acyclic
relation, and no new name that looks like a near-duplicate of an existing entity. Anything else
is quarantined with its reason (a table in the state database, never nodes in the fact graph),
listed at GET /quarantine. Writes invalidate the cache entries that depended on the changed triple.
"""
import difflib
import time

from . import state_cache
from .db import connect
from .engine.entities import alias_candidates, normalize
from .graph_common import EXCLUSIVE_RELATIONS, rel_name


def _quarantine(s: str, r: str, o: str, source: str | None, reason: str) -> int:
    with connect() as conn:
        row = conn.execute("SELECT id FROM quarantine WHERE subject=? AND relation=? AND object=?", (s, r, o)).fetchone()
        if row:
            return row["id"]
        return conn.execute("INSERT INTO quarantine(subject, relation, object, source, reason, created_at) "
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


def problems(store, s: str, r: str, o: str) -> list[str]:
    """Why this fact must not be written (empty list = it may be)."""
    out = []
    v = store.validate(s, r, o)
    if v["verdict"] == "contradicted":
        out.append(v["reason"])
    if v["verdict"] == "supported":
        return []
    flags = store.relation_flags(r)
    rs, ro = store.resolve(s), store.resolve(o)
    if flags.get("acyclic"):
        if rs == ro:
            out.append(f"{r} is acyclic; a self-loop is not allowed")
        elif store.path_exists(o, r, s):
            out.append(f"{r} is acyclic; {o} already leads back to {s}")
    excl = {f["object"] if f["subject"] == ro else f["subject"]
            for f in store.facts_about(o) if f["relation"] in EXCLUSIVE_RELATIONS and ro in (f["subject"], f["object"])}
    clash = [f["object"] for f in store.facts_about(s) if f["subject"] == rs and f["relation"] == r and f["object"] in excl]
    if clash:
        out.append(f"{s} {r} {clash[0]}, which the graph marks incompatible with {o}")
    for name in (s, o):
        dup = _duplicate_of(store, name)
        if dup:
            out.append(f"'{normalize(name)}' may be the same entity as existing '{dup}' (add an alias or rename)")
    return out


def add_fact_checked(store, subject: str, relation: str, obj: str, source: str | None = None, *,
                     added_by: str | None = None, batch_id: str | None = None, force: bool = False) -> dict:
    r = rel_name(relation)
    issues = [] if force else problems(store, subject, r, obj)
    if issues:
        qid = _quarantine(normalize(subject), r, normalize(obj), source, "; ".join(issues))
        return {"status": "quarantined", "reason": "; ".join(issues), "quarantine_id": qid}
    created = store.add_fact(subject, r, obj, source, added_by=added_by, batch_id=batch_id)
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

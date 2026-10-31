"""Graph audit: finds what would make the factual tier verify against bad data.

    python -m app.audit [--json out.json] [--fail-on error|warning]

Checks (severity): functional-relation violations (error); incompatible_with / contradicts pairs
asserted for one subject (error); cycles in acyclic relations and self-loops (error); near-duplicate
entity names (warning, blocked by first/last token so it is not O(n^2)); facts without provenance
(warning) or not re-verified for STALE_DAYS (info, temporal.stale); relations used without a rule
decision, and possible relation-name drift (info); cached paths / reasoning states that cite graph
facts that are gone (warning).

On AuraDB every check is a server-side Cypher aggregation with a LIMIT; nothing pulls the whole graph.
The last result is cached (AUDIT_CACHE_SECONDS) for GET /audit/summary and for the pipeline's
"refuse on a known conflict" stop condition, which only ever reads the cache.
"""
import argparse
import difflib
import json
import sys
import threading
import time
import zlib
from collections import defaultdict

import networkx as nx

from . import config
from .db import connect, init_db
from .engine.entities import normalize
from .graph_common import EXCLUSIVE_RELATIONS

SAMPLE = 50
MAX_FINDINGS = 1000
_cache: dict = {"result": None, "at": 0.0}
_lock = threading.Lock()


def _finding(check: str, severity: str, detail: str, entities=(), facts=()) -> dict:
    return {"check": check, "severity": severity, "detail": detail, "entities": sorted(set(entities)), "facts": list(facts)[:SAMPLE]}


def _unwrap(store):
    """The concrete store behind a ResilientStore."""
    if hasattr(store, "_primary"):
        return store._fallback() if store.degraded else store._primary
    return store


WINDOW = 6


def _near_duplicates(names: list[str]) -> list[tuple[str, str]]:
    """Sorted-neighbourhood blocking: near-duplicates share a prefix or a suffix, so they end up within a few
    places of each other when the names are sorted forwards or reversed. O(n * WINDOW) comparisons."""
    pairs = set()
    uniq = sorted(set(names))
    for key in (lambda x: x, lambda x: x[::-1]):
        ordered = sorted(uniq, key=key)
        for i, a in enumerate(ordered):
            for b in ordered[i + 1:i + 1 + WINDOW]:
                if a != b and difflib.SequenceMatcher(None, a, b).ratio() >= 0.85:
                    pairs.add(tuple(sorted((a, b))))
    return sorted(pairs)


def _relation_drift(names: list[str]) -> list[tuple[str, str]]:
    out = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if a.replace("_", "") == b.replace("_", "") or difflib.SequenceMatcher(None, a, b).ratio() >= 0.85:
                out.append((a, b))
    return out


def _rule_decisions() -> set[str]:
    with connect() as conn:
        return {r["relation"] for r in conn.execute("SELECT relation FROM rule_proposals")}


def _cached_graph_triples() -> list[tuple[str, str, str, str]]:
    """(subject, relation, object, where) for graph-backed claims inside cached paths and reasoning states."""
    out = []
    with connect() as conn:
        rows = conn.execute("SELECT key, value FROM state_cache ORDER BY created_at DESC LIMIT 1000").fetchall()
        states = conn.execute("SELECT id, tier, state FROM reasoning_states ORDER BY id DESC LIMIT 1000").fetchall()
    for r in rows:
        try:
            v = json.loads(zlib.decompress(r["value"]))
        except (zlib.error, ValueError):
            continue
        if isinstance(v, dict) and v.get("tier") == "factual":
            for c in v.get("path", []):
                if c.get("status") == "accepted" and c.get("source") in ("graph", "graph-corrected"):
                    out.append((c["subject"], c["relation"], c["object"], f"cached path {r['key'][:8]}"))
    for s in states:
        v = json.loads(zlib.decompress(s["state"]))
        for c in v.get("triples", []) + v.get("premises", []):
            if c.get("source") in ("graph", "graph-corrected"):
                out.append((c["subject"], c["relation"], c["object"], f"reasoning state {s['id']}"))
    return out


# --------------------------------------------------------------------------------------------- Neo4j
def _audit_neo4j(s, stale_days: float, include_cache: bool = True) -> list[dict]:
    E, R = s.E, s.R
    found = []
    rules = s._load_rules()
    functional = [n.upper() for n, f in rules.items() if f["functional"]]
    acyclic = [n for n, f in rules.items() if f["acyclic"]]

    if functional:
        for r in s._run(f"MATCH (a:{E})-[e]->(b:{E}) WHERE type(e) IN $rels "
                        "WITH a, type(e) AS t, collect(b.name) AS objs WHERE size(objs) > 1 "
                        f"RETURN a.name AS s, toLower(t) AS r, objs LIMIT {MAX_FINDINGS}", rels=functional):
            found.append(_finding("functional_violation", "error",
                                  f"{r['s']} has {len(r['objs'])} values for single-valued {r['r']}: {r['objs']}",
                                  [r["s"], *r["objs"]], [f"{r['s']}|{r['r']}|{o}" for o in r["objs"]]))

    excl = [x.upper() for x in EXCLUSIVE_RELATIONS]
    for r in s._run(f"MATCH (x:{E})-[c]-(y:{E}) WHERE type(c) IN $excl AND elementId(x) < elementId(y) "
                    f"MATCH (a:{E})-[r1]->(x), (a)-[r2]->(y) WHERE type(r1) = type(r2) "
                    f"RETURN DISTINCT a.name AS s, toLower(type(r1)) AS r, x.name AS x, y.name AS y LIMIT {MAX_FINDINGS}", excl=excl):
        found.append(_finding("incompatible_pair", "error",
                              f"{r['s']} {r['r']} both {r['x']} and {r['y']}, which the graph marks incompatible",
                              [r["s"], r["x"], r["y"]], [f"{r['s']}|{r['r']}|{r['x']}", f"{r['s']}|{r['r']}|{r['y']}"]))

    for r in s._run(f"MATCH (a:{E})-[e]->(a) RETURN a.name AS a, toLower(type(e)) AS r LIMIT {MAX_FINDINGS}"):
        found.append(_finding("self_loop", "error" if r["r"] in acyclic else "warning",
                              f"{r['a']} {r['r']} itself", [r["a"]], [f"{r['a']}|{r['r']}|{r['a']}"]))
    for rel in acyclic:
        for r in s._run(f"MATCH p = (a:{E})-[:{rel.upper()}*2..{config.PATH_MAX_DEPTH}]->(a) "
                        f"RETURN [n IN nodes(p) | n.name] AS p LIMIT {MAX_FINDINGS}"):
            found.append(_finding("cycle", "error", f"{rel} cycle: {' -> '.join(r['p'])}", r["p"]))

    names, skip = [], 0
    while True:  # names only, paged; the comparison itself is O(n) over sorted neighbours
        page = [r["n"] for r in s._run(f"MATCH (e:{E}) RETURN e.name AS n ORDER BY n SKIP $skip LIMIT 20000", skip=skip)]
        names += page
        if len(page) < 20000:
            break
        skip += 20000
    for a, b in _near_duplicates(names):
        found.append(_finding("near_duplicate", "warning", f"'{a}' and '{b}' may be the same entity", [a, b]))

    r = s._run(f"MATCH (a:{E})-[e]->(b:{E}) WHERE e.source IS NULL OR e.source = 'unknown' "
               "RETURN count(e) AS n, collect(a.name + '|' + toLower(type(e)) + '|' + b.name)[..50] AS sample")[0]
    if r["n"]:
        found.append(_finding("no_provenance", "warning", f"{r['n']} fact(s) have no source", facts=r["sample"]))
    r = s._run(f"MATCH (a:{E})-[e]->(b:{E}) WHERE e.verified_at IS NULL OR e.verified_at < datetime() - duration({{days: $d}}) "
               "RETURN count(e) AS n, collect(a.name + '|' + toLower(type(e)) + '|' + b.name)[..50] AS sample",
               d=int(stale_days))[0]
    if r["n"]:
        found.append(_finding("stale", "info", f"{r['n']} fact(s) not re-verified in {int(stale_days)} days (temporal.stale)",
                              facts=r["sample"]))

    used = [x["r"] for x in s._run(f"MATCH (:{E})-[e]->(:{E}) RETURN DISTINCT toLower(type(e)) AS r LIMIT 1000")]
    decided = _rule_decisions() | {n for n, f in rules.items() if any(f.values())}
    for rel in sorted(set(used) - decided):
        found.append(_finding("undefined_relation", "info", f"relation '{rel}' is used but has no rule decision"))
    for a, b in _relation_drift(sorted(set(used))):
        found.append(_finding("relation_drift", "info", f"relations '{a}' and '{b}' may be synonyms"))

    cached = _cached_graph_triples() if include_cache else []
    if cached:
        rows = [{"s": normalize(a), "r": b.upper(), "o": normalize(c)} for a, b, c, _ in cached]
        present = {(x["s"], x["r"], x["o"]) for x in s._run(
            f"UNWIND $rows AS t MATCH (a:{E} {{name: t.s}})-[e]->(b:{E} {{name: t.o}}) WHERE type(e) = t.r "
            "RETURN t.s AS s, t.r AS r, t.o AS o", rows=rows)}
        for (a, b, c, where), row in zip(cached, rows):
            if (row["s"], row["r"], row["o"]) not in present:
                found.append(_finding("dangling_cache", "warning", f"{where} cites {a} {b} {c}, no longer in the graph",
                                      [row["s"], row["o"]], [f"{a}|{b}|{c}"]))
    return found


# --------------------------------------------------------------------------------------------- SQLite
def _audit_sqlite(s, stale_days: float, include_cache: bool = True) -> list[dict]:
    found = []
    with connect(s.path) as conn:
        for r in conn.execute("SELECT f.subject AS s, f.relation AS r, group_concat(f.object, '\x1f') AS objs "
                              "FROM facts f JOIN relations rl ON rl.name = f.relation AND rl.functional = 1 "
                              f"GROUP BY f.subject, f.relation HAVING COUNT(*) > 1 LIMIT {MAX_FINDINGS}"):
            objs = r["objs"].split("\x1f")
            found.append(_finding("functional_violation", "error",
                                  f"{r['s']} has {len(objs)} values for single-valued {r['r']}: {objs}",
                                  [r["s"], *objs], [f"{r['s']}|{r['r']}|{o}" for o in objs]))
        marks = ",".join("?" * len(EXCLUSIVE_RELATIONS))
        for r in conn.execute(
                "SELECT DISTINCT a.subject AS s, a.relation AS r, a.object AS x, b.object AS y FROM facts c "
                "JOIN facts a ON a.object = c.subject JOIN facts b ON b.object = c.object "
                f"AND b.subject = a.subject AND b.relation = a.relation WHERE c.relation IN ({marks}) LIMIT {MAX_FINDINGS}",
                EXCLUSIVE_RELATIONS):
            found.append(_finding("incompatible_pair", "error",
                                  f"{r['s']} {r['r']} both {r['x']} and {r['y']}, which the graph marks incompatible",
                                  [r["s"], r["x"], r["y"]], [f"{r['s']}|{r['r']}|{r['x']}", f"{r['s']}|{r['r']}|{r['y']}"]))
        acyclic = {r["name"] for r in conn.execute("SELECT name FROM relations WHERE acyclic = 1")}
        for r in conn.execute(f"SELECT subject AS a, relation AS r FROM facts WHERE subject = object LIMIT {MAX_FINDINGS}"):
            found.append(_finding("self_loop", "error" if r["r"] in acyclic else "warning", f"{r['a']} {r['r']} itself",
                                  [r["a"]], [f"{r['a']}|{r['r']}|{r['a']}"]))
        for rel in acyclic:
            g = nx.DiGraph((x["subject"], x["object"]) for x in
                           conn.execute("SELECT subject, object FROM facts WHERE relation = ? AND subject != object", (rel,)))
            for cyc in list(nx.simple_cycles(g, length_bound=config.PATH_MAX_DEPTH))[:20]:
                found.append(_finding("cycle", "error", f"{rel} cycle: {' -> '.join(cyc + cyc[:1])}", cyc))
        names = [r["n"] for r in conn.execute("SELECT subject AS n FROM facts UNION SELECT object FROM facts")]
        for a, b in _near_duplicates(names):
            found.append(_finding("near_duplicate", "warning", f"'{a}' and '{b}' may be the same entity", [a, b]))
        rows = conn.execute("SELECT subject||'|'||relation||'|'||object AS t FROM facts "
                            "WHERE source IS NULL OR source = 'unknown'").fetchall()
        if rows:
            found.append(_finding("no_provenance", "warning", f"{len(rows)} fact(s) have no source", facts=[r["t"] for r in rows]))
        rows = conn.execute("SELECT subject||'|'||relation||'|'||object AS t FROM facts WHERE verified_at IS NULL "
                            "OR verified_at < ?", (time.time() - stale_days * 86400,)).fetchall()
        if rows:
            found.append(_finding("stale", "info", f"{len(rows)} fact(s) not re-verified in {int(stale_days)} days "
                                                   "(temporal.stale)", facts=[r["t"] for r in rows]))
        used = sorted({r["relation"] for r in conn.execute("SELECT DISTINCT relation FROM facts")})
        flagged = {r["name"] for r in conn.execute(
            "SELECT name FROM relations WHERE functional = 1 OR transitive = 1 OR acyclic = 1")}
    for rel in sorted(set(used) - _rule_decisions() - flagged):
        found.append(_finding("undefined_relation", "info", f"relation '{rel}' is used but has no rule decision"))
    for a, b in _relation_drift(used):
        found.append(_finding("relation_drift", "info", f"relations '{a}' and '{b}' may be synonyms"))
    for a, b, c, where in (_cached_graph_triples() if include_cache else []):
        if s.validate(a, b, c)["verdict"] != "supported":
            found.append(_finding("dangling_cache", "warning", f"{where} cites {a} {b} {c}, no longer in the graph",
                                  [normalize(a), normalize(c)], [f"{a}|{b}|{c}"]))
    return found


def run_audit(store, stale_days: float | None = None, include_cache: bool = True, remember: bool = True) -> dict:
    """include_cache=False / remember=False are for namespaced runs (scale tests) that must not mix with,
    or replace, the production audit result."""
    s = _unwrap(store)
    t0 = time.time()
    stale = config.STALE_DAYS if stale_days is None else stale_days
    findings = (_audit_neo4j if s.backend == "neo4j" else _audit_sqlite)(s, stale, include_cache)
    counts = {sev: sum(f["severity"] == sev for f in findings) for sev in ("error", "warning", "info")}
    by_check = defaultdict(int)
    for f in findings:
        by_check[f["check"]] += 1
    result = {"ran_at": time.time(), "duration_s": round(time.time() - t0, 3), "backend": s.backend,
              "counts": counts, "by_check": dict(by_check), "findings": findings}
    if remember:
        with _lock:
            _cache.update(result=result, at=time.time())
    return result


def summary(store, max_age: float | None = None) -> dict:
    """Counts only, computed at most once per AUDIT_CACHE_SECONDS so it cannot hammer Aura."""
    max_age = config.AUDIT_CACHE_SECONDS if max_age is None else max_age
    with _lock:
        cached, at = _cache["result"], _cache["at"]
    if cached is None or time.time() - at > max_age:
        cached = run_audit(store)
    return {k: cached[k] for k in ("ran_at", "duration_s", "backend", "counts", "by_check")}


def last_result() -> dict | None:
    with _lock:
        return _cache["result"]


def cached_conflict(store, entities: list[str]) -> str | None:
    """Stop condition 2: an error-level finding about one of these entities, from the last audit only."""
    with _lock:
        result = _cache["result"]
    if not result or not entities:
        return None
    resolve = getattr(store, "resolve", normalize)
    names = {resolve(e) for e in entities}
    for f in result["findings"]:
        if f["severity"] == "error" and names & set(f["entities"]):
            return f["detail"]
    return None


def clear_cache() -> None:
    with _lock:
        _cache.update(result=None, at=0.0)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Audit the fact graph for contradictions, cycles, duplicates and stale facts.")
    ap.add_argument("--json", help="write the full result to this file")
    ap.add_argument("--fail-on", choices=("error", "warning"), help="exit 1 if a finding of this severity (or worse) exists")
    ap.add_argument("--stale-days", type=float)
    args = ap.parse_args(argv)
    init_db()
    from .graph_store import get_store

    result = run_audit(get_store(), args.stale_days)
    for f in result["findings"]:
        print(f"[{f['severity']:7}] {f['check']:20} {f['detail']}")
    print(f"\n{result['counts']} in {result['duration_s']}s on {result['backend']}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
    bad = result["counts"]["error"] + (result["counts"]["warning"] if args.fail_on == "warning" else 0)
    return 1 if args.fail_on and bad else 0


if __name__ == "__main__":
    sys.exit(main())

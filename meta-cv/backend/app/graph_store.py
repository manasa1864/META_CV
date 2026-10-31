"""Graph stores. Production is Neo4j AuraDB (neo4j_store.py); SqliteGraphStore is the test/dev
fallback and the read-only snapshot used when Aura is unreachable. Both satisfy the same
contract (tests/test_store_contract.py); callers only use the methods below.
"""
import logging
import time
from typing import Literal

import networkx as nx

from . import config, meter
from .db import connect, init_graph_db
from .engine.entities import normalize
from .graph_common import GraphUnavailable, ReadOnlyGraph, rel_name

log = logging.getLogger(__name__)
Verdict = Literal["supported", "contradicted", "unknown"]


class SqliteGraphStore:
    backend = "sqlite"

    def __init__(self, path: str | None = None, read_only: bool = False):
        self.path, self.read_only = path, read_only
        if path:
            init_graph_db(path)

    def _conn(self):
        meter.record_graph()
        return connect(self.path)

    def _writable(self) -> None:
        if self.read_only:
            raise ReadOnlyGraph("this graph is read-only")

    def init_schema(self) -> None:
        """Tables and indexes are created by db.init_db / init_graph_db."""

    def ping(self) -> None:
        with self._conn() as conn:
            conn.execute("SELECT 1")

    # names -----------------------------------------------------------------
    def resolve(self, name: str) -> str:
        n = normalize(name)
        with self._conn() as conn:
            row = conn.execute("SELECT canonical FROM aliases WHERE alias=?", (n,)).fetchone()
        return row["canonical"] if row else n

    def add_alias(self, alias: str, canonical: str) -> bool:
        """False if the alias already points somewhere else (the caller should queue it for review)."""
        self._writable()
        a, c = normalize(alias), normalize(canonical)
        with self._conn() as conn:
            row = conn.execute("SELECT canonical FROM aliases WHERE alias=?", (a,)).fetchone()
            if row and row["canonical"] != c:
                return False
            conn.execute("INSERT OR IGNORE INTO aliases(alias, canonical) VALUES (?,?)", (a, c))
        return True

    def entity_names(self, limit: int = 100000) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute("SELECT subject AS n FROM facts UNION SELECT object FROM facts LIMIT ?", (limit,))
            return [r["n"] for r in rows]

    # relations -------------------------------------------------------------
    def define_relation(self, name: str, functional: bool = False, transitive: bool = False,
                        acyclic: bool = False) -> None:
        self._writable()
        name = rel_name(name)
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO relations(name, functional, transitive, acyclic) VALUES (?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET functional=excluded.functional, transitive=excluded.transitive, "
                "acyclic=excluded.acyclic",
                (name, int(functional), int(transitive), int(acyclic)),
            )

    def relation_flags(self, relation: str) -> dict:
        with self._conn() as conn:
            row = conn.execute("SELECT functional, transitive, acyclic FROM relations WHERE name=?",
                               (str(relation).lower(),)).fetchone()
        return {k: bool(row and row[k]) for k in ("functional", "transitive", "acyclic")}

    def is_functional(self, relation: str) -> bool:
        return self.relation_flags(relation)["functional"]

    def relation_names(self) -> list[str]:
        with self._conn() as conn:
            return sorted(r["n"] for r in conn.execute("SELECT name AS n FROM relations UNION SELECT DISTINCT relation FROM facts"))

    # facts -----------------------------------------------------------------
    def add_fact(self, subject: str, relation: str, obj: str, source: str | None = None, *,
                 added_by: str | None = None, batch_id: str | None = None) -> bool:
        """Idempotent. Returns True if the fact is new; re-adding only refreshes verified_at."""
        return self.add_facts([{"subject": subject, "relation": relation, "object": obj, "source": source,
                                "added_by": added_by, "batch_id": batch_id}]) == 1

    def add_facts(self, rows: list[dict]) -> int:
        self._writable()
        now, new = time.time(), 0
        prepared = [(self.resolve(r["subject"]), rel_name(r["relation"]), self.resolve(r["object"]),
                     r.get("source"), r.get("added_by"), r.get("batch_id")) for r in rows]
        with self._conn() as conn:
            for s, rel, o, src, by, batch in prepared:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO facts(subject, relation, object, source, verified_at, added_by, batch_id) "
                    "VALUES (?,?,?,?,?,?,?)", (s, rel, o, src, now, by, batch))
                if cur.rowcount:
                    new += 1
                else:
                    conn.execute("UPDATE facts SET verified_at=? WHERE subject=? AND relation=? AND object=?",
                                 (now, s, rel, o))
                conn.execute("INSERT OR IGNORE INTO relations(name) VALUES (?)", (rel,))
        return new

    def delete_batch(self, batch_id: str) -> int:
        """Remove only the facts this batch created."""
        self._writable()
        with self._conn() as conn:
            return conn.execute("DELETE FROM facts WHERE batch_id=?", (batch_id,)).rowcount

    def counts(self) -> dict:
        with self._conn() as conn:
            facts = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
            ents = conn.execute("SELECT COUNT(*) FROM (SELECT subject FROM facts UNION SELECT object FROM facts)").fetchone()[0]
        return {"entities": ents, "facts": facts}

    def connections(self, a: str, b: str) -> list[dict]:
        a, b = self.resolve(a), self.resolve(b)
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT relation, subject, object, source FROM facts WHERE (subject=? AND object=?) OR (subject=? AND object=?)",
                (a, b, b, a),
            ).fetchall()
        return [{"relation": r["relation"], "source": r["subject"], "target": r["object"], "count": 1,
                 "evidence": r["source"]} for r in rows]

    def facts_about(self, entity: str, limit: int = 50) -> list[dict]:
        """Facts touching an entity: exact (or alias) name, then name prefix, then a capped substring match."""
        e = self.resolve(entity)
        if not e:
            return []
        q = "SELECT subject, relation, object FROM facts WHERE {c} LIMIT ?"
        with self._conn() as conn:
            rows = conn.execute(q.format(c="subject=? OR object=?"), (e, e, limit)).fetchall()
            if not rows and len(e) >= 3:
                rows = conn.execute(q.format(c="subject >= ? AND subject < ? OR object >= ? AND object < ?"),
                                    (e, e + "￿", e, e + "￿", limit)).fetchall()
            if not rows and len(e) >= 4:
                like = f"%{e}%"
                rows = conn.execute(q.format(c="subject LIKE ? OR object LIKE ?"), (like, like, min(limit, 20))).fetchall()
        return [dict(r) for r in rows]

    def relation_stats(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT relation, COUNT(*) AS edges, COUNT(DISTINCT subject) AS subjects, "
                "MAX(n) AS m FROM (SELECT relation, subject, COUNT(*) AS n FROM facts GROUP BY relation, subject) "
                "JOIN (SELECT relation AS r2, COUNT(*) AS edges FROM facts GROUP BY relation) ON r2=relation "
                "GROUP BY relation"
            ).fetchall()
            flags = {r["name"]: r for r in conn.execute("SELECT name, functional, transitive FROM relations")}
        return [
            {
                "relation": r["relation"],
                "edges": r["edges"],
                "subjects": r["subjects"],
                "functional": bool(flags.get(r["relation"]) and flags[r["relation"]]["functional"]),
                "transitive": bool(flags.get(r["relation"]) and flags[r["relation"]]["transitive"]),
                "looks_functional": r["m"] == 1 and r["subjects"] >= 2,
            }
            for r in rows
        ]

    def _graph(self, relation: str) -> nx.DiGraph:
        g = nx.DiGraph()
        with self._conn() as conn:
            rows = conn.execute("SELECT subject, object FROM facts WHERE relation=?", (relation,)).fetchall()
        g.add_edges_from((r["subject"], r["object"]) for r in rows)
        return g

    def path_exists(self, a: str, relation: str, b: str) -> list[str] | None:
        """A chain of `relation` edges from a to b (bounded depth), regardless of the transitive flag."""
        g = self._graph(rel_name(relation))
        a, b = self.resolve(a), self.resolve(b)
        if a not in g:
            return None
        if a == b:
            return [a, a] if g.has_edge(a, a) else None
        return nx.single_source_shortest_path(g, a, cutoff=config.PATH_MAX_DEPTH).get(b)

    def similar_names(self, name: str, limit: int = 50) -> list[str]:
        """Existing names sharing the first token (the blocking key for duplicate detection)."""
        n = normalize(name)
        if not n:
            return []
        first = n.split()[0]
        with self._conn() as conn:
            rows = conn.execute("SELECT subject AS n FROM facts WHERE subject >= ? AND subject < ? UNION "
                                "SELECT object FROM facts WHERE object >= ? AND object < ? LIMIT ?",
                                (first, first + "￿", first, first + "￿", limit))
            return [r["n"] for r in rows]

    def validate(self, subject: str, relation: str, obj: str) -> dict:
        s, r, o = self.resolve(subject), rel_name(relation), self.resolve(obj)
        flags = self.relation_flags(r)
        g = self._graph(r)
        if g.has_edge(s, o):
            return {"verdict": "supported", "reason": "fact stored directly"}
        if flags["transitive"] and s in g and o in g:
            reach = nx.single_source_shortest_path(g, s, cutoff=config.PATH_MAX_DEPTH)
            if o in reach:
                return {"verdict": "supported", "reason": "derived via transitivity", "path": reach[o]}
        if flags["functional"] and s in g and g.out_degree(s) > 0:
            known = sorted(g.successors(s))
            return {"verdict": "contradicted", "reason": f"{r} is single-valued; stored value(s): {known}"}
        linked = sorted({c["relation"] for c in self.connections(s, o)})
        if linked:
            return {"verdict": "unknown", "reason": f"{s} and {o} are connected, but not by {r}", "related_via": linked}
        return {"verdict": "unknown", "reason": "no matching or conflicting facts"}


class ResilientStore:
    """AuraDB first. If Aura is unreachable (at startup or mid-run), answer read-only from the bundled
    snapshot (python -m app.export_snapshot), report `degraded`, and retry Aura periodically."""

    def __init__(self, make_primary, snapshot_path: str):
        self._make_primary, self._snapshot_path = make_primary, snapshot_path
        self._primary, self._snapshot, self._failed_at = None, None, 0.0
        self.degraded: str | None = None
        try:
            self._primary = make_primary()
        except GraphUnavailable as e:
            log.warning("graph unavailable at startup (%s); using the snapshot", e)
            self._degrade()

    @property
    def backend(self) -> str:
        return "neo4j" if not self.degraded else "snapshot"

    @property
    def read_only(self) -> bool:
        return bool(self.degraded)

    def _degrade(self) -> None:
        self.degraded, self._failed_at = "graph_snapshot", time.time()

    def _fallback(self):
        if self._snapshot is None:
            import os

            if not os.path.exists(self._snapshot_path):
                raise GraphUnavailable("the graph is unreachable and no snapshot is bundled")
            self._snapshot = SqliteGraphStore(self._snapshot_path, read_only=True)
        return self._snapshot

    def _maybe_recover(self) -> None:
        if self.degraded and time.time() - self._failed_at > config.GRAPH_RECOVER_SECONDS:
            try:
                self._primary = self._primary or self._make_primary()
                self._primary.ping()
                self.degraded = None
            except GraphUnavailable:
                self._failed_at = time.time()

    def __getattr__(self, name):
        self._maybe_recover()
        if self.degraded:
            return getattr(self._fallback(), name)
        attr = getattr(self._primary, name)
        if not callable(attr):
            return attr

        def call(*a, **kw):
            try:
                return attr(*a, **kw)
            except GraphUnavailable as e:
                log.warning("graph became unavailable (%s); switching to the snapshot", e)
                self._degrade()
                return getattr(self._fallback(), name)(*a, **kw)

        return call


def get_store():
    """Pick the backend from GRAPH_BACKEND (sqlite | neo4j)."""
    if config.GRAPH_BACKEND == "neo4j":
        from .neo4j_store import Neo4jGraphStore

        return ResilientStore(Neo4jGraphStore, config.GRAPH_SNAPSHOT)
    return SqliteGraphStore()

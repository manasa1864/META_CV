"""Fact graph kept in SQLite, validated with NetworkX.

Neo4jGraphStore (AuraDB) has the same interface; callers only use the methods below.
"""
from typing import Literal

import networkx as nx

from .db import connect

Verdict = Literal["supported", "contradicted", "unknown"]


class SqliteGraphStore:
    def define_relation(self, name: str, functional: bool = False, transitive: bool = False) -> None:
        with connect() as conn:
            conn.execute(
                "INSERT INTO relations(name, functional, transitive) VALUES (?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET functional=excluded.functional, transitive=excluded.transitive",
                (name, int(functional), int(transitive)),
            )

    def is_functional(self, relation: str) -> bool:
        with connect() as conn:
            row = conn.execute("SELECT functional FROM relations WHERE name=?", (relation.lower(),)).fetchone()
        return bool(row and row["functional"])

    def add_fact(self, subject: str, relation: str, obj: str, source: str | None = None) -> None:
        with connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO facts(subject, relation, object) VALUES (?,?,?)",
                (subject.lower(), relation.lower(), obj.lower()),
            )

    def connections(self, a: str, b: str) -> list[dict]:
        with connect() as conn:
            rows = conn.execute(
                "SELECT relation, subject, object FROM facts WHERE (subject=? AND object=?) OR (subject=? AND object=?)",
                (a.lower(), b.lower(), b.lower(), a.lower()),
            ).fetchall()
        return [{"relation": r["relation"].upper(), "source": r["subject"], "target": r["object"], "count": 1} for r in rows]

    def facts_about(self, entity: str, limit: int = 50) -> list[dict]:
        """Facts touching an entity: exact name first, partial match as a fallback."""
        e = entity.lower().strip()
        with connect() as conn:
            rows = conn.execute(
                "SELECT subject, relation, object FROM facts WHERE subject=? OR object=? LIMIT ?", (e, e, limit)
            ).fetchall()
            if not rows and len(e) >= 3:
                like = f"%{e}%"
                rows = conn.execute(
                    "SELECT subject, relation, object FROM facts WHERE subject LIKE ? OR object LIKE ? LIMIT ?",
                    (like, like, limit),
                ).fetchall()
        return [dict(r) for r in rows]

    def relation_stats(self) -> list[dict]:
        with connect() as conn:
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
        with connect() as conn:
            rows = conn.execute("SELECT subject, object FROM facts WHERE relation=?", (relation,)).fetchall()
        g.add_edges_from((r["subject"], r["object"]) for r in rows)
        return g

    def validate(self, subject: str, relation: str, obj: str) -> dict:
        s, r, o = subject.lower(), relation.lower(), obj.lower()
        with connect() as conn:
            rel = conn.execute("SELECT functional, transitive FROM relations WHERE name=?", (r,)).fetchone()
        functional = bool(rel and rel["functional"])
        transitive = bool(rel and rel["transitive"])

        g = self._graph(r)
        if g.has_edge(s, o):
            return {"verdict": "supported", "reason": "fact stored directly"}
        if transitive and s in g and o in g and nx.has_path(g, s, o):
            path = nx.shortest_path(g, s, o)
            return {"verdict": "supported", "reason": "derived via transitivity", "path": path}
        if functional and s in g and g.out_degree(s) > 0:
            known = sorted(g.successors(s))
            return {"verdict": "contradicted", "reason": f"{r} is single-valued; stored value(s): {known}"}
        linked = sorted({c["relation"].lower() for c in self.connections(s, o)})
        if linked:
            return {"verdict": "unknown", "reason": f"{s} and {o} are connected, but not by {r}", "related_via": linked}
        return {"verdict": "unknown", "reason": "no matching or conflicting facts"}


def get_store():
    """Pick the backend from GRAPH_BACKEND (sqlite | neo4j)."""
    from .config import GRAPH_BACKEND

    if GRAPH_BACKEND == "neo4j":
        from .neo4j_store import Neo4jGraphStore

        return Neo4jGraphStore()
    return SqliteGraphStore()

"""Fact graph in FalkorDB. Same interface as SqliteGraphStore.

Entities are (:Entity {name}); each relation becomes its own edge type
(e.g. is_a -> :IS_A) so transitivity is a native variable-length path query.
Relation flags live on (:Relation {name, functional, transitive}) nodes.
"""
import re

from falkordb import FalkorDB

from . import config

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _edge_type(relation: str) -> str:
    r = relation.lower()
    if not _NAME.match(r):  # edge types can't be parameterised, so whitelist
        raise ValueError(f"invalid relation name: {relation!r}")
    return r.upper()


class FalkorGraphStore:
    def __init__(self) -> None:
        db = FalkorDB(
            host=config.FALKORDB_HOST,
            port=config.FALKORDB_PORT,
            username=config.FALKORDB_USERNAME,
            password=config.FALKORDB_PASSWORD,
            ssl=config.FALKORDB_SSL,
        )
        self.g = db.select_graph(config.FALKORDB_GRAPH)
        self.g.query("CREATE INDEX FOR (e:Entity) ON (e.name)")  # no-op if it exists

    def define_relation(self, name: str, functional: bool = False, transitive: bool = False) -> None:
        _edge_type(name)
        self.g.query(
            "MERGE (r:Relation {name:$n}) SET r.functional=$f, r.transitive=$t",
            {"n": name.lower(), "f": functional, "t": transitive},
        )

    def add_fact(self, subject: str, relation: str, obj: str, source: str | None = None) -> None:
        t = _edge_type(relation)
        self.g.query(
            f"MERGE (a:Entity {{name:$s}}) MERGE (b:Entity {{name:$o}}) MERGE (a)-[:{t}]->(b)",
            {"s": subject.lower(), "o": obj.lower()},
        )

    def facts_about(self, entity: str, limit: int = 50) -> list[dict]:
        """Facts touching an entity: exact name first, partial match as a fallback."""
        e = entity.lower().strip()
        q = (
            "MATCH (a:Entity)-[r]->(b:Entity) WHERE {cond} "
            "RETURN a.name, toLower(type(r)), b.name LIMIT $limit"
        )
        rows = self.g.query(q.format(cond="(a.name = $e OR b.name = $e)"), {"e": e, "limit": limit}).result_set
        if not rows and len(e) >= 3:
            rows = self.g.query(q.format(cond="(a.name CONTAINS $e OR b.name CONTAINS $e)"), {"e": e, "limit": limit}).result_set
        return [{"subject": s, "relation": r, "object": o} for s, r, o in rows]

    def validate(self, subject: str, relation: str, obj: str) -> dict:
        s, o, r = subject.lower(), obj.lower(), relation.lower()
        t = _edge_type(r)
        p = {"s": s, "o": o}

        flags = self.g.query("MATCH (r:Relation {name:$n}) RETURN r.functional, r.transitive", {"n": r}).result_set
        functional, transitive = (bool(flags[0][0]), bool(flags[0][1])) if flags else (False, False)

        if self.g.query(f"MATCH (:Entity {{name:$s}})-[:{t}]->(:Entity {{name:$o}}) RETURN 1 LIMIT 1", p).result_set:
            return {"verdict": "supported", "reason": "fact stored directly"}

        if transitive:
            rows = self.g.query(
                f"MATCH path = shortestPath((:Entity {{name:$s}})-[:{t}*]->(:Entity {{name:$o}})) "
                "RETURN [n IN nodes(path) | n.name]",
                p,
            ).result_set
            if rows:
                return {"verdict": "supported", "reason": "derived via transitivity", "path": rows[0][0]}

        if functional:
            known = [row[0] for row in self.g.query(
                f"MATCH (:Entity {{name:$s}})-[:{t}]->(x:Entity) RETURN x.name ORDER BY x.name", {"s": s}
            ).result_set]
            if known:
                return {"verdict": "contradicted", "reason": f"{r} is single-valued; stored value(s): {known}"}

        return {"verdict": "unknown", "reason": "no matching or conflicting facts"}

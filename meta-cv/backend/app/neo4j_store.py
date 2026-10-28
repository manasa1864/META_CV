"""Fact graph in Neo4j (Aura). Same interface as SqliteGraphStore."""
import re

from neo4j import GraphDatabase

from . import config

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _rel_type(relation: str) -> str:
    r = relation.lower()
    if not _NAME.match(r):  # relationship types can't be parameterised, so whitelist
        raise ValueError(f"invalid relation name: {relation!r}")
    return r.upper()


class Neo4jGraphStore:
    def __init__(self) -> None:
        missing = [n for n, v in (("NEO4J_URI", config.NEO4J_URI), ("NEO4J_PASSWORD", config.NEO4J_PASSWORD)) if not v]
        if missing:
            raise RuntimeError(f"Set {', '.join(missing)} in backend/.env (from the Aura credentials file)")
        self.driver = GraphDatabase.driver(config.NEO4J_URI, auth=(config.NEO4J_USER, config.NEO4J_PASSWORD))
        self.driver.verify_connectivity()
        self._run("CREATE INDEX entity_name IF NOT EXISTS FOR (e:Entity) ON (e.name)")

    def _run(self, query: str, **params):
        records, _, _ = self.driver.execute_query(query, params, database_=config.NEO4J_DATABASE)
        return records

    def define_relation(self, name: str, functional: bool = False, transitive: bool = False) -> None:
        _rel_type(name)
        self._run(
            "MERGE (r:Relation {name:$n}) SET r.functional=$f, r.transitive=$t",
            n=name.lower(), f=functional, t=transitive,
        )

    def is_functional(self, relation: str) -> bool:
        rows = self._run("MATCH (r:Relation {name:$n}) RETURN r.functional AS f", n=relation.lower())
        return bool(rows and rows[0]["f"])

    def add_fact(self, subject: str, relation: str, obj: str, source: str | None = None) -> None:
        """Relations need no prior definition: unseen ones are registered with no rules."""
        t = _rel_type(relation)
        self._run(
            f"MERGE (a:Entity {{name:$s}}) MERGE (b:Entity {{name:$o}}) "
            f"MERGE (a)-[e:{t}]->(b) "
            "ON CREATE SET e.count = 1 ON MATCH SET e.count = e.count + 1 "
            "SET e.source = coalesce($src, e.source), e.verified_at = datetime() "
            "MERGE (r:Relation {name:$r}) ON CREATE SET r.functional = false, r.transitive = false",
            s=subject.lower(), o=obj.lower(), r=relation.lower(), src=source,
        )

    def connections(self, a: str, b: str) -> list[dict]:
        """Every edge between two entities, in either direction."""
        rows = self._run(
            "MATCH (x:Entity {name:$a})-[e]-(y:Entity {name:$b}) "
            "RETURN type(e) AS relation, startNode(e).name AS source, endNode(e).name AS target, "
            "e.count AS count, e.source AS evidence",
            a=a.lower(), b=b.lower(),
        )
        return [dict(r) for r in rows]

    def facts_about(self, entity: str, limit: int = 50) -> list[dict]:
        """Facts touching an entity: exact name first, partial match as a fallback."""
        e = entity.lower().strip()
        q = (
            "MATCH (a:Entity)-[r]->(b:Entity) WHERE {cond} "
            "RETURN a.name AS subject, toLower(type(r)) AS relation, b.name AS object LIMIT $limit"
        )
        rows = self._run(q.format(cond="(a.name = $e OR b.name = $e)"), e=e, limit=limit)
        if not rows and len(e) >= 3:
            rows = self._run(q.format(cond="(a.name CONTAINS $e OR b.name CONTAINS $e)"), e=e, limit=limit)
        return [dict(r) for r in rows]

    def relation_stats(self) -> list[dict]:
        """Usage per relation, plus rules the data suggests (never applied automatically)."""
        rows = self._run(
            "MATCH (a:Entity)-[e]->(b:Entity) "
            "WITH type(e) AS rel, a, count(DISTINCT b) AS outdeg "
            "RETURN rel, count(a) AS subjects, sum(outdeg) AS edges, max(outdeg) AS max_objects_per_subject "
            "ORDER BY edges DESC"
        )
        flags = {r["n"]: r for r in self._run("MATCH (r:Relation) RETURN r.name AS n, r.functional AS f, r.transitive AS t")}
        out = []
        for r in rows:
            name = r["rel"].lower()
            f = flags.get(name)
            out.append({
                "relation": name,
                "edges": r["edges"],
                "subjects": r["subjects"],
                "functional": bool(f and f["f"]),
                "transitive": bool(f and f["t"]),
                "looks_functional": r["max_objects_per_subject"] == 1 and r["subjects"] >= 2,
            })
        return out

    def validate(self, subject: str, relation: str, obj: str) -> dict:
        s, o, r = subject.lower(), obj.lower(), relation.lower()
        t = _rel_type(r)

        flags = self._run("MATCH (r:Relation {name:$n}) RETURN r.functional AS f, r.transitive AS t", n=r)
        functional, transitive = (bool(flags[0]["f"]), bool(flags[0]["t"])) if flags else (False, False)

        if self._run(f"MATCH (:Entity {{name:$s}})-[:{t}]->(:Entity {{name:$o}}) RETURN 1 LIMIT 1", s=s, o=o):
            return {"verdict": "supported", "reason": "fact stored directly"}

        if transitive:
            rows = self._run(
                f"MATCH path = shortestPath((:Entity {{name:$s}})-[:{t}*]->(:Entity {{name:$o}})) "
                "RETURN [n IN nodes(path) | n.name] AS p",
                s=s, o=o,
            )
            if rows:
                return {"verdict": "supported", "reason": "derived via transitivity", "path": rows[0]["p"]}

        if functional:
            known = [rec["n"] for rec in self._run(
                f"MATCH (:Entity {{name:$s}})-[:{t}]->(x:Entity) RETURN x.name AS n ORDER BY n", s=s
            )]
            if known:
                return {"verdict": "contradicted", "reason": f"{r} is single-valued; stored value(s): {known}"}

        linked = sorted({c["relation"].lower() for c in self.connections(s, o)})
        if linked:
            return {"verdict": "unknown", "reason": f"{s} and {o} are connected, but not by {r}", "related_via": linked}
        return {"verdict": "unknown", "reason": "no matching or conflicting facts"}

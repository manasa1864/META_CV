"""Fact graph in Neo4j AuraDB - the production store. Same contract as SqliteGraphStore.

Schema
  (:Entity {name})                          unique, normalised name
  (:Entity)-[:WRITTEN_BY]->(:Entity)        one relationship type per relation, carrying
                                            source, sources, count, verified_at, added_by, batch_id
  (:Relation {name, functional, transitive, acyclic})
  (:EntityAlias {name})-[:ALIAS_OF]->(:Entity)

Labels are configurable: tests, benchmark fixtures and scale runs use their own labels or a tag
label + run_id, and delete only what they created. One driver is shared by the process, with
connection lifetime below Aura's idle cut-off, query timeouts, and bounded retry on transient errors.
"""
import logging
import random
import threading
import time
from collections import defaultdict

from neo4j import GraphDatabase, NotificationMinimumSeverity, Query, RoutingControl
from neo4j.exceptions import (AuthError, ClientError, ConnectionAcquisitionTimeoutError, ServiceUnavailable,
                              SessionExpired, TransientError)

from . import config, meter
from .engine.entities import normalize
from .graph_common import NO_FLAGS, GraphLimitError, GraphUnavailable, TTLCache, label, rel_name

log = logging.getLogger(__name__)
BATCH = 1000
_TRANSIENT = (ServiceUnavailable, SessionExpired, TransientError, ConnectionAcquisitionTimeoutError)

_driver = None
_driver_lock = threading.Lock()


class GraphQueryTimeout(RuntimeError):
    pass


def driver():
    global _driver
    with _driver_lock:
        if _driver is None:
            missing = [n for n, v in (("NEO4J_URI", config.NEO4J_URI), ("NEO4J_PASSWORD", config.NEO4J_PASSWORD)) if not v]
            if missing:
                raise RuntimeError(f"Set {', '.join(missing)} (from the Aura credentials file)")
            _driver = GraphDatabase.driver(
                config.NEO4J_URI, auth=(config.NEO4J_USER, config.NEO4J_PASSWORD),
                max_connection_lifetime=config.NEO4J_MAX_CONN_LIFETIME,
                connection_acquisition_timeout=config.NEO4J_ACQUIRE_TIMEOUT,
                connection_timeout=config.NEO4J_ACQUIRE_TIMEOUT,
                notifications_min_severity=NotificationMinimumSeverity.OFF,
            )
        return _driver


def close_driver() -> None:
    global _driver
    with _driver_lock:
        if _driver is not None:
            _driver.close()
            _driver = None


def _is_limit(e: Exception) -> bool:
    text = f"{getattr(e, 'code', '')} {e}".lower()
    return any(w in text for w in ("limit", "quota", "exceed", "forbiddenonreadonly", "storage"))


def with_retry(fn):
    """Run fn with bounded retry + backoff on transient errors. Limit and auth errors are never retried."""
    last = None
    for attempt in range(max(1, config.GRAPH_RETRIES)):
        try:
            return fn()
        except AuthError as e:
            raise GraphUnavailable("Neo4j authentication failed") from e
        except _TRANSIENT as e:
            last = e
            time.sleep(min(4.0, 0.5 * 2 ** attempt) + random.random() * 0.2)
        except ClientError as e:
            code = getattr(e, "code", "") or ""
            if "TransactionTimedOut" in code or "Timeout" in code:
                raise GraphQueryTimeout(f"query exceeded {config.NEO4J_QUERY_TIMEOUT}s") from e
            if _is_limit(e):
                raise GraphLimitError(f"the graph refused the write ({code or 'limit reached'})") from e
            raise
    raise GraphUnavailable(f"Neo4j unreachable after {config.GRAPH_RETRIES} attempts ({type(last).__name__})") from last


class Neo4jGraphStore:
    backend = "neo4j"
    read_only = False

    def __init__(self, entity_label: str = "Entity", relation_label: str = "Relation", tag: str | None = None,
                 run_id: str | None = None, verify: bool = True):
        self.E, self.R, self.A = label(entity_label), label(relation_label), label(entity_label + "Alias")
        self.tag, self.run_id = (label(tag) if tag else None), run_id
        self._rules = TTLCache(self._load_rules)
        self._aliases = TTLCache(self._load_aliases)
        if verify:
            self.ping()
            self.init_schema()

    # plumbing --------------------------------------------------------------
    def _exec(self, query: str, write: bool = False, **params):
        meter.record_graph()
        return with_retry(lambda: driver().execute_query(
            Query(query, timeout=config.NEO4J_QUERY_TIMEOUT), params, database_=config.NEO4J_DATABASE,
            routing_=RoutingControl.WRITE if write else RoutingControl.READ))

    def _run(self, query: str, write: bool = False, **params):
        return self._exec(query, write, **params)[0]

    def _autocommit(self, query: str, **params) -> None:
        """For CALL { ... } IN TRANSACTIONS, which needs an implicit transaction."""
        meter.record_graph()

        def go():
            with driver().session(database=config.NEO4J_DATABASE) as s:
                s.run(Query(query, timeout=max(60.0, config.NEO4J_QUERY_TIMEOUT)), params).consume()

        with_retry(go)

    def ping(self) -> None:
        with_retry(lambda: driver().verify_connectivity())

    def _create_tag(self, var: str) -> str:
        return f" ON CREATE SET {var}:{self.tag}, {var}.run_id = $run_id" if self.tag else ""

    def init_schema(self) -> None:
        """Idempotent: unique names (this also gives the index MERGE and lookups need)."""
        if self.E == "Entity":
            self._run("DROP INDEX entity_name IF EXISTS", write=True)  # superseded by the constraint
        for lbl in (self.E, self.R, self.A):
            try:
                self._run(f"CREATE CONSTRAINT {lbl.lower()}_name_unique IF NOT EXISTS "
                          f"FOR (n:{lbl}) REQUIRE n.name IS UNIQUE", write=True)
            except ClientError as e:
                log.warning("unique constraint on %s.name not created (%s); duplicate names exist, run app.audit",
                            lbl, getattr(e, "code", e))
        if self.tag:
            self._run(f"CREATE INDEX {self.tag.lower()}_run IF NOT EXISTS FOR (n:{self.tag}) ON (n.run_id)", write=True)

    # names -----------------------------------------------------------------
    def _load_aliases(self) -> dict:
        return {r["a"]: r["c"] for r in self._run(f"MATCH (a:{self.A})-[:ALIAS_OF]->(c:{self.E}) RETURN a.name AS a, c.name AS c")}

    def resolve(self, name: str) -> str:
        n = normalize(name)
        return self._aliases.get().get(n, n)

    def add_alias(self, alias: str, canonical: str) -> bool:
        a, c = normalize(alias), normalize(canonical)
        rows = self._run(f"MATCH (:{self.A} {{name:$a}})-[:ALIAS_OF]->(c:{self.E}) RETURN c.name AS c", a=a)
        if rows and rows[0]["c"] != c:
            return False
        self._run(f"MERGE (c:{self.E} {{name:$c}}){self._create_tag('c')} "
                  f"MERGE (a:{self.A} {{name:$a}}){self._create_tag('a')} MERGE (a)-[:ALIAS_OF]->(c)",
                  write=True, a=a, c=c, run_id=self.run_id)
        self._aliases.put(a, c)
        return True

    def entity_names(self, limit: int = 100000) -> list[str]:
        return [r["n"] for r in self._run(f"MATCH (e:{self.E}) RETURN e.name AS n LIMIT $limit", limit=limit)]

    # relations -------------------------------------------------------------
    def _load_rules(self) -> dict:
        return {r["n"]: {"functional": bool(r["f"]), "transitive": bool(r["t"]), "acyclic": bool(r["a"])}
                for r in self._run(f"MATCH (r:{self.R}) RETURN r.name AS n, r.functional AS f, "
                                   "r.transitive AS t, r.acyclic AS a")}

    def define_relation(self, name: str, functional: bool = False, transitive: bool = False,
                        acyclic: bool = False) -> None:
        name = rel_name(name)
        self._run(f"MERGE (r:{self.R} {{name:$n}}){self._create_tag('r')} "
                  "SET r.functional=$f, r.transitive=$t, r.acyclic=$a",
                  write=True, n=name, f=functional, t=transitive, a=acyclic, run_id=self.run_id)
        self._rules.put(name, {"functional": functional, "transitive": transitive, "acyclic": acyclic})

    def relation_flags(self, relation: str) -> dict:
        return dict(self._rules.get().get(str(relation).lower(), NO_FLAGS))

    def is_functional(self, relation: str) -> bool:
        return self.relation_flags(relation)["functional"]

    def relation_names(self) -> list[str]:
        return sorted(self._rules.get())

    # facts -----------------------------------------------------------------
    def add_fact(self, subject: str, relation: str, obj: str, source: str | None = None, *,
                 added_by: str | None = None, batch_id: str | None = None, valid_from: str | None = None,
                 valid_to: str | None = None) -> bool:
        """Idempotent: re-adding the same fact from the same source does not inflate `count`."""
        return self.add_facts([{"subject": subject, "relation": relation, "object": obj, "source": source,
                                "added_by": added_by, "batch_id": batch_id, "valid_from": valid_from,
                                "valid_to": valid_to}]) == 1

    def add_facts(self, rows: list[dict]) -> int:
        """UNWIND batches, one query per relation type and batch. Returns how many facts were new."""
        groups = defaultdict(list)
        for r in rows:
            groups[rel_name(r["relation"])].append({
                "s": normalize(r["subject"]), "o": normalize(r["object"]), "src": r.get("source") or "unknown",
                "by": r.get("added_by"), "batch": r.get("batch_id"),
                "vf": r.get("valid_from") or None, "vt": r.get("valid_to") or None})
        created = 0
        for rel, items in groups.items():
            for i in range(0, len(items), BATCH):
                _, summary, _ = self._exec(
                    f"UNWIND $rows AS row "
                    f"OPTIONAL MATCH (:{self.A} {{name: row.s}})-[:ALIAS_OF]->(cs:{self.E}) "
                    f"OPTIONAL MATCH (:{self.A} {{name: row.o}})-[:ALIAS_OF]->(co:{self.E}) "
                    f"WITH row, coalesce(cs.name, row.s) AS s, coalesce(co.name, row.o) AS o "
                    f"MERGE (a:{self.E} {{name: s}}){self._create_tag('a')} "
                    f"MERGE (b:{self.E} {{name: o}}){self._create_tag('b')} "
                    f"MERGE (a)-[e:{rel.upper()}]->(b) "
                    "ON CREATE SET e.source = row.src, e.sources = [row.src], e.count = 1, e.verified_at = datetime(), "
                    "e.added_by = row.by, e.batch_id = row.batch" + (", e.tag = $tag, e.run_id = $run_id" if self.tag else "") + " "
                    "ON MATCH SET e.verified_at = datetime(), e.sources = CASE WHEN row.src IN coalesce(e.sources, []) "
                    "THEN e.sources ELSE coalesce(e.sources, []) + row.src END "
                    "SET e.count = size(e.sources), e.valid_from = coalesce(row.vf, e.valid_from), "
                    "e.valid_to = coalesce(row.vt, e.valid_to)",
                    write=True, rows=items[i:i + BATCH], tag=self.tag, run_id=self.run_id)
                created += summary.counters.relationships_created
        if groups:
            self._run(f"UNWIND $rels AS n MERGE (r:{self.R} {{name: n}}) "
                      "ON CREATE SET r.functional = false, r.transitive = false, r.acyclic = false"
                      + (f", r:{self.tag}, r.run_id = $run_id" if self.tag else ""),
                      write=True, rels=list(groups), run_id=self.run_id)
            for n in groups:
                if n not in self._rules.get():
                    self._rules.put(n, dict(NO_FLAGS))
        return created

    def fact_info(self, subject: str, relation: str, obj: str) -> dict | None:
        """Provenance and validity of one stored fact: verified_at (epoch seconds), valid_from, valid_to, source."""
        rows = self._run(f"MATCH (:{self.E} {{name:$s}})-[e:{rel_name(relation).upper()}]->(:{self.E} {{name:$o}}) "
                         "RETURN e.verified_at.epochSeconds AS verified_at, e.valid_from AS valid_from, "
                         "e.valid_to AS valid_to, e.source AS source LIMIT 1",
                         s=self.resolve(subject), o=self.resolve(obj))
        return dict(rows[0]) if rows else None

    def delete_batch(self, batch_id: str) -> int:
        """Remove only the edges this batch created (nodes are kept: they may be shared)."""
        rows = self._run(f"MATCH (:{self.E})-[e {{batch_id:$b}}]->(:{self.E}) RETURN count(e) AS n", b=batch_id)
        self._autocommit(f"MATCH (:{self.E})-[e {{batch_id:$b}}]->(:{self.E}) "
                         "CALL (e) { DELETE e } IN TRANSACTIONS OF 1000 ROWS", b=batch_id)
        return rows[0]["n"] if rows else 0

    def delete_tagged(self) -> None:
        """Remove everything this tag + run_id created: its edges, then its nodes that are left unconnected."""
        if not self.tag:
            raise ValueError("delete_tagged needs a tag")
        self._autocommit("MATCH ()-[e {tag:$tag, run_id:$run_id}]->() "
                         "CALL (e) { DELETE e } IN TRANSACTIONS OF 1000 ROWS", tag=self.tag, run_id=self.run_id)
        self._autocommit(f"MATCH (a:{self.tag}:{self.A} {{run_id:$run_id}}) "
                         "CALL (a) { DETACH DELETE a } IN TRANSACTIONS OF 1000 ROWS", run_id=self.run_id)
        self._autocommit(f"MATCH (n:{self.tag} {{run_id:$run_id}}) WHERE NOT (n)--() "
                         "CALL (n) { DELETE n } IN TRANSACTIONS OF 1000 ROWS", run_id=self.run_id)
        self._rules.invalidate()
        self._aliases.invalidate()

    def drop_namespace(self) -> None:
        """For private test labels only: delete all their nodes and their constraints."""
        if self.E == "Entity":
            raise ValueError("refusing to drop the production namespace")
        for lbl in (self.E, self.R, self.A):
            self._autocommit(f"MATCH (n:{lbl}) CALL (n) {{ DETACH DELETE n }} IN TRANSACTIONS OF 1000 ROWS")
            self._run(f"DROP CONSTRAINT {lbl.lower()}_name_unique IF EXISTS", write=True)

    def counts(self) -> dict:
        ents = self._run(f"MATCH (n:{self.E}) RETURN count(n) AS n")[0]["n"]
        facts = self._run(f"MATCH (:{self.E})-[r]->(:{self.E}) RETURN count(r) AS n")[0]["n"]
        return {"entities": ents, "facts": facts}

    def db_counts(self) -> dict:
        """Whole-database totals (all labels), for plan-limit checks."""
        nodes = self._run("MATCH (n) RETURN count(n) AS n")[0]["n"]
        rels = self._run("MATCH ()-[r]->() RETURN count(r) AS n")[0]["n"]
        return {"nodes": nodes, "relationships": rels}

    def iter_facts(self, page: int = 5000):
        skip = 0
        while True:
            rows = self._run(f"MATCH (a:{self.E})-[r]->(b:{self.E}) RETURN a.name AS subject, toLower(type(r)) AS relation, "
                             "b.name AS object, r.source AS source, r.added_by AS added_by, r.batch_id AS batch_id, "
                             "r.valid_from AS valid_from, r.valid_to AS valid_to "
                             "ORDER BY elementId(r) SKIP $skip LIMIT $n", skip=skip, n=page)
            if not rows:
                return
            yield from (dict(r) for r in rows)
            skip += page

    def connections(self, a: str, b: str) -> list[dict]:
        """Every edge between two entities, in either direction."""
        rows = self._run(
            f"MATCH (x:{self.E} {{name:$a}})-[e]-(y:{self.E} {{name:$b}}) "
            "RETURN toLower(type(e)) AS relation, startNode(e).name AS source, endNode(e).name AS target, "
            "e.count AS count, e.source AS evidence",
            a=self.resolve(a), b=self.resolve(b),
        )
        return [dict(r) for r in rows]

    def facts_about(self, entity: str, limit: int = 50) -> list[dict]:
        """Exact (or alias) name via the unique index, then an indexed prefix match, then a capped CONTAINS."""
        e = self.resolve(entity)
        if not e:
            return []
        rows = self._run(
            f"CALL {{ MATCH (a:{self.E} {{name:$e}})-[r]->(b:{self.E}) RETURN a.name AS subject, type(r) AS rel, b.name AS object "
            f"UNION MATCH (a:{self.E})-[r]->(b:{self.E} {{name:$e}}) RETURN a.name AS subject, type(r) AS rel, b.name AS object }} "
            "RETURN subject, toLower(rel) AS relation, object LIMIT $limit", e=e, limit=limit)
        for cond, nodes, min_len in (("x.name STARTS WITH $e", 10, 3), ("x.name CONTAINS $e", 5, 4)):
            if rows or len(e) < min_len:
                break
            rows = self._run(
                f"MATCH (x:{self.E}) WHERE {cond} WITH x LIMIT {nodes} MATCH (x)-[r]-(y:{self.E}) "
                "RETURN DISTINCT startNode(r).name AS subject, toLower(type(r)) AS relation, endNode(r).name AS object "
                "LIMIT $limit", e=e, limit=limit)
        return [dict(r) for r in rows]

    def relation_stats(self) -> list[dict]:
        """Usage per relation (a full aggregation: for the dashboard and ingest, not per question)."""
        rows = self._run(
            f"MATCH (a:{self.E})-[e]->(b:{self.E}) "
            "WITH type(e) AS rel, a, count(DISTINCT b) AS outdeg "
            "RETURN rel, count(a) AS subjects, sum(outdeg) AS edges, max(outdeg) AS max_objects_per_subject "
            "ORDER BY edges DESC"
        )
        rules = self._rules.get()
        out = []
        for r in rows:
            name = r["rel"].lower()
            f = rules.get(name, NO_FLAGS)
            out.append({"relation": name, "edges": r["edges"], "subjects": r["subjects"],
                        "functional": f["functional"], "transitive": f["transitive"],
                        "looks_functional": r["max_objects_per_subject"] == 1 and r["subjects"] >= 2})
        return out

    def path_exists(self, a: str, relation: str, b: str) -> list[str] | None:
        """A chain of `relation` edges from a to b (bounded depth), regardless of the transitive flag."""
        a, b, t = self.resolve(a), self.resolve(b), rel_name(relation).upper()
        if a == b:
            rows = self._run(f"MATCH (x:{self.E} {{name:$a}})-[:{t}]->(x) RETURN 1 LIMIT 1", a=a)
            return [a, a] if rows else None
        try:
            rows = self._run(f"MATCH (x:{self.E} {{name:$a}}), (y:{self.E} {{name:$b}}) "
                             f"MATCH p = shortestPath((x)-[:{t}*..{config.PATH_MAX_DEPTH}]->(y)) "
                             "RETURN [n IN nodes(p) | n.name] AS p", a=a, b=b)
        except GraphQueryTimeout:
            return None
        return rows[0]["p"] if rows else None

    def similar_names(self, name: str, limit: int = 50) -> list[str]:
        """Existing names sharing the first token (indexed prefix match; the duplicate-detection block)."""
        n = normalize(name)
        if not n:
            return []
        return [r["n"] for r in self._run(f"MATCH (e:{self.E}) WHERE e.name STARTS WITH $p RETURN e.name AS n LIMIT $limit",
                                          p=n.split()[0], limit=limit)]

    def validate(self, subject: str, relation: str, obj: str) -> dict:
        s, r, o = self.resolve(subject), rel_name(relation), self.resolve(obj)
        t = r.upper()
        flags = self.relation_flags(r)

        if self._run(f"MATCH (:{self.E} {{name:$s}})-[:{t}]->(:{self.E} {{name:$o}}) RETURN 1 LIMIT 1", s=s, o=o):
            return {"verdict": "supported", "reason": "fact stored directly"}

        note = ""
        if flags["transitive"] and s != o:
            try:
                rows = self._run(
                    f"MATCH (a:{self.E} {{name:$s}}), (b:{self.E} {{name:$o}}) "
                    f"MATCH p = shortestPath((a)-[:{t}*..{config.PATH_MAX_DEPTH}]->(b)) "
                    "RETURN [n IN nodes(p) | n.name] AS p", s=s, o=o)
                if rows:
                    return {"verdict": "supported", "reason": "derived via transitivity", "path": rows[0]["p"]}
            except GraphQueryTimeout:
                note = " (transitive path search timed out)"

        if flags["functional"]:
            known = [rec["n"] for rec in self._run(
                f"MATCH (:{self.E} {{name:$s}})-[:{t}]->(x:{self.E}) RETURN x.name AS n ORDER BY n", s=s)]
            if known:
                return {"verdict": "contradicted", "reason": f"{r} is single-valued; stored value(s): {known}"}

        linked = sorted({c["relation"] for c in self.connections(s, o)})
        if linked:
            return {"verdict": "unknown", "reason": f"{s} and {o} are connected, but not by {r}{note}", "related_via": linked}
        return {"verdict": "unknown", "reason": f"no matching or conflicting facts{note}"}

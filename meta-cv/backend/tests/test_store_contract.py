"""One contract, two stores: the same assertions run against SqliteGraphStore and Neo4jGraphStore.

The Neo4j variant runs only when Neo4j credentials are configured (and NEO4J_TESTS is not 0). It
works inside private labels (T<run>Entity / T<run>Relation) that are deleted afterwards, so it
never reads or changes real data.
"""
import pytest

def test_relation_rules_round_trip(store):
    store.define_relation("Born_In", functional=True)
    store.define_relation("located_in", transitive=True, acyclic=True)
    assert store.relation_flags("born_in") == {"functional": True, "transitive": False, "acyclic": False}
    assert store.relation_flags("located_in") == {"functional": False, "transitive": True, "acyclic": True}
    assert store.relation_flags("never_defined") == {"functional": False, "transitive": False, "acyclic": False}
    assert store.is_functional("born_in") and not store.is_functional("located_in")
    store.add_fact("a", "written_by", "b")
    assert {"born_in", "located_in", "written_by"} <= set(store.relation_names())


def test_invalid_relation_names_are_refused(store):
    with pytest.raises(ValueError):
        store.add_fact("a", "bad name; DROP", "b")
    with pytest.raises(ValueError):
        store.validate("a", "x)-[:Y", "b")


def test_add_fact_is_idempotent_and_normalises_names(store):
    assert store.add_fact("J. K. Rowling", "born_in", "Yate", "seed") is True
    assert store.add_fact("j.k. rowling", "born_in", "yate", "seed") is False
    assert store.add_fact("J K Rowling", "born_in", "Yate", "other source") is False
    assert store.counts()["facts"] == 1
    assert store.facts_about("J. K. Rowling") == [{"subject": "j k rowling", "relation": "born_in", "object": "yate"}]


def test_validate_verdicts(store):
    store.define_relation("born_in", functional=True)
    store.define_relation("located_in", transitive=True)
    store.add_fact("jane austen", "born_in", "steventon")
    store.add_fact("warsaw", "located_in", "poland")
    store.add_fact("poland", "located_in", "europe")
    store.add_fact("jane austen", "wrote", "emma")
    assert store.validate("Jane Austen", "born_in", "Steventon")["verdict"] == "supported"
    v = store.validate("jane austen", "born_in", "bath")
    assert v["verdict"] == "contradicted" and "steventon" in v["reason"]
    v = store.validate("warsaw", "located_in", "europe")
    assert v["verdict"] == "supported" and v["path"] == ["warsaw", "poland", "europe"]
    assert store.validate("jane austen", "lived_in", "emma")["related_via"] == ["wrote"]
    assert store.validate("jane austen", "born_in", "emma")["verdict"] == "contradicted"
    assert store.validate("x", "located_in", "y")["verdict"] == "unknown"


def test_transitive_search_is_depth_bounded(store):
    store.define_relation("part_of", transitive=True)
    chain = [f"n{i}" for i in range(6)]
    for a, b in zip(chain, chain[1:]):
        store.add_fact(a, "part_of", b)
    assert store.validate("n0", "part_of", "n3")["verdict"] == "supported"   # 3 hops = PATH_MAX_DEPTH
    assert store.validate("n0", "part_of", "n5")["verdict"] == "unknown"     # 5 hops: beyond the bound
    assert store.path_exists("n1", "part_of", "n3") == ["n1", "n2", "n3"]
    assert store.path_exists("n3", "part_of", "n1") is None


def test_lookup_exact_then_prefix(store):
    store.add_fact("pride and prejudice", "written_by", "jane austen")
    store.add_fact("elizabeth bennet", "character_in", "pride and prejudice")
    assert len(store.facts_about("Pride and Prejudice")) == 2
    assert store.facts_about("pride and") != []          # prefix fallback
    assert store.facts_about("zz") == []
    assert "pride and prejudice" in store.similar_names("Pride")


def test_aliases(store):
    store.add_fact("jane austen", "born_in", "steventon")
    assert store.add_alias("J. Austen", "Jane Austen") is True
    assert store.resolve("j austen") == "jane austen"
    assert store.validate("J Austen", "born_in", "steventon")["verdict"] == "supported"
    assert store.add_alias("j austen", "joan austen") is False   # ambiguous: already points elsewhere
    store.add_fact("J. Austen", "wrote", "emma")                  # written under the canonical name
    assert {f["subject"] for f in store.facts_about("jane austen")} == {"jane austen"}


def test_connections_and_stats(store):
    store.add_fact("a", "likes", "b")
    store.add_fact("b", "knows", "a")
    rel = sorted(c["relation"] for c in store.connections("a", "b"))
    assert rel == ["knows", "likes"]
    stats = {s["relation"]: s for s in store.relation_stats()}
    assert stats["likes"]["edges"] == 1 and stats["likes"]["subjects"] == 1


def test_delete_batch_removes_only_that_batch(store):
    store.add_fact("a", "likes", "b", "x", batch_id="keep")
    store.add_fact("c", "likes", "d", "x", batch_id="drop")
    assert store.delete_batch("drop") == 1
    assert store.validate("a", "likes", "b")["verdict"] == "supported"
    assert store.validate("c", "likes", "d")["verdict"] == "unknown"


def test_validity_periods_round_trip(store):
    import time

    before = time.time() - 5
    store.add_fact("alice", "ceo_of", "acme corp", "filing", valid_from="2015", valid_to="2021-06")
    store.add_fact("bob", "ceo_of", "acme corp", "filing", valid_from="2021-06")
    info = store.fact_info("Alice", "ceo_of", "Acme Corp")
    assert (info["valid_from"], info["valid_to"], info["source"]) == ("2015", "2021-06", "filing")
    assert info["verified_at"] >= before
    assert store.fact_info("bob", "ceo_of", "acme corp")["valid_to"] is None
    store.add_fact("alice", "ceo_of", "acme corp", "filing")              # re-adding without dates keeps them
    assert store.fact_info("alice", "ceo_of", "acme corp")["valid_to"] == "2021-06"
    assert store.fact_info("carol", "ceo_of", "acme corp") is None

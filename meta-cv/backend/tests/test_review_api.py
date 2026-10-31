"""The Review page's API: what is waiting (review queue, quarantine, rule proposals), how writes are allowed
(/status "writes"), and that each action really changes the graph."""
import time

from fastapi.testclient import TestClient

from app import config, main
from app.db import connect
from app.graph_store import SqliteGraphStore


def _client(monkeypatch, store):
    monkeypatch.setattr(main, "store", store)
    return TestClient(main.app)


def _store():
    s = SqliteGraphStore()
    s.add_fact("pride and prejudice", "written_by", "jane austen", "seed")
    s.add_fact("warsaw", "located_in", "poland", "seed")
    return s


def test_status_says_how_admin_writes_work(monkeypatch):
    with _client(monkeypatch, _store()) as c:
        monkeypatch.setattr(config, "ADMIN_TOKEN", "")
        monkeypatch.setattr(config, "JUDGE_MODE", False)
        assert c.get("/status").json()["writes"] == "open"
        monkeypatch.setattr(config, "JUDGE_MODE", True)
        assert c.get("/status").json()["writes"] == "closed"
        monkeypatch.setattr(config, "ADMIN_TOKEN", "s3cret")
        assert c.get("/status").json()["writes"] == "token"


def test_review_actions_change_the_graph(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TOKEN", "")
    monkeypatch.setattr(config, "JUDGE_MODE", False)
    store = _store()
    now = time.time()
    with connect() as conn:
        conn.execute("INSERT INTO review_queue(subject, relation, object, source, reason, created_at) VALUES (?,?,?,?,?,?)",
                     ("emma", "written_by", "jane austen", "chat", "confirmed by Wikidata Q1", now))
        conn.execute("INSERT INTO review_queue(subject, relation, object, source, reason, created_at) VALUES (?,?,?,?,?,?)",
                     ("emma", "written_by", "warsaw", "chat", "model-verified, not in graph", now))
        conn.execute("INSERT INTO rule_proposals(relation, functional, transitive, confidence, rationale, status, created_at) "
                     "VALUES ('capital_of', 1, 0, 0.7, 'one capital', 'pending', ?)", (now,))
    with _client(monkeypatch, store) as c:
        good, bad = c.get("/review").json()
        assert c.post(f"/review/{good['id']}/accept").json()["status"] == "accepted"
        assert store.validate("emma", "written_by", "jane austen")["verdict"] == "supported"
        res = c.post(f"/review/{bad['id']}/accept").json()                      # the write gate still applies
        assert res["status"] == "quarantined" and "'warsaw' is a place" in res["reason"]
        assert c.get("/review").json() == []
        (q,) = c.get("/quarantine").json()
        assert c.delete(f"/quarantine/{q['id']}").json()["status"] == "discarded" and c.get("/quarantine").json() == []
        r = c.post("/rules/capital_of/approve", json={"functional": True, "transitive": False}).json()
        assert r["status"] == "approved" and store.is_functional("capital_of") and c.get("/rules/pending").json() == []


def test_writes_need_the_token_when_one_is_set(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TOKEN", "s3cret")
    with connect() as conn:
        conn.execute("INSERT INTO review_queue(subject, relation, object, source, reason, created_at) VALUES (?,?,?,?,?,?)",
                     ("emma", "written_by", "jane austen", "chat", "x", time.time()))
    with _client(monkeypatch, _store()) as c:
        (item,) = c.get("/review").json()                                       # reading needs no token
        assert c.post(f"/review/{item['id']}/accept").status_code == 403
        assert c.post(f"/review/{item['id']}/accept", headers={"X-Admin-Token": "s3cret"}).json()["status"] == "accepted"

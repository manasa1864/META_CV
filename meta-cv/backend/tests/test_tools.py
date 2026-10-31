"""The command-line tools at toy sizes, on SQLite, writing into temporary folders: their code paths run,
but nothing here is a measurement."""
import json

from app import bench_taxonomy, config, export_snapshot, loadtest, preflight, scale, seed
from app.graph_store import SqliteGraphStore


def test_scale_generator_is_deterministic_and_injects_known_errors():
    a = scale.generate(2000, seed=7, depth=6, error_rate=0.01)
    b = scale.generate(2000, seed=7, depth=6, error_rate=0.01)
    assert a["facts"] == b["facts"] and len(a["facts"]) >= 2000
    assert all(len(v) > 0 for v in a["truth"].values())


def test_scale_runs_on_sqlite_and_measures_audit_recall(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path)
    res = scale.main(["--backend", "sqlite", "--sizes", "400", "--error-rate", "0.02", "--llm-latency", "0"])
    row = res["rows"][0]
    assert row["size"] == 400 and row["validate_supported"]["n"] == 50 and row["factual_tier"]["n"] == 5
    recall = row["audit_recall"]
    assert recall["functional_violation"]["recall"] == 1.0 and recall["incompatible_pair"]["recall"] == 1.0
    assert recall["cycle"]["recall"] == 1.0 and recall["near_duplicate"]["recall"] == 1.0
    assert list(tmp_path.glob("scale-*.csv"))


def test_loadtest_has_no_lock_errors(tmp_path, monkeypatch):
    from app import llm

    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(llm, "chat_json", llm.chat_json)   # restored after the tool patches it
    monkeypatch.setattr(llm, "chat_text", llm.chat_text)
    monkeypatch.setattr(config, "DB_PATH", config.DB_PATH)
    res = loadtest.main(["--conversations", "8", "--turns", "2", "--latency", "0"])
    assert res["pass"] and res["answered"] == 16 and res["database_locked"] == 0


def test_taxonomy_eval_writes_results(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RESULTS_DIR", tmp_path)
    res = bench_taxonomy.main(["--fallback-only"])
    assert res["headline_set"] == "taxonomy_holdout2.json" and set(res["sets"]) >= {"taxonomy_cases.json", "taxonomy_holdout.json"}
    assert res["by_source"]["fallback"]["n"] == res["n"]
    assert json.loads(next(tmp_path.glob("taxonomy-*.json")).read_text())["n"] == res["n"]


def test_preflight_local_and_snapshot_export(tmp_path, monkeypatch):
    s = SqliteGraphStore()
    monkeypatch.setattr("app.graph_store.get_store", lambda: s)
    assert preflight.main(["--seed"]) == 0

    class FakeNeo:   # export reads through these three calls only
        def _load_rules(self):
            return {"born_in": {"functional": True, "transitive": False, "acyclic": False}}

        def iter_facts(self):
            yield {"subject": "jane austen", "relation": "born_in", "object": "steventon", "source": "seed"}

        def _load_aliases(self):
            return {"j austen": "jane austen"}

    out = export_snapshot.export(FakeNeo(), str(tmp_path / "snap.db"))
    snap = SqliteGraphStore(out["path"], read_only=True)
    assert out["facts"] == 1 and snap.validate("J. Austen", "born_in", "bath")["verdict"] == "contradicted"
    assert seed.BATCH_ID == "seed-v1"

import os
import sys
import tempfile
from pathlib import Path

# Must be set before app.config is imported; load_dotenv does not override these.
os.environ["GRAPH_BACKEND"] = "sqlite"
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["RESULTS_DIR"] = os.path.join(tempfile.mkdtemp(), "results")
os.environ["REPLAYS_DIR"] = os.path.join(tempfile.mkdtemp(), "replays")
os.environ["GRAPH_SNAPSHOT"] = os.path.join(tempfile.mkdtemp(), "no_snapshot.db")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from app import audit, db  # noqa: E402
from app.engine.health import health  # noqa: E402

# The guardrail's claim extractor: by default the composed text asserts nothing beyond what the
# lexical check already covers. Tests that exercise the guard script it explicitly.
GUARD_EXTRACT = "List the atomic factual claims"
# Compression (c): by default a similar question's facts are "not enough on their own", so tests that do not
# exercise compression run the full procedure; compression tests script it explicitly.
STATE_ANSWER = "Answer the question using ONLY the numbered verified facts"
# Graph-first: by default a question is not split into parts, so tests that do not exercise it see the plain
# draft-and-verify walk; graph-first tests script the plan explicitly.
PLAN = "Split the question into the facts it asks for"


@pytest.fixture(autouse=True)
def fresh_db():
    for suffix in ("", "-wal", "-shm"):
        Path(os.environ["DB_PATH"] + suffix).unlink(missing_ok=True)
    db.init_db()
    health.reset()
    audit.clear_cache()
    yield


class FakeLLM:
    """Routes each call by its system prompt to a scripted handler: handler(user, temperature) -> value."""

    def __init__(self, monkeypatch):
        self.handlers = {}
        self.defaults = {GUARD_EXTRACT: lambda u, t: {"claims": []}, STATE_ANSWER: lambda u, t: {"answerable": False},
                         PLAN: lambda u, t: {"parts": []}}
        self.calls = []
        self.prompts = {}
        from app import llm

        monkeypatch.setattr(llm, "chat_json", self._call)
        monkeypatch.setattr(llm, "chat_text", self._call)

    def on(self, system_prefix: str, handler):
        self.handlers[system_prefix] = handler

    def _call(self, system, user, temperature=0):
        from app import llm

        for table in (self.handlers, self.defaults):
            for prefix, h in table.items():
                if system.startswith(prefix):
                    llm.before_llm_call()
                    self.calls.append(prefix)
                    self.prompts.setdefault(prefix, []).append(user)
                    out = h(user, temperature)
                    llm.record_usage(llm.estimate_tokens(system + user), llm.estimate_tokens(str(out)))
                    return out
        raise AssertionError(f"unexpected LLM call: {system[:60]!r}")


@pytest.fixture
def fake_llm(monkeypatch):
    return FakeLLM(monkeypatch)


def _neo4j_ok() -> bool:
    from app import config

    return bool(config.NEO4J_URI and config.NEO4J_PASSWORD) and os.getenv("NEO4J_TESTS", "1") != "0"


@pytest.fixture(params=["sqlite", pytest.param("neo4j", marks=pytest.mark.skipif(not _neo4j_ok(), reason="no Neo4j credentials"))])
def store(request, monkeypatch):
    """The same graph contract on both stores. The Neo4j variant runs only with credentials configured, inside
    private labels (T<run>Entity / T<run>Relation) that are deleted afterwards - real data is never touched."""
    import uuid

    from app import config
    from app.graph_store import SqliteGraphStore

    monkeypatch.setattr(config, "PATH_MAX_DEPTH", 3)
    if request.param == "sqlite":
        yield SqliteGraphStore()
        return
    from app.neo4j_store import Neo4jGraphStore

    s = Neo4jGraphStore(entity_label=f"T{uuid.uuid4().hex[:8]}Entity", relation_label=f"T{uuid.uuid4().hex[:8]}Relation")
    try:
        yield s
    finally:
        s.drop_namespace()

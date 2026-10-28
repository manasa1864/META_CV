import os
import sys
import tempfile
from pathlib import Path

# Must be set before app.config is imported; load_dotenv does not override these.
os.environ["GRAPH_BACKEND"] = "sqlite"
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from app import db  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    Path(os.environ["DB_PATH"]).unlink(missing_ok=True)
    db.init_db()
    yield


class FakeLLM:
    """Routes each call by its system prompt to a scripted handler: handler(user, temperature) -> value."""

    def __init__(self, monkeypatch):
        self.handlers = {}
        self.calls = []
        self.prompts = {}
        from app import llm

        monkeypatch.setattr(llm, "chat_json", self._call)
        monkeypatch.setattr(llm, "chat_text", self._call)

    def on(self, system_prefix: str, handler):
        self.handlers[system_prefix] = handler

    def _call(self, system, user, temperature=0):
        from app import llm

        for prefix, h in self.handlers.items():
            if system.startswith(prefix):
                self.calls.append(prefix)
                self.prompts.setdefault(prefix, []).append(user)
                out = h(user, temperature)
                llm.record_usage(llm.estimate_tokens(system + user), llm.estimate_tokens(str(out)))
                return out
        raise AssertionError(f"unexpected LLM call: {system[:60]!r}")


@pytest.fixture
def fake_llm(monkeypatch):
    return FakeLLM(monkeypatch)

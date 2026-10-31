"""Demo backend: real pipeline + seeded SQLite graph, with a scripted stand-in for the LLM (no API key needed).

The UI labels everything from this server "Scripted demo (no model)": it is never presented as a live run.
"""
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ["GRAPH_BACKEND"] = "sqlite"
os.environ["DB_PATH"] = str(HERE / "demo.db")
os.environ["SCRIPTED_DEMO"] = "1"
os.environ["META_NO_HOME_ENV"] = "1"
for suffix in ("", "-wal", "-shm"):
    Path(os.environ["DB_PATH"] + suffix).unlink(missing_ok=True)
sys.path.insert(0, str(HERE / "backend"))

from app import llm  # noqa: E402


def script(system, user, temperature=0):
    time.sleep(0.4)  # feel like a model call
    if system.startswith("Profile the hallucination risks"):
        q = user.split("Question:")[-1]
        if "Conversation so far" in user:
            return {"risks": [{"label": "faithful.context", "weight": 0.9}], "entities": ["pride and prejudice"], "why": "demo"}
        mathy = any(ch.isdigit() for ch in q)
        return {"risks": [{"label": "faithful.logic" if mathy else "factual.entity_error", "weight": 0.9}],
                "entities": ["pride and prejudice"], "why": "demo"}
    if system.startswith("List the atomic factual claims"):
        return {"claims": []}
    if system.startswith("Answer the question using ONLY the numbered verified facts"):
        return {"answerable": False}
    if system.startswith("The user asks a follow-up"):
        follow_up = user.split("Follow-up:")[-1].strip()
        return {"answerable": False, "standalone": follow_up,
                "standalone_type": "reasoning" if any(ch.isdigit() for ch in follow_up) else "factual"}
    if system.startswith("Split the question into the facts it asks for"):
        q = user.split("Question:")[-1].lower()
        if "pride and prejudice" in q and "wrote" in q:  # the multi-fact example: answered from the graph alone
            return {"parts": [{"subject": "pride and prejudice", "relation": "written_by", "object": "?"},
                              {"subject": "pride and prejudice", "relation": "published_in", "object": "?"},
                              {"subject": "#1", "relation": "born_in", "object": "?"}]}
        return {"parts": []}
    if system.startswith("Answer the question as a chain"):
        return {"claims": [
            {"subject": "pride and prejudice", "relation": "written_by", "object": "jane austen", "depends_on": []},
            {"subject": "jane austen", "relation": "born_in", "object": "bath", "depends_on": [0]},
            {"subject": "elizabeth bennet", "relation": "character_in", "object": "pride and prejudice", "depends_on": []},
            {"subject": "elizabeth bennet", "relation": "marries", "object": "fitzwilliam darcy", "depends_on": [2]},
            {"subject": "mr collins", "relation": "character_in", "object": "pride and prejudice", "depends_on": []},
            {"subject": "mr wickham", "relation": "character_in", "object": "emma", "depends_on": []},
            {"subject": "mr wickham", "relation": "elopes_with", "object": "lydia bennet", "depends_on": [5]},
        ]}
    if system.startswith("Judge whether"):
        return {"verdict": "false" if "emma" in user else "true", "confidence": 0.9}
    if system.startswith("Answer from your own knowledge"):
        return {"answers": ["Pride and Prejudice"]}
    if system.startswith("Write the answer"):
        return ("Pride and Prejudice was written by Jane Austen, who was born in Steventon. Its heroine, Elizabeth "
                "Bennet, marries Fitzwilliam Darcy; Mr Collins is another character. I couldn't verify the rest.")
    if system.startswith("Solve the problem"):
        if "VERIFIED steps" in user or temperature > 0:
            return {"steps": [{"text": "Apples in all boxes", "expression": "12*7", "result": "84"},
                              {"text": "Subtract the eaten ones", "expression": "84-4", "result": "80"}],
                    "final_answer": "80"}
        return {"steps": [{"text": "Apples in all boxes", "expression": "12*7", "result": "86"},
                          {"text": "Subtract the eaten ones", "expression": "86-4", "result": "82"}],
                "final_answer": "82"}
    if system.startswith("You are checking"):
        return {"valid": True, "first_bad_step": None, "reason": ""}
    raise RuntimeError(system[:40])


def fake(system, user, temperature=0):
    out = script(system, user, temperature)
    llm.record_usage(llm.estimate_tokens(system + user), llm.estimate_tokens(str(out)))  # estimated, like a model
    return out


llm.chat_json = fake
llm.chat_text = fake

from app import seed  # noqa: E402

seed.main()

import uvicorn  # noqa: E402

from app.main import app  # noqa: E402

uvicorn.run(app, port=8000)

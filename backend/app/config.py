import os
from pathlib import Path

from dotenv import load_dotenv

# backend/.env wins; ~/.meta_cv/.env (outside the repo) fills anything it lacks, so fresh clones keep working.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")
load_dotenv(os.getenv("META_ENV_FILE") or Path.home() / ".meta_cv" / ".env")

DB_PATH = os.getenv("DB_PATH", str(Path(__file__).resolve().parent.parent / "meta.db"))

GRAPH_BACKEND = os.getenv("GRAPH_BACKEND", "sqlite").lower()


def _env(*names: str, default: str = "") -> str:
    for n in names:
        v = os.getenv(n)
        if v:
            return v.strip().strip("\"'")
    return default


NEO4J_URI = _env("NEO4J_URI")
NEO4J_USER = _env("NEO4J_USERNAME", "NEO4J_USER", default="neo4j")
NEO4J_PASSWORD = _env("NEO4J_PASSWORD")
NEO4J_DATABASE = _env("NEO4J_DATABASE", default="neo4j")

META_API_KEY = _env("META_API_KEY")
META_BASE_URL = _env("META_BASE_URL")
META_MODEL = _env("META_MODEL")

# LLM-proposed relation rules are enforced only after approval. Set a number
# (e.g. 0.95) to also auto-apply proposals at or above that confidence; leave
# empty to require manual approval for every rule.
_t = _env("RULE_AUTO_THRESHOLD")
RULE_AUTO_THRESHOLD = float(_t) if _t else None
# Extracted triples below this confidence go to the review queue.
MIN_TRIPLE_CONFIDENCE = float(_env("MIN_TRIPLE_CONFIDENCE", default="0.6"))

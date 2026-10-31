"""All settings come from environment variables (or backend/.env in development)."""
import os
from pathlib import Path

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = BACKEND_DIR.parent

# backend/.env wins; ~/.meta_cv/.env (outside the repo) fills gaps on a developer machine.
# Deployments set real environment variables and META_NO_HOME_ENV=1, so nothing depends on that file.
load_dotenv(BACKEND_DIR / ".env")
if not os.getenv("META_NO_HOME_ENV"):
    load_dotenv(os.getenv("META_ENV_FILE") or Path.home() / ".meta_cv" / ".env")


def _env(*names: str, default: str = "") -> str:
    for n in names:
        v = os.getenv(n)
        if v:
            return v.strip().strip("\"'")
    return default


def _flag(name: str, default: bool = False) -> bool:
    return _env(name, default="1" if default else "0").lower() in ("1", "true", "yes", "on")


def _num(name: str, default: float) -> float:
    v = _env(name)
    return float(v) if v else default


def _int(name: str, default: int) -> int:
    return int(_num(name, default))


DB_PATH = _env("DB_PATH", default=str(BACKEND_DIR / "meta.db"))
INITIAL_STATE_DB = _env("INITIAL_STATE_DB", default=str(PROJECT_DIR / "data" / "initial_state.db"))
GRAPH_SNAPSHOT = _env("GRAPH_SNAPSHOT", default=str(PROJECT_DIR / "data" / "graph_snapshot.db"))
RESULTS_DIR = Path(_env("RESULTS_DIR", default=str(PROJECT_DIR / "results")))
REPLAYS_DIR = Path(_env("REPLAYS_DIR", default=str(PROJECT_DIR / "replays")))
BENCH_DATA_DIR = Path(_env("BENCH_DATA_DIR", default=str(PROJECT_DIR / "bench_data")))
FRONTEND_DIST = Path(_env("FRONTEND_DIST", default=str(PROJECT_DIR / "frontend" / "dist")))

GRAPH_BACKEND = _env("GRAPH_BACKEND", default="sqlite").lower()

NEO4J_URI = _env("NEO4J_URI")
NEO4J_USER = _env("NEO4J_USERNAME", "NEO4J_USER", default="neo4j")
NEO4J_PASSWORD = _env("NEO4J_PASSWORD")
NEO4J_DATABASE = _env("NEO4J_DATABASE", default="neo4j")
NEO4J_QUERY_TIMEOUT = _num("NEO4J_QUERY_TIMEOUT", 10)          # seconds per Cypher query
NEO4J_MAX_CONN_LIFETIME = _num("NEO4J_MAX_CONN_LIFETIME", 240)  # below Aura's idle-connection cut-off
NEO4J_ACQUIRE_TIMEOUT = _num("NEO4J_ACQUIRE_TIMEOUT", 10)
GRAPH_RETRIES = _int("GRAPH_RETRIES", 3)
PATH_MAX_DEPTH = _int("PATH_MAX_DEPTH", 8)                     # transitive path search depth
RELATION_CACHE_SECONDS = _num("RELATION_CACHE_SECONDS", 60)
GRAPH_RECOVER_SECONDS = _num("GRAPH_RECOVER_SECONDS", 60)      # how often a degraded app retries Aura

META_API_KEY = _env("META_API_KEY")
META_BASE_URL = _env("META_BASE_URL")
META_MODEL = _env("META_MODEL")
# Optional second model for the independent checks (judge + blind probe). Unset: the main model checks its
# own drafts. Base URL and key default to the main ones, so a second model on the same API needs only the name.
META_JUDGE_MODEL = _env("META_JUDGE_MODEL")
META_JUDGE_BASE_URL = _env("META_JUDGE_BASE_URL")
META_JUDGE_API_KEY = _env("META_JUDGE_API_KEY")
# Outside evidence for claims the graph does not hold (engine/wikidata.py). 0 turns the lookup off.
WIKIDATA_LOOKUP = _env("WIKIDATA_LOOKUP", default="1") != "0"
WIKIDATA_TIMEOUT = _num("WIKIDATA_TIMEOUT", 6)
LLM_TIMEOUT = _num("LLM_TIMEOUT", 30)
LLM_RETRIES = _int("LLM_RETRIES", 3)
LLM_BREAKER_FAILURES = _int("LLM_BREAKER_FAILURES", 5)        # consecutive failures that open the breaker
LLM_BREAKER_COOLDOWN = _num("LLM_BREAKER_COOLDOWN", 60)

# LLM-proposed relation rules are enforced only after approval. Set a number (e.g. 0.95) to also
# auto-apply proposals at or above that confidence; leave empty to require manual approval.
_t = _env("RULE_AUTO_THRESHOLD")
RULE_AUTO_THRESHOLD = float(_t) if _t else None
MIN_TRIPLE_CONFIDENCE = _num("MIN_TRIPLE_CONFIDENCE", 0.6)
# Sources whose facts may be written without graph support; everything else 'unknown' goes to review.
TRUSTED_SOURCES = {s.strip() for s in _env("TRUSTED_SOURCES", default="seed").split(",") if s.strip()}

CLAIM_TTL_HOURS = _num("CLAIM_TTL_HOURS", 24)                  # model-verified claims expire after this
STALE_DAYS = _num("STALE_DAYS", 365)                           # audit flags facts not re-verified for longer
FRESH_DAYS = _num("FRESH_DAYS", 30)                            # questions about the present: older facts get a date note
STATE_MAX_ROWS = _int("STATE_MAX_ROWS", 5000)                  # cap on reasoning_states (least-hit pruned)
STATE_CACHE_MAX_AGE_DAYS = _num("STATE_CACHE_MAX_AGE_DAYS", 30)
TURNS_RETENTION_DAYS = _num("TURNS_RETENTION_DAYS", 0)         # 0 keeps every turn
AUDIT_CACHE_SECONDS = _num("AUDIT_CACHE_SECONDS", 300)

# Token compression (engine/compression.py): trimmed prompts, answering from a similar question's verified
# facts, and adapting a similar verified calculation instead of re-solving it from scratch.
COMPRESSION = _flag("COMPRESSION", True)
COMPRESS_MIN_SIMILARITY = _num("COMPRESS_MIN_SIMILARITY", 0.6)

# Router: score = P(correct) - ROUTER_LAMBDA * P(hallucinated) - ABSTAIN_COST * P(abstained)
ROUTER_FROZEN = _flag("ROUTER_FROZEN")
ROUTER_LAMBDA = _num("ROUTER_LAMBDA", 1.0)
ABSTAIN_COST = _num("ABSTAIN_COST", 0.1)
SELF_SIGNAL_WEIGHT = _num("SELF_SIGNAL_WEIGHT", 0.0)           # the system's own confidence; excluded by default
USER_SIGNAL_WEIGHT = _num("USER_SIGNAL_WEIGHT", 1.0)

# Per-question budget
MAX_LLM_CALLS = _int("MAX_LLM_CALLS", 40)
MAX_GRAPH_CALLS = _int("MAX_GRAPH_CALLS", 400)
MAX_TOKENS = _int("MAX_TOKENS", 60000)
CHECK_WORKERS = _int("CHECK_WORKERS", 6)   # model checks run at the same time (engine/parallel.py); 1 = one by one

# Global breaker: switch to verified-only mode when recent turns look unhealthy
BREAKER_WINDOW = _int("BREAKER_WINDOW", 20)
BREAKER_MIN_TURNS = _int("BREAKER_MIN_TURNS", 8)
BREAKER_REJECT_RATE = _num("BREAKER_REJECT_RATE", 0.6)
BREAKER_ERROR_RATE = _num("BREAKER_ERROR_RATE", 0.5)

# Set only by demo_server.py: the model is a scripted stand-in, and the UI must say so
SCRIPTED_DEMO = _flag("SCRIPTED_DEMO")

# Judge mode: read-only graph, frozen router, rate limits
JUDGE_MODE = _flag("JUDGE_MODE")
if JUDGE_MODE:
    ROUTER_FROZEN = True
ADMIN_TOKEN = _env("ADMIN_TOKEN")
RATE_LIMIT_PER_MIN = _int("RATE_LIMIT_PER_MIN", 12)
DAILY_TOKEN_BUDGET = _int("DAILY_TOKEN_BUDGET", 2_000_000)
MAX_CONCURRENT = _int("MAX_CONCURRENT", 8)

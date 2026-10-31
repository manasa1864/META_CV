"""Meta API (OpenAI-compatible) client: one shared client, timeouts, retry with backoff and jitter,
a circuit breaker, and per-question metering/budgets (meter.py)."""
import json
import random
import re
import threading
import time

import openai
from openai import OpenAI

from . import config
from .meter import metered, metering, new_meter  # noqa: F401  (re-exported for callers)
from .meter import before_llm_call, record_llm


class LLMNotConfigured(RuntimeError):
    pass


class LLMUnavailable(RuntimeError):
    """The model kept failing (or the breaker is open)."""


class LLMBadOutput(ValueError):
    pass


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def record_usage(prompt: int, completion: int) -> None:
    record_llm(prompt, completion)


def configured() -> bool:
    return bool(config.META_API_KEY and config.META_BASE_URL and config.META_MODEL)


_client_obj: OpenAI | None = None
_client_lock = threading.Lock()


def _client() -> OpenAI:
    global _client_obj
    if not configured():
        raise LLMNotConfigured("The language model is not configured (META_API_KEY, META_BASE_URL, META_MODEL)")
    with _client_lock:
        if _client_obj is None:
            _client_obj = OpenAI(api_key=config.META_API_KEY, base_url=config.META_BASE_URL,
                                 timeout=config.LLM_TIMEOUT, max_retries=0)
        return _client_obj


class Breaker:
    """Opens after N consecutive failures; while open, calls fail fast until the cooldown passes."""

    def __init__(self):
        self.failures, self.opened_at, self._lock = 0, 0.0, threading.Lock()

    def is_open(self) -> bool:
        return self.failures >= config.LLM_BREAKER_FAILURES and time.time() - self.opened_at < config.LLM_BREAKER_COOLDOWN

    def success(self) -> None:
        with self._lock:
            self.failures = 0

    def failure(self) -> None:
        with self._lock:
            self.failures += 1
            if self.failures >= config.LLM_BREAKER_FAILURES:
                self.opened_at = time.time()


breaker = Breaker()
_RETRYABLE = (openai.APITimeoutError, openai.APIConnectionError, openai.RateLimitError, openai.InternalServerError)


def status() -> dict:
    return {"configured": configured(), "breaker_open": breaker.is_open()}


def _complete(system: str, user: str, temperature: float):
    if breaker.is_open():
        raise LLMUnavailable("the language model is temporarily unavailable")
    last = None
    for attempt in range(max(1, config.LLM_RETRIES)):
        try:
            resp = _client().chat.completions.create(
                model=config.META_MODEL, temperature=temperature,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            )
            breaker.success()
            return resp
        except _RETRYABLE as e:
            last = e
        except openai.APIStatusError as e:
            if e.status_code < 500:
                raise
            last = e
        time.sleep(min(8.0, 0.5 * 2 ** attempt) + random.random() * 0.3)
    breaker.failure()
    raise LLMUnavailable(f"the language model failed after {config.LLM_RETRIES} attempts") from last


def _parse_json(text: str):
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start = min((i for i in (text.find("{"), text.find("[")) if i != -1), default=-1)
    if start == -1:
        raise LLMBadOutput("model did not return JSON")
    try:
        return json.JSONDecoder().raw_decode(text[start:])[0]
    except json.JSONDecodeError as e:
        raise LLMBadOutput(f"model returned invalid JSON: {e}") from e


def chat_text(system: str, user: str, temperature: float = 0) -> str:
    before_llm_call()
    resp = _complete(system, user, temperature)
    text = resp.choices[0].message.content or ""
    u = getattr(resp, "usage", None)
    if u is not None and u.prompt_tokens is not None:
        record_usage(u.prompt_tokens, u.completion_tokens or 0)
    else:
        record_usage(estimate_tokens(system + user), estimate_tokens(text))
    return text


def chat_json(system: str, user: str, temperature: float = 0):
    return _parse_json(chat_text(system, user, temperature))


def normalize_relation(name: str) -> str | None:
    r = re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")[:64]
    if r and not r[0].isalpha():
        r = "r_" + r
    return r or None


def _confidence(v) -> float:
    try:
        return min(1.0, max(0.0, float(v)))
    except (TypeError, ValueError):
        return 0.0


EXTRACT_SYSTEM = """Extract facts from the text as subject-relation-object triples.
Rules:
- Only facts the text explicitly states. Never infer, guess, or add outside knowledge.
- subject and object are short entity names. relation is a snake_case verb phrase (e.g. works_at, capital_of).
- Reuse a relation from the provided list when it means the same thing instead of inventing a synonym.
- confidence is 0-1: how clearly the text states the fact.
Return only JSON: {"triples": [{"subject": "...", "relation": "...", "object": "...", "confidence": 0.0}]}"""


def extract_triples(text: str, known_relations: list[str]) -> list[dict]:
    data = chat_json(EXTRACT_SYSTEM, f"Existing relations: {sorted(known_relations)}\n\nText:\n{text}")
    raw = data.get("triples") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        raise LLMBadOutput("expected a list of triples")
    out = []
    for t in raw:
        if not isinstance(t, dict):
            continue
        s, r, o = (str(t.get(k, "")).strip() for k in ("subject", "relation", "object"))
        rel = normalize_relation(r)
        if s and o and rel:
            out.append({"subject": s, "relation": rel, "object": o, "confidence": _confidence(t.get("confidence"))})
    return out


RULES_SYSTEM = """You judge the logical properties of a relation name used in a knowledge graph.
- functional: a subject can have AT MOST ONE object at a time (capital_of, born_in, has_ssn). False if a subject can legitimately have several (works_at, friend_of, speaks).
- transitive: a-r->b and b-r->c always imply a-r->c (is_a, part_of, ancestor_of). False otherwise (friend_of, parent_of).
Be conservative: if unsure, answer false with low confidence. A wrong 'true' makes the system reject valid facts.
Return only JSON: {"functional": false, "transitive": false, "confidence": 0.0, "rationale": "one sentence"}"""


def propose_relation_rules(relation: str) -> dict:
    d = chat_json(RULES_SYSTEM, f"Relation: {relation}")
    if not isinstance(d, dict):
        raise LLMBadOutput("expected an object")
    return {
        "functional": bool(d.get("functional")),
        "transitive": bool(d.get("transitive")),
        "confidence": _confidence(d.get("confidence")),
        "rationale": str(d.get("rationale", ""))[:300],
    }

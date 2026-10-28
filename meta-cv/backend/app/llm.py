"""Thin wrapper over the Meta API (OpenAI-compatible), with per-run token metering."""
import json
import re
from contextlib import contextmanager
from contextvars import ContextVar

from openai import OpenAI

from . import config


class LLMNotConfigured(RuntimeError):
    pass


class LLMBadOutput(ValueError):
    pass


_meter: ContextVar[dict | None] = ContextVar("llm_meter", default=None)


def new_meter() -> dict:
    return {"prompt": 0, "completion": 0, "calls": 0}


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def record_usage(prompt: int, completion: int) -> None:
    m = _meter.get()
    if m is not None:
        m["prompt"] += prompt
        m["completion"] += completion
        m["calls"] += 1


@contextmanager
def metering(meter: dict):
    token = _meter.set(meter)
    try:
        yield meter
    finally:
        _meter.reset(token)


def metered(gen, meter: dict):
    """Drive a generator so every LLM call made inside it is counted in `meter`.

    The meter is set around each step, not once, because a streaming response may
    resume the generator from a different thread (and so a different context).
    """
    while True:
        with metering(meter):
            try:
                item = next(gen)
            except StopIteration as stop:
                return stop.value
        yield item


def _client() -> OpenAI:
    if not (config.META_API_KEY and config.META_BASE_URL and config.META_MODEL):
        raise LLMNotConfigured("Set META_API_KEY, META_BASE_URL and META_MODEL in backend/.env")
    return OpenAI(api_key=config.META_API_KEY, base_url=config.META_BASE_URL)


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
    resp = _client().chat.completions.create(
        model=config.META_MODEL,
        temperature=temperature,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
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

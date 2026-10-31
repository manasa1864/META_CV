"""Token compression: spend fewer tokens without skipping the checks that catch hallucinations.

(b) trimmed prompts   the claim draft sees only the known facts and relation names relevant to the question
                      (ranked, capped), and follow-ups see a compact history.
(c) state answer      a factual question similar to one answered before is first tried against that
                      question's verified facts (re-validated); if they fully answer it and the answer passes
                      the faithfulness guard, no new claim chain is drafted or checked.
(a) template reuse    a calculation similar to a verified one adapts its checked steps in one solution, which
                      is machine-checked and logic-checked; the independent cross-check solutions are skipped.
                      If the adaptation fails a check, the full procedure runs as usual.
Every saving is measured, not assumed: `python -m app.bench --ablate full,no_compression`.
Off with COMPRESSION=0, or per question with the no_compression flag (benchmark ablation).
"""
from .. import config, meter
from .entities import normalize
from .states import terms

FACT_CAP = 30
RELATION_CAP = 20
HISTORY_TURNS = 4
HISTORY_ANSWER_CHARS = 200


def enabled() -> bool:
    m = meter.current()
    return config.COMPRESSION and not (m and m.get("flags", {}).get("no_compression"))


def similar_enough(prior: dict | None) -> bool:
    return bool(prior) and enabled() and prior.get("similarity", 0) >= config.COMPRESS_MIN_SIMILARITY


def rank_facts(facts: list[dict], question: str, entities: list[str], cap: int = FACT_CAP) -> list[dict]:
    """Facts about the question's own entities first, then by word overlap with the question."""
    q = terms(question)
    names = {normalize(e) for e in entities}

    def score(f):
        about = (f["subject"] in names) + (f["object"] in names)
        return (about, len(q & terms(f"{f['subject']} {f['relation'].replace('_', ' ')} {f['object']}")))

    return sorted(facts, key=score, reverse=True)[:cap]


def relevant_relations(names: list[str], facts: list[dict], question: str, cap: int = RELATION_CAP) -> list[str]:
    """Relations the known facts use, then names sharing a word with the question."""
    used = {f["relation"] for f in facts}
    q = terms(question)
    rest = [n for n in names if n not in used and q & terms(n.replace("_", " "))]
    return sorted(used)[:cap] + rest[: max(0, cap - len(used))]

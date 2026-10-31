"""Token compression: spend fewer tokens without skipping the checks that catch hallucinations,
and without losing context the answer needs.

(b) trimmed prompts   the claim draft sees the relevant known facts and relation names, and follow-ups see a
                      compact history. Guarded twice:
                        keep rules   facts established in this conversation, facts sharing a word with the
                                     question, and each named entity's most relevant facts are never cut;
                        lost-context check
                                     facts: if any drafted claim could not be verified and a cut fact or
                                            relation concerns the same entity or relation, the draft is redone
                                            untrimmed (context_lost);
                                     history: if the question mentions something that only appears in the cut
                                            part of the history, the full history is used (compact_history).
                      What was kept, cut and restored is recorded per question (final.compression.context).
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
PER_ENTITY_MIN = 5
RELATION_CAP = 20
HISTORY_TURNS = 4
HISTORY_ANSWER_CHARS = 200


def enabled() -> bool:
    m = meter.current()
    return config.COMPRESSION and not (m and m.get("flags", {}).get("no_compression"))


def similar_enough(prior: dict | None) -> bool:
    return bool(prior) and enabled() and prior.get("similarity", 0) >= config.COMPRESS_MIN_SIMILARITY


def record(**kw) -> None:
    """Note what compression kept, cut or restored for this question (reported in final.compression.context)."""
    m = meter.current()
    if m is not None:
        ctx = m.setdefault("compression", {})
        for k, v in kw.items():
            ctx[k] = ctx.get(k, 0) + v if isinstance(v, int) and not isinstance(v, bool) else v


def _fact_terms(f: dict) -> set[str]:
    return terms(f"{f['subject']} {f['relation'].replace('_', ' ')} {f['object']}")


def split_facts(facts: list[dict], question: str, entities: list[str], must_keep: list[dict] = (),
                cap: int = FACT_CAP) -> tuple[list[dict], list[dict]]:
    """(shown, cut). Never cut: must_keep (what this conversation established), facts sharing a word with the
    question, and each named entity's PER_ENTITY_MIN most relevant facts. The cap only trims the rest."""
    names = {normalize(e) for e in entities}
    # the entity's own name is in every fact about it, so it says nothing about relevance
    q = terms(question) - terms(" ".join(names))
    keep_ids = {id(f) for f in must_keep}

    def overlap(f):
        return len(q & _fact_terms(f))

    ranked = sorted(facts, key=lambda f: ((f["subject"] in names) + (f["object"] in names), overlap(f)), reverse=True)
    shown, seen = [], set()

    def take(f):
        if id(f) not in seen:
            seen.add(id(f))
            shown.append(f)

    for f in ranked:
        if id(f) in keep_ids or overlap(f):
            take(f)
    for name in names:
        for f in [f for f in ranked if name in (f["subject"], f["object"])][:PER_ENTITY_MIN]:
            take(f)
    for f in ranked:   # fill any remaining room up to the cap with the next most relevant facts
        if len(shown) >= cap:
            break
        take(f)
    order = {id(f): i for i, f in enumerate(facts)}
    shown.sort(key=lambda f: order[id(f)])
    return shown, [f for f in facts if id(f) not in seen]


def split_relations(names: list[str], shown: list[dict], question: str,
                    cap: int = RELATION_CAP) -> tuple[list[str], list[str]]:
    """(shown, cut): relations the shown facts use, then names sharing a word with the question."""
    used = sorted({f["relation"] for f in shown})
    q = terms(question)
    rest = [n for n in names if n not in used and q & terms(n.replace("_", " "))]
    keep = used + rest[: max(0, cap - len(used))]
    kept = set(keep)
    return keep, [n for n in names if n not in kept]


def context_lost(checked: list[dict], cut_facts: list[dict], cut_relations: list[str]) -> str | None:
    """Did trimming possibly hide something the answer needed? Only asked when some claim failed: claims the
    graph supports were validated against the full graph, so trimming cannot have changed them."""
    unresolved = [c for c in checked if c["status"] != "accepted"]
    if not unresolved or not (cut_facts or cut_relations):
        return None
    for c in unresolved:
        names = {normalize(c["subject"]), normalize(c["object"])}
        for f in cut_facts:
            if names & {f["subject"], f["object"]}:
                return f"a cut fact about {', '.join(sorted(names & {f['subject'], f['object']}))} may have been needed"
        rel = terms(c["relation"].replace("_", " "))
        for name in cut_relations:
            if rel & terms(name.replace("_", " ")):
                return f"the cut relation '{name}' may be the one '{c['relation']}' meant"
    return None


def compact_history(hist: list[dict], question: str = "") -> tuple[list[dict], int, bool]:
    """(turns to show, answer length, expanded). Compact unless the question mentions something that only the
    cut part of the history contains - then the full history is used."""
    full_size = 400
    if not enabled():
        return hist, full_size, False
    kept = hist[-HISTORY_TURNS:]
    shown = " ".join(f"{t['question']} {t['answer'][:HISTORY_ANSWER_CHARS]}" for t in kept)
    everything = " ".join(f"{t['question']} {t['answer'][:full_size]}" for t in hist)
    missing = (terms(question) & terms(everything)) - terms(shown)
    if missing:
        record(history_expanded=True, history_missing=", ".join(sorted(missing)[:5]))
        return hist, full_size, True
    if len(kept) < len(hist) or any(len(t["answer"]) > HISTORY_ANSWER_CHARS for t in kept):
        record(history_trimmed=True)
    return kept, HISTORY_ANSWER_CHARS, False

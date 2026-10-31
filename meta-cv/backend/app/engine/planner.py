"""Mixed-risk planning: a question that needs both facts and a calculation ("How old was Einstein
when he won the Nobel Prize?") runs the factual tier first and hands its accepted triples to the
reasoning tier as verified premises. A premise that cannot be verified is never recalled silently:
the reasoning tier may use only the premises, and with none verified the turn abstains.
"""
from . import factual, reasoning
from .router import caught
from .trace import Trace


def answer(store, question: str, entities: list[str], mode: str, trace: Trace, prior: dict | None = None,
           earlier_claims: list[dict] | None = None, earlier_answers: list[dict] | None = None,
           verified_only: bool = False, time_sensitive: bool = False):
    yield trace.note("planner", "Mixed question: facts first, then the calculation",
                     "The facts it needs are verified before any arithmetic is allowed to use them.")
    checked, found = yield from factual.walk(store, question, entities, trace, None, earlier_claims, verified_only,
                                             time_sensitive)
    premises = [c for c in checked if c["status"] == "accepted"]
    missing = [c for c in checked if c["status"] != "accepted"]
    for c in missing:
        found.append(caught("faithful.premise", "planner", "dropped",
                            f"premise not verified: {c['subject']} {c['relation']} {c['object']}"))
    if not premises:
        yield trace.note("planner", "No premise could be verified", "Not calculating from unverified facts.", "fail")
        return {"tier": "reasoning", "mode": mode, "plan": ["factual", "reasoning"], "confidence": "low",
                "answer": "I can't answer this reliably: the calculation needs real-world facts I could not verify, "
                          "and I'd rather not guess them.",
                "path": [], "premises": [], "premise_path": checked, "final_answer": "", "caught": found,
                "withheld": sum(c["status"] == "unverified" for c in checked), "cacheable": False,
                "stopped": {"reason": "premise_unverified", "detail": "no premise passed verification"}}
    yield trace.note("planner", f"{len(premises)} premise(s) verified",
                     "; ".join(f"{p['subject']} {p['relation'].replace('_', ' ')} {p['object']}" for p in premises), "ok")
    res = yield from reasoning.answer(question, mode, trace, prior, earlier_answers, premises)
    return {**res, "plan": ["factual", "reasoning"], "premise_path": checked, "caught": found + res["caught"],
            "withheld": sum(c["status"] == "unverified" for c in checked),
            "cacheable": res["cacheable"] and not missing}

"""Reasoning tier: step-by-step solutions that are checked, backtracked and cross-checked.

Per solution:
  - every arithmetic step is machine-checked (expression vs. claimed result)
  - a separate checker looks for the first step that does not follow logically
  - on a contradiction the solver goes back to that step and redoes it from there,
    keeping the steps before it
Across solutions: independent solutions must agree on the final answer
(self-consistency); disagreement draws more solutions, up to the mode's limit.

The three modes trade cost for caution. Edit MODES to retune them.
"""
from collections import Counter

from .. import llm
from . import mathcheck
from .trace import Trace

MODES = {
    # strictest: 3 independent solutions that must all agree (later, >=75% with at least 3 agreeing),
    # every step logic-checked, up to 3 backtracks each
    "conservative": {"samples": 3, "max_samples": 5, "logic_check": True, "max_backtracks": 3, "agreement": "strong"},
    # 2 solutions, majority must agree, logic-checked, 2 backtracks each
    "balanced": {"samples": 2, "max_samples": 3, "logic_check": True, "max_backtracks": 2, "agreement": "majority"},
    # fastest: 1 solution, arithmetic machine-checked only, 1 backtrack
    "aggressive": {"samples": 1, "max_samples": 1, "logic_check": False, "max_backtracks": 1, "agreement": "single"},
}
STRICTNESS = {"aggressive": 0, "balanced": 1, "conservative": 2}

SOLVE_SYSTEM = """Solve the problem step by step. Each step states one thing.
When a step computes a number, put the pure arithmetic in "expression" (digits, + - * / ** % and parentheses only;
no units, words or variables) and the value you get in "result". Otherwise leave both empty.
Return only JSON: {"steps": [{"text": "...", "expression": "", "result": ""}], "final_answer": "..."}"""

RESUME_SYSTEM = SOLVE_SYSTEM + """
You are resuming a solution. The steps given as VERIFIED are correct: keep them exactly and continue after them.
The step that came next was wrong for the reason given; do not repeat that mistake. Return ALL steps (verified ones first)."""

CHECK_SYSTEM = """You are checking someone else's solution; do not assume it is right.
Find the FIRST step that is wrong: it misreads the problem, does not follow from the problem and earlier steps,
uses a wrong formula, or contradicts an earlier step. Arithmetic has already been machine-checked; focus on logic and setup.
Also check the final answer actually answers the question asked.
Return only JSON: {"valid": true, "first_bad_step": null, "reason": ""}"""


def _parse_solution(data) -> dict:
    if not isinstance(data, dict):
        raise llm.LLMBadOutput("expected a solution object")
    steps = []
    for s in data.get("steps") or []:
        if isinstance(s, dict) and str(s.get("text", "")).strip():
            steps.append({"text": str(s["text"]).strip(), "expression": str(s.get("expression") or "").strip(),
                          "result": str(s.get("result") or "").strip()})
        elif isinstance(s, str) and s.strip():
            steps.append({"text": s.strip(), "expression": "", "result": ""})
    return {"steps": steps, "final_answer": str(data.get("final_answer", "")).strip()}


def _steps_txt(steps: list[dict]) -> str:
    lines = []
    for i, s in enumerate(steps, 1):
        calc = f"  [{s['expression']} = {s['result']}]" if s["expression"] else ""
        lines.append(f"{i}. {s['text']}{calc}")
    return "\n".join(lines)


def answer_key(final: str) -> str:
    n = mathcheck.to_number(final)
    return mathcheck.fmt(round(n, 6)) if n is not None else " ".join(final.lower().strip(" .").split())


def first_arithmetic_error(steps: list[dict]) -> tuple[int, str] | None:
    for i, s in enumerate(steps):
        err = mathcheck.check_step(s)
        if err:
            return i, err
    return None


def _logic_check(question: str, sol: dict) -> tuple[int, str] | None:
    d = llm.chat_json(CHECK_SYSTEM, f"Problem: {question}\n\nSolution:\n{_steps_txt(sol['steps'])}\n\n"
                                    f"Final answer: {sol['final_answer']}")
    if not isinstance(d, dict) or d.get("valid", True) is not False:
        return None
    try:
        idx = int(d.get("first_bad_step")) - 1
    except (TypeError, ValueError):
        idx = len(sol["steps"]) - 1
    return max(0, min(idx, max(len(sol["steps"]) - 1, 0))), str(d.get("reason", "inconsistent step"))[:300]


def solve_once(question: str, cfg: dict, trace: Trace, label: str, temperature: float):
    """Generator: one solution, checked and backtracked. Returns the solution with verified flag."""
    ev = trace.start("reasoning", f"{label}: solving step by step")
    yield ev
    sol = _parse_solution(llm.chat_json(SOLVE_SYSTEM, f"Problem: {question}", temperature))
    yield trace.settle(ev, "ok", f"{len(sol['steps'])} steps, answer: {sol['final_answer'] or '(none)'}",
                       steps=sol["steps"])

    backtracks = 0
    while True:
        ev = trace.start("reasoning", f"{label}: checking every step for contradictions")
        yield ev
        bad = first_arithmetic_error(sol["steps"])
        kind = "arithmetic"
        if bad is None and cfg["logic_check"]:
            bad, kind = _logic_check(question, sol), "logic"
        if bad is None:
            what = "arithmetic machine-checked" + (", logic checked independently" if cfg["logic_check"] else "")
            yield trace.settle(ev, "ok", f"No contradictions ({what}).")
            return {**sol, "verified": True, "backtracks": backtracks}

        idx, why = bad
        if backtracks >= cfg["max_backtracks"]:
            yield trace.settle(ev, "fail", f"Step {idx + 1} still wrong ({kind}: {why}); backtrack limit reached.")
            return {**sol, "verified": False, "backtracks": backtracks, "problem": f"step {idx + 1}: {why}"}

        backtracks += 1
        yield trace.settle(ev, "warn", f"Contradiction at step {idx + 1} ({kind}): {why}. "
                                       f"Going back to step {idx + 1} (backtrack {backtracks}/{cfg['max_backtracks']}).")
        ev = trace.start("reasoning", f"{label}: redoing from step {idx + 1}",
                         f"keeping steps 1-{idx} as verified" if idx else "starting over")
        yield ev
        kept = sol["steps"][:idx]
        redo = _parse_solution(llm.chat_json(
            RESUME_SYSTEM,
            f"Problem: {question}\n\nVERIFIED steps:\n{_steps_txt(kept) or '(none)'}\n\n"
            f"Wrong next step: {sol['steps'][idx]['text'] if idx < len(sol['steps']) else '(final answer)'}\n"
            f"Why it is wrong: {why}",
            temperature,
        ))
        sol = {"steps": _splice(kept, redo["steps"]), "final_answer": redo["final_answer"]}
        yield trace.settle(ev, "ok", f"New answer: {sol['final_answer'] or '(none)'}", steps=sol["steps"])


def _splice(kept: list[dict], redo: list[dict]) -> list[dict]:
    """The resumed solution may repeat the verified steps or only continue after them."""
    def same(a, b):
        return " ".join(a["text"].lower().split()) == " ".join(b["text"].lower().split())

    if kept and len(redo) >= len(kept) and same(redo[0], kept[0]):
        return kept + redo[len(kept):]
    return kept + redo


def _agreed(cfg: dict, keys: list[str], total: int) -> tuple[str | None, int]:
    """keys: answers of verified solutions; total: all solutions drawn (unverified ones count against)."""
    if not keys:
        return None, 0
    top, n = Counter(keys).most_common(1)[0]
    if cfg["agreement"] == "strong":
        ok = n >= cfg["samples"] and n >= 0.75 * total
    elif cfg["agreement"] == "majority":
        ok = n > total / 2 and n >= min(2, cfg["samples"])
    else:
        ok = True
    return (top if ok else None), n


def answer(question: str, mode: str, trace: Trace):
    """Generator: yields trace events, returns the tier result."""
    cfg = MODES[mode]
    yield trace.note("reasoning", f"Mode: {mode}",
                     f"{cfg['samples']} independent solution(s) (up to {cfg['max_samples']}), agreement: "
                     f"{cfg['agreement']}, logic check: {'on' if cfg['logic_check'] else 'off'}, "
                     f"max backtracks: {cfg['max_backtracks']}")
    solutions: list[dict] = []
    winner = None
    while len(solutions) < cfg["max_samples"]:
        n = len(solutions)
        sol = yield from solve_once(question, cfg, trace, f"Solution {n + 1}", 0 if n == 0 else 0.7)
        solutions.append(sol)
        if len(solutions) < cfg["samples"]:
            continue
        keys = [answer_key(s["final_answer"]) for s in solutions if s["verified"]]
        winner, votes = _agreed(cfg, keys, len(solutions))
        ev = trace.start("reasoning", "Cross-checking independent solutions")
        yield ev
        answers = ", ".join(f"#{i + 1}: {s['final_answer'] or '-'}{'' if s['verified'] else ' (unverified)'}"
                            for i, s in enumerate(solutions))
        if winner is not None:
            yield trace.settle(ev, "ok", f"Consistent ({votes}/{len(solutions)} agree). {answers}")
            break
        more = len(solutions) < cfg["max_samples"]
        yield trace.settle(ev, "warn" if more else "fail",
                           f"Not consistent yet. {answers}" + (" Drawing another independent solution." if more else ""))

    verified = [s for s in solutions if s["verified"]]
    if winner is not None:
        best = next(s for s in verified if answer_key(s["final_answer"]) == winner)
        confidence = "high"
    elif verified:
        top = Counter(answer_key(s["final_answer"]) for s in verified).most_common(1)[0][0]
        best = next(s for s in verified if answer_key(s["final_answer"]) == top)
        confidence = "medium"
    else:
        best, confidence = solutions[0], "low"

    lines = [f"**Answer: {best['final_answer'] or 'not determined'}**", "", "How I got there:"]
    lines += [f"{i}. {s['text']}" + (f" ({s['expression']} = {s['result']})" if s["expression"] else "")
              for i, s in enumerate(best["steps"], 1)]
    if confidence == "medium":
        lines += ["", "_Note: the independent solutions did not fully agree, so treat this with some caution._"]
    if confidence == "low":
        lines += ["", f"_Warning: I could not verify this solution ({best.get('problem', 'checks failed')})._"]
    return {
        "tier": "reasoning",
        "mode": mode,
        "answer": "\n".join(lines),
        "confidence": confidence,
        "path": best["steps"],
        "final_answer": best["final_answer"],
        "solutions": len(solutions),
        "cacheable": confidence == "high",
    }

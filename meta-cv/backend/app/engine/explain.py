"""Open-ended questions: designs, explanations, how-tos.

The factual and reasoning tiers verify claims and calculations. A question like "design 30 subnets of 2000 hosts" or
"explain how authentication works" has no single fact to verify, so those tiers found nothing to accept and declined -
a refusal on a question every plain chatbot answers. For such a question (the router marks it open_ended), this tier:
  1. lets the model write the answer, with every calculation it uses marked as [[expression = result]];
  2. machine-checks each marked calculation and corrects a wrong result in the text (mathcheck), so the numbers in a design
     are never wrong arithmetic;
  3. labels the rest honestly: the explanation itself is the model's, not verified against sources (medium confidence at
     most, low if a calculation had to be corrected).
It is never used for what the other tiers exist to stop: a plot or a work that could not be found, a suspected false
premise, a question that risks an invented detail, or one flagged as an injection attempt. Those still decline.
"""
import re

from .. import llm
from . import mathcheck
from .router import caught
from .trace import Trace

EXPLAIN_SYSTEM = """Answer the user's question fully and accurately: a design, an explanation or steps, as asked.
Mark EVERY calculation you use as [[expression = result]], with only digits, + - * / ** ( ) in the expression, for
example [[2**11 - 2 = 2046]] or [[256 - 248 = 8]]. Put each mark where the number is used.
Do not invent names, quotes, sources or statistics; if something depends on facts you are unsure of, say so.
At most 500 words."""

_MARK = re.compile(r"\[\[\s*([^\]=]+?)\s*=\s*([^\]]+?)\s*\]\]")


def check_marks(text: str) -> tuple[str, int, list[str]]:
    """(text with each mark replaced by 'expression = result', marks checked, corrections). A wrong result is replaced by the
    machine's value and listed; an expression that is not plain arithmetic is shown as written and not counted."""
    fixes: list[str] = []
    checked = 0

    def repl(m):
        nonlocal checked
        expr, claimed = m.group(1).strip(), m.group(2).strip()
        try:
            value = mathcheck.evaluate(expr)
        except mathcheck.NotArithmetic:
            return f"{expr} = {claimed}"
        checked += 1
        if mathcheck.matches(value, claimed):
            return f"{expr} = {claimed}"
        fixes.append(f"{expr} = {mathcheck.fmt(value)}, not {claimed}")
        return f"{expr} = {mathcheck.fmt(value)}"
    return _MARK.sub(repl, text), checked, fixes


def answer(question: str, history_txt: str, trace: Trace, why_declined: str):
    """Generator: yields trace events, returns a tier result."""
    ev = trace.start("explain", "Answering an open-ended question",
                     f"The verifying tiers stopped ({why_declined}). This asks for a design or an explanation, not one checkable "
                     "fact, so the model writes it and every calculation in it is machine-checked.")
    yield ev
    user = (f"Conversation so far:\n{history_txt}\n\n" if history_txt else "") + f"Question: {question}"
    text = str(llm.chat_text(EXPLAIN_SYSTEM, user) or "").strip()
    if not text:
        yield trace.settle(ev, "fail", "The model gave no answer.")
        return None
    text, checked, fixes = check_marks(text)
    found = [caught("faithful.logic", "explain", "corrected", f) for f in fixes]
    yield trace.settle(ev, "warn" if fixes else "ok",
                       f"{checked} calculation(s) machine-checked" + (f"; corrected: {'; '.join(fixes)}" if fixes else ", all correct")
                       + ". The explanation itself is the model's, not verified against sources.")
    note = (f"_Written by the model: {checked} calculation(s) in it were machine-checked"
            + (f" and {len(fixes)} corrected" if fixes else "") + "; the explanation itself was not verified against sources._")
    return {"tier": "explain", "answer": f"{text}\n\n{note}", "confidence": "low" if fixes else "medium",
            "path": [], "caught": found, "withheld": 0, "cacheable": False, "stopped": None,
            "calculations_checked": checked}

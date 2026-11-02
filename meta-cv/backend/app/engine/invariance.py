"""Instruction-invariance check: does the instruction-like text in a question change what gets verified?

A pattern detector only says a question *looks* like an attack. This measures the effect instead: when the
tripwire fires, the question is answered a second time with the instruction-like sentences removed, and the two
verified results are compared. Instructions that only add noise leave the verified content unchanged, and the
original answer stands. If the original run accepted claims the clean run did not, or reached a different final
number, the instructions steered the outcome: the clean answer is used and the difference is reported as
adversarial.injection. Paraphrased attacks the tripwire misses are not re-run; the data rule and verification
still apply to them.
"""
import re

from . import mathcheck
from .entities import normalize

_SENTENCE = re.compile(r"[^.!?;\n]+[.!?;]*")


def strip(question: str) -> str:
    """The question without its instruction-like sentences ("" when nothing else is left)."""
    from .. import safety  # noqa: PLC0415

    kept = [s.strip() for s in _SENTENCE.findall(question) if s.strip() and not safety.injection_signals(s)]
    return " ".join(kept)


def drain(gen):
    """Run a tier generator to the end without yielding its trace events; returns its result."""
    while True:
        try:
            next(gen)
        except StopIteration as stop:
            return stop.value


def _claims(result: dict) -> set[str]:
    items = list(result.get("path") or []) + list(result.get("premises") or [])
    return {f"{normalize(c['subject'])}|{c['relation']}|{normalize(c['object'])}" for c in items
            if c.get("kind", "claim") == "claim" and c.get("status", "accepted") == "accepted"
            and c.get("subject") and c.get("object")}


def compare(original: dict, clean: dict) -> list[str]:
    """What the original run verified that the clean run did not (empty: the instructions changed nothing)."""
    diffs = [f"accepted {c.replace('|', ' ')}" for c in sorted(_claims(original) - _claims(clean))]
    a, b = original.get("final_answer"), clean.get("final_answer")
    if a and b and mathcheck.to_number(a) is not None and mathcheck.to_number(b) is not None:
        if not mathcheck.matches(mathcheck.to_number(b), a):
            diffs.append(f"final answer {a} instead of {b}")
    elif a and b and normalize(str(a)) != normalize(str(b)):
        diffs.append(f"final answer '{a}' instead of '{b}'")
    return diffs

"""Time-sensitive facts.

Facts that change over time carry valid_from / valid_to (YYYY, YYYY-MM or YYYY-MM-DD; an empty valid_to
means still true). Every accepted graph fact with a validity period carries it into the answer
("from 2010 until 2021"), so an ended fact is never phrased as current.

When the router marks a question time-sensitive ("current", "latest", "now", "who is the CEO of ..."),
every accepted node is also reviewed before the answer is written:
  - a graph fact whose valid_to has passed is no longer current: dropped (temporal.stale), so a former
    office-holder is never presented as the current one;
  - a graph fact not re-verified within FRESH_DAYS is kept with an "as of <date>" note;
  - a claim confirmed only by the model is kept with a note that model knowledge may be out of date.
Any note caps confidence at "medium", and the answer is not stored for reuse (pipeline.py).
"""
import calendar
import re
import time
from datetime import date

from .. import config
from .router import caught
from .trace import Trace

_DATE = re.compile(r"^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?$")
MODEL_NOTE = "from model knowledge, which may be out of date"


def period_end(s: str | None) -> date | None:
    """The last day a 'YYYY', 'YYYY-MM' or 'YYYY-MM-DD' value covers; None if it is not such a value."""
    m = _DATE.match(str(s or "").strip())
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2) or 12), m.group(3)
    try:
        return date(y, mo, int(d) if d else calendar.monthrange(y, mo)[1])
    except ValueError:
        return None


def ended(valid_to: str | None, today: date | None = None) -> bool:
    end = period_end(valid_to)
    return bool(end and end < (today or date.today()))


def period(valid_from: str | None, valid_to: str | None) -> str:
    if valid_from and valid_to:
        return f"from {valid_from} until {valid_to}"
    if valid_to:
        return f"until {valid_to}"
    return f"since {valid_from}" if valid_from else ""


def _note(*parts: str) -> str:
    return "; ".join(p for p in parts if p)


def _fmt(n: dict) -> str:
    return f"{n['subject']} {n['relation'].replace('_', ' ')} {n['object']}"


def review(store, nodes: list[dict], time_sensitive: bool, trace: Trace):
    """Generator: attach validity to accepted graph facts and, for a time-sensitive question, drop facts that
    have ended and flag ones that may be out of date. Returns (nodes, caught)."""
    found: list[dict] = []
    out: list[dict] = []
    dropped, flagged = [], []
    now = time.time()
    for n in nodes:
        if n.get("status") != "accepted":
            out.append(n)
            continue
        n = dict(n)
        info = None
        if n.get("source") in ("graph", "graph-corrected") and hasattr(store, "fact_info"):
            try:
                info = store.fact_info(n["subject"], n["relation"], n["object"])
            except ValueError:
                info = None
        if info and (info.get("valid_from") or info.get("valid_to")):
            n["valid_from"], n["valid_to"] = info.get("valid_from"), info.get("valid_to")
            n["time_note"] = period(n["valid_from"], n["valid_to"])
        if time_sensitive:
            if info and ended(info.get("valid_to")):
                n.update(status="rejected", ended=True, reason=f"no longer current ({n['time_note']})")
                found.append(caught("temporal.stale", "factual", "dropped",
                                    f"{_fmt(n)} is not current: {n['time_note']}"))
                dropped.append(n)
            elif info and info.get("verified_at") and now - float(info["verified_at"]) > config.FRESH_DAYS * 86400:
                verified = time.strftime("%Y-%m-%d", time.gmtime(float(info["verified_at"])))
                n.update(stale=True, time_note=_note(n.get("time_note", ""), f"as of {verified}, when it was last verified"))
                found.append(caught("temporal.stale", "factual", "flagged",
                                    f"{_fmt(n)} was last verified on {verified}"))
                flagged.append(n)
            elif n.get("source") in ("model", "cache"):
                n.update(stale=True, time_note=_note(n.get("time_note", ""), MODEL_NOTE))
                found.append(caught("temporal.stale", "factual", "flagged", f"{_fmt(n)} is {MODEL_NOTE}"))
                flagged.append(n)
        out.append(n)

    if time_sensitive:
        accepted = [n for n in out if n.get("status") == "accepted"]
        lines = [f"{len(accepted) - len(flagged)} fact(s) current and recently verified"]
        if dropped:
            lines.append("no longer current, dropped: " + "; ".join(f"{_fmt(n)} ({n['time_note']})" for n in dropped))
        if flagged:
            lines.append("kept with a date note: " + "; ".join(f"{_fmt(n)} ({n['time_note']})" for n in flagged))
        yield trace.note("temporal", "Checking the facts are still current",
                         "This question asks about the present. " + ". ".join(lines) + ".",
                         "fail" if dropped and not accepted else "warn" if dropped or flagged else "ok")
    return out, found

"""Per-question accounting: LLM calls and tokens, graph round trips, and the budget they must stay within."""
import threading
from contextlib import contextmanager
from contextvars import ContextVar

from . import config

_meter: ContextVar[dict | None] = ContextVar("meter", default=None)
_lock = threading.Lock()  # checks run in parallel (engine/parallel.py) and share one meter per question


class BudgetExceeded(RuntimeError):
    def __init__(self, what: str, limit: int):
        super().__init__(f"per-question {what} budget ({limit}) used up")
        self.what, self.limit = what, limit


def new_meter(limits: dict | None = None) -> dict:
    return {"prompt": 0, "completion": 0, "calls": 0, "graph": 0,
            "limits": limits if limits is not None else
            {"calls": config.MAX_LLM_CALLS, "tokens": config.MAX_TOKENS, "graph": config.MAX_GRAPH_CALLS}}


def current() -> dict | None:
    return _meter.get()


@contextmanager
def metering(meter: dict):
    token = _meter.set(meter)
    try:
        yield meter
    finally:
        _meter.reset(token)


def metered(gen, meter: dict):
    """Drive a generator so every call made inside it is counted in `meter`.

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


def before_llm_call() -> None:
    m = _meter.get()
    if not m or not m.get("limits"):
        return
    lim = m["limits"]
    if lim.get("calls") and m["calls"] >= lim["calls"]:
        raise BudgetExceeded("LLM call", lim["calls"])
    if lim.get("tokens") and m["prompt"] + m["completion"] >= lim["tokens"]:
        raise BudgetExceeded("token", lim["tokens"])


def record_llm(prompt: int, completion: int) -> None:
    m = _meter.get()
    if m is not None:
        with _lock:
            m["prompt"] += prompt
            m["completion"] += completion
            m["calls"] += 1


def record_graph(n: int = 1) -> None:
    m = _meter.get()
    if m is None:
        return
    with _lock:
        m["graph"] += n
    lim = (m.get("limits") or {}).get("graph")
    if lim and m["graph"] > lim:
        raise BudgetExceeded("graph query", lim)

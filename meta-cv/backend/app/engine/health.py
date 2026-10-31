"""Global breaker: when recent turns are mostly rejections, or the model or graph keep failing,
answer in verified-only mode (graph-supported claims only, no model fallback) until it recovers."""
import threading
from collections import deque

from .. import config


class Health:
    def __init__(self):
        self._events: deque = deque(maxlen=config.BREAKER_WINDOW)
        self._lock = threading.Lock()

    def record(self, rejected: bool = False, error: bool = False) -> None:
        with self._lock:
            self._events.append((rejected, error))

    def reset(self) -> None:
        with self._lock:
            self._events.clear()

    def state(self) -> dict:
        with self._lock:
            events = list(self._events)
        n = len(events)
        reject = sum(r for r, _ in events) / n if n else 0.0
        error = sum(e for _, e in events) / n if n else 0.0
        reason = None
        if n >= config.BREAKER_MIN_TURNS:
            if reject >= config.BREAKER_REJECT_RATE:
                reason = f"{reject:.0%} of the last {n} answers needed claims dropped or stopped"
            elif error >= config.BREAKER_ERROR_RATE:
                reason = f"{error:.0%} of the last {n} questions hit model or graph errors"
        return {"verified_only": reason is not None, "reason": reason, "turns": n,
                "reject_rate": round(reject, 3), "error_rate": round(error, 3)}


health = Health()
